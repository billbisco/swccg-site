"""Aggregate-only adapter for the existing GEMP completed-UTC-day rollup.

This reads one private, bounded receipt produced by the authorized GEMP source
owner. It never connects to a live database, reads raw event rows, generates
guest identifiers, or translates an instrumented event into all player activity.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .contracts import AdapterResult, Metric, check_epoch
from .health import read_receipt


# Existing MetricsService.rollupDay's generic event keys only. In particular,
# game_start has a bot dimension in the existing event schema. Refuse arbitrary
# detail keys and dynamic suffixes rather than risking high-cardinality labels.
EVENT_IDS = {
    "play_click": "player.instrumented_play_click",
    "game_start": "player.instrumented_bot_game_start",
    "turn_reached": "player.instrumented_turn_reached",
    "game_end": "player.instrumented_game_end",
    "game_abandoned": "player.instrumented_game_abandoned",
    "game_resume": "player.instrumented_game_resume",
    "signup_after_guest": "player.instrumented_signup_after_guest",
    "outbound_click": "player.instrumented_outbound_click",
}
MAX_DAYS = 60
MAX_SOURCE_COUNT = 2**31 - 1  # Existing SQL INT count column.
MAX_RECEIPT_AGE = 30 * 3600
WINDOWS = (
    ("completed_1d", 0, 1), ("prior_1d", 1, 1),
    ("completed_7d", 0, 7), ("prior_7d", 7, 7),
    ("completed_30d", 0, 30), ("prior_30d", 30, 30),
)
RECEIPT_KEYS = frozenset({"schema", "source", "environment", "observed_at", "validated_events", "days"})
ROW_KEYS = frozenset({"day", "metrics"})
ALLOWED_KEYS = frozenset(EVENT_IDS) | {"_rolled"}


def _utc_day(epoch: int) -> date:
    check_epoch(epoch)
    return datetime.fromtimestamp(epoch, timezone.utc).date()


def _parse_day(raw) -> date:
    if not isinstance(raw, str) or len(raw) != 10:
        raise ValueError("invalid completed day")
    day = date.fromisoformat(raw)
    if day.isoformat() != raw:
        raise ValueError("invalid completed day")
    return day


def _unknown(now: int, status: str, detail: str) -> AdapterResult:
    return AdapterResult("player", now, (), status=status, detail=detail)


def _validate_receipt(document: object, now: int, validated_events: frozenset[str]):
    if (not isinstance(document, dict) or set(document) != RECEIPT_KEYS
            or type(document["schema"]) is not int or document["schema"] != 1
            or document["source"] != "gemp_daily" or document["environment"] != "play"):
        raise ValueError("invalid aggregate receipt envelope")
    observed = document["observed_at"]
    observed_day = _utc_day(observed)
    if observed > now:
        raise ValueError("future aggregate receipt")
    # Capability must agree with deployment's separately configured, owner-
    # validated event allowlist. A receipt alone cannot assert new coverage.
    declared = document["validated_events"]
    if (not isinstance(declared, list) or len(declared) > len(EVENT_IDS)
            or any(not isinstance(event, str) or event not in EVENT_IDS for event in declared)
            or len(set(declared)) != len(declared) or frozenset(declared) != validated_events):
        raise ValueError("invalid source capability")
    rows = document["days"]
    if not isinstance(rows, list) or len(rows) > MAX_DAYS:
        raise ValueError("daily receipt out of bounds")
    days = {}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != ROW_KEYS:
            raise ValueError("invalid daily row")
        day = _parse_day(row["day"])
        if not observed_day - timedelta(days=MAX_DAYS) <= day < observed_day or day in seen:
            raise ValueError("duplicate or out-of-window day")
        seen.add(day)
        values = row["metrics"]
        if (not isinstance(values, dict) or not set(values) <= ALLOWED_KEYS
                or any(type(count) is not int or not 0 <= count <= MAX_SOURCE_COUNT for count in values.values())):
            raise ValueError("invalid daily counts")
        # _rolled is written transactionally with the finished UTC day's
        # totals. No marker means incomplete evidence, not a zero day.
        if values.get("_rolled") == 1:
            days[day] = values
        elif "_rolled" in values:
            raise ValueError("invalid rollup marker")
    return observed, days


def parse_gemp_daily(document: object, now: int, validated_events: frozenset[str],
                     max_age_seconds: int = MAX_RECEIPT_AGE) -> AdapterResult:
    """Return only complete UTC-window totals or null, with coverage per window."""
    if (not isinstance(validated_events, frozenset) or not validated_events <= EVENT_IDS.keys()
            or type(max_age_seconds) is not int or max_age_seconds <= 0):
        raise ValueError("invalid analytics configuration")
    end_day = _utc_day(now)
    try:
        observed, days = _validate_receipt(document, now, validated_events)
    except (ValueError, TypeError, OverflowError):
        return _unknown(now, "error", "invalid_receipt")
    metrics = [
        Metric("player.source_checked_at", observed, "epoch_seconds"),
        Metric("player.window_end_at", int(datetime.combine(end_day, datetime.min.time(), timezone.utc).timestamp()), "epoch_seconds"),
        Metric("player.validated_event_types", len(validated_events), "count"),
    ]
    partial = False
    for dim, offset, length in WINDOWS:
        window_end = end_day - timedelta(days=offset)
        dates = tuple(window_end - timedelta(days=i) for i in range(1, length + 1))
        covered = sum(day in days for day in dates)
        metrics.extend((Metric("player.covered_days", covered, "count", dim),
                        Metric("player.expected_days", length, "count", dim)))
        complete = covered == length
        partial |= not complete
        for event, metric_id in EVENT_IDS.items():
            # Source implementation emits zero by absence only on a known
            # rolled day, and only when the actual emitter was validated.
            count = sum(days[day].get(event, 0) for day in dates) if complete and event in validated_events else None
            metrics.append(Metric(metric_id, count, "count", dim))
    if now - observed > max_age_seconds:
        status, detail = "degraded", "source_old"
    elif len(validated_events) != len(EVENT_IDS):
        status, detail = "degraded", "event_coverage_unverified"
    elif partial:
        status, detail = "degraded", "partial_days"
    else:
        status, detail = "ok", None
    return AdapterResult("player", now, tuple(metrics), status=status, detail=detail)


@dataclass(frozen=True)
class GempDailyAdapter:
    """Read the private Play-only rollup projection; no default source path."""
    path: Path
    validated_events: frozenset[str] = frozenset()
    max_age_seconds: int = MAX_RECEIPT_AGE
    read: Callable[[Path], dict] = read_receipt

    def __post_init__(self):
        if (not isinstance(self.validated_events, frozenset) or not self.validated_events <= EVENT_IDS.keys()
                or type(self.max_age_seconds) is not int or self.max_age_seconds <= 0):
            raise ValueError("invalid analytics configuration")

    def collect(self, now: int) -> AdapterResult:
        try:
            return parse_gemp_daily(self.read(self.path), now, self.validated_events, self.max_age_seconds)
        except FileNotFoundError:
            return _unknown(now, "unavailable", "missing_receipt")
        except (OSError, ValueError, TypeError, RecursionError):
            return _unknown(now, "error", "invalid_receipt")
