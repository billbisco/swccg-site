"""Bounded read-only operational adapters; no provider traffic or source mutations.

All source paths and systemd units are deployment-supplied configuration. The
exported samples contain only allowlisted aggregate fields; source contents and
exception strings must never be included in a dashboard result.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
from typing import Callable, Mapping

from .contracts import AdapterResult, Metric


MAX_SAFE_INT = 2**53 - 1
MAX_RECEIPT_BYTES = 64 * 1024
MAX_PROC_BYTES = 16 * 1024
MAX_SYSTEMD_UNITS = 18  # At most seven metrics per unit; fits the contract's 128-sample cap.
READ_TIMEOUT_SECONDS = 2
SYSTEMD_BUDGET_SECONDS = 8


def _bounded_text(path: Path, limit: int) -> str:
    # A file configuration error (pipe/device) must not block the collector.
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("source must be a regular file")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("bounded input exceeded")
    return raw.decode("utf-8", errors="strict")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_receipt(path: Path) -> dict:
    """Decode a bounded JSON object; ignore irrelevant fields after parsing."""
    result = json.loads(_bounded_text(path, MAX_RECEIPT_BYTES), object_pairs_hook=_unique_object)
    if not isinstance(result, dict):
        raise ValueError("receipt must be an object")
    return result


def _count(value) -> int | None:
    return value if type(value) is int and 0 <= value <= MAX_SAFE_INT else None


def _boolean(value) -> int | None:
    return int(value) if type(value) is bool else None


def _epoch(value, *, latest: int | None = None) -> int | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        result = int(dt.astimezone(timezone.utc).timestamp())
    except (ValueError, OverflowError, OSError):
        return None
    if result < 0 or result > 4_102_444_800 or (latest is not None and result > latest):
        return None
    return result


def _sample(metric_id: str, value, unit: str, dimension: str = "all") -> Metric:
    return Metric(metric_id, value, unit, dimension)


def _unknown(source: str, now: int, detail: str, status: str = "unavailable") -> AdapterResult:
    return AdapterResult(source=source, observed_at=now, samples=(), status=status, detail=detail)


def _freshness(observed: int, now: int, max_age: int) -> tuple[str, str | None]:
    return ("degraded", "source_old") if now - observed > max_age else ("ok", None)


ARCHIVE_COUNTS = ("verified", "failed", "pending", "retry", "unverified", "ok", "unknown")
ARCHIVE_MODES = ("healthy", "open", "half_open", "auth_blocked", "probe_blocked")
ARCHIVE_OUTCOMES = ("completed", "paused", "failed")
ARCHIVE_REASONS = {"provider unavailable": "provider_unavailable", "canary unverified; next bounded probe deferred": "canary_unverified"}
ARCHIVE_LEDGER_METRICS = ("verified_24h", "verified_7d", "verified_undated", "failed_transient", "failed_due", "failed_other", "verified_manpc", "manpc_candidates", "manpc_deferred")
ARCHIVE_SPECS = (
    tuple((f"archive.{status}", "count", "all") for status in ARCHIVE_COUNTS)
    + tuple((f"archive.mode_{mode}", "boolean", "all") for mode in ARCHIVE_MODES)
    + tuple((f"archive.run_{outcome}", "boolean", "all") for outcome in ARCHIVE_OUTCOMES)
    + (("archive.run_success", "boolean", "all"),
       ("archive.last_verified_at", "epoch_seconds", "all"),
       ("archive.last_submission_at", "epoch_seconds", "all"),
       ("archive.next_eligible_at", "epoch_seconds", "all"),
       ("archive.oldest_pending_at", "epoch_seconds", "all"),
       ("archive.source_checked_at", "epoch_seconds", "all"),
       ("archive.awaiting_canary", "boolean", "all"))
    + tuple((f"archive.reason_{reason}", "boolean", "all") for reason in ARCHIVE_REASONS.values())
    + tuple((f"archive.{name}", "count", "all") for name in ARCHIVE_LEDGER_METRICS)
)


def archive_ledger_metrics(path: Path, now: int, expected: Mapping) -> dict:
    """Current ledger census, not an append-only event history. No URL leaves this function."""
    from collections import Counter
    data = json.loads(_bounded_text(path, 24 * 1024 * 1024), object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or len(data) > 100000 or any(not isinstance(v, dict) for v in data.values()):
        raise ValueError("invalid archive ledger")
    if Counter(v.get("status") for v in data.values()) != Counter(expected):
        raise ValueError("ledger and receipt disagree")
    result = dict.fromkeys(ARCHIVE_LEDGER_METRICS, 0)
    for row in data.values():
        status = row.get("status")
        verify = row.get("verify") or {}
        if not isinstance(verify, dict):
            raise ValueError("invalid verification receipt")
        if status == "verified":
            at = _epoch(verify.get("at"), latest=now)
            if at is not None:
                result["verified_24h"] += now - at <= 86400
                result["verified_7d"] += now - at <= 7 * 86400
            elif at is None:
                # An undated verified row makes historical coverage incomplete.
                result["verified_undated"] += 1
            result["verified_manpc"] += verify.get("lane") == "manpc"
        elif status == "failed":
            transient = any(s in str(row.get("error") or "").lower() for s in ("stalled spn2", "connection refused", "errno 111", "timed out", "429", "503", "502"))
            result["failed_transient" if transient else "failed_other"] += 1
            at = _epoch(row.get("last"), latest=now)
            result["failed_due"] += bool(transient and at is not None and now - at > 7 * 86400)
        elif status in {"ok", "pending", "unverified"} and (row.get("capture") or row.get("job_id")):
            result["manpc_candidates"] += 1
            result["manpc_deferred"] += verify.get("defer") == "manpc"
    return result


def parse_archive_health(data: dict, now: int, max_age_seconds: int, ledger: Path | None = None) -> AdapterResult:
    """Read owner's URL-ledger status counts, never claim per-version proof."""
    if not isinstance(data, dict):
        return _unknown("archive", now, "invalid_receipt", "error")
    observed = _epoch(data.get("checked_at"), latest=now)
    if (type(data.get("schema")) is not int or data["schema"] != 1 or observed is None
            or data.get("owner") != "server-wayback-queue"):
        return _unknown("archive", now, "invalid_receipt", "error")

    counts = data.get("ledger_status_counts")
    # This schema emits a complete Counter of all URL ledger statuses. A missing
    # key is zero only if the entire map validates. Never mistake absent input
    # for an empty ledger; unexpected statuses stay in a single unknown bucket.
    valid_counts = (isinstance(counts, dict) and len(counts) <= 32
                    and all(_count(v) is not None for v in counts.values())
                    and sum(counts.values()) <= MAX_SAFE_INT)
    safe_counts = {key: None for key in ARCHIVE_COUNTS}
    if valid_counts:
        safe_counts = {key: counts.get(key, 0) for key in ARCHIVE_COUNTS}
        safe_counts["unknown"] += sum(v for k, v in counts.items() if k not in ARCHIVE_COUNTS)
    mode = data.get("mode")
    outcome = data.get("outcome")
    valid_mode = isinstance(mode, str) and mode in ARCHIVE_MODES
    valid_outcome = isinstance(outcome, str) and outcome in ARCHIVE_OUTCOMES
    metrics = [_sample(f"archive.{key}", safe_counts[key], "count") for key in ARCHIVE_COUNTS]
    metrics += [_sample(f"archive.mode_{key}", int(mode == key) if valid_mode else None, "boolean")
                for key in ARCHIVE_MODES]
    metrics += [_sample(f"archive.run_{key}", int(outcome == key) if valid_outcome else None, "boolean")
                for key in ARCHIVE_OUTCOMES]
    metrics += [
        _sample("archive.run_success", int(outcome == "completed") if valid_outcome else None, "boolean"),
        _sample("archive.last_verified_at", _epoch(data.get("last_verified_progress_at"), latest=observed), "epoch_seconds"),
        _sample("archive.last_submission_at", _epoch(data.get("last_submission_attempt_at"), latest=observed), "epoch_seconds"),
        _sample("archive.next_eligible_at", _epoch(data.get("next_eligible_attempt_at")), "epoch_seconds"),
        _sample("archive.oldest_pending_at", _epoch(data.get("oldest_pending_activity_at"), latest=observed), "epoch_seconds"),
        _sample("archive.source_checked_at", observed, "epoch_seconds"),
        _sample("archive.awaiting_canary", _boolean(data.get("awaiting_canary_verification")), "boolean"),
    ]
    reason = ARCHIVE_REASONS.get(data.get("reason")) if isinstance(data.get("reason"), str) else None
    metrics += [_sample(f"archive.reason_{key}", int(reason == key) if reason else None, "boolean") for key in ARCHIVE_REASONS.values()]
    census = {}
    if ledger is not None and valid_counts:
        try:
            census = archive_ledger_metrics(ledger, now, counts)
        except (OSError, ValueError, TypeError, RecursionError):
            pass
    metrics += [_sample(f"archive.{key}", census.get(key), "count") for key in ARCHIVE_LEDGER_METRICS]
    status, detail = _freshness(observed, now, max_age_seconds)
    if status == "ok" and not (valid_counts and valid_mode and valid_outcome):
        status, detail = "degraded", "partial_receipt"
    if status == "ok" and mode in ("open", "half_open"):
        status, detail = "backoff", "provider_backoff"
    if status == "ok" and mode in ("auth_blocked", "probe_blocked"):
        status, detail = "degraded", "provider_blocked"
    if status == "ok" and outcome == "failed":
        status, detail = "degraded", "worker_failed"
    if status == "ok" and outcome == "paused":
        status, detail = "degraded", "worker_paused"
    # observed_at is collection time for the transactional store. The actual
    # owner receipt time is explicitly exported, and never reset by polling.
    return AdapterResult(source="archive", observed_at=now, samples=tuple(metrics), status=status, detail=detail)


@dataclass(frozen=True)
class ArchiveHealthAdapter:
    path: Path
    max_age_seconds: int = 30 * 60
    read: Callable[[Path], dict] = read_receipt
    ledger: Path | None = None

    def collect(self, now: int) -> AdapterResult:
        try:
            return parse_archive_health(self.read(self.path), now, self.max_age_seconds, self.ledger)
        except FileNotFoundError:
            return _unknown("archive", now, "missing_receipt")
        except (OSError, ValueError, TypeError, RecursionError):
            return _unknown("archive", now, "invalid_receipt", "error")


BACKUP_SPECS = (("backup.last_verified_at", "epoch_seconds", "all"),
                ("backup.completed_tasks", "count", "all"),
                ("backup.failed_pending_tasks", "count", "all"),
                ("backup.pending_tasks", "count", "all"),
                ("backup.coverage_partial", "boolean", "all"),
                ("backup.source_checked_at", "epoch_seconds", "all"),
                ("backup.discovery_failures", "count", "all"))


def parse_backup_health(data: dict, now: int, max_age_seconds: int) -> AdapterResult:
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 2:
        return _unknown("backup", now, "invalid_receipt", "error")
    observed = _epoch(data.get("observed_at"), latest=now)
    if observed is None:
        return _unknown("backup", now, "invalid_receipt", "error")
    partial = _boolean(data.get("task_count_partial"))
    failed = _count(data.get("failed_pending_task_count")) if partial == 0 else None
    pending = _count(data.get("pending_task_count")) if partial == 0 else None
    if failed is not None and pending is not None and failed > pending:
        failed = pending = None
    discovery = data.get("discovery_failures")
    discovery_failures = len(discovery) if isinstance(discovery, list) else None
    # Count coverage is explicit in this receipt. Partial counts cannot be
    # represented as full backlog, even if a reader saw several task rows.
    values = (_epoch(data.get("last_actual_verified_backup_at"), latest=observed),
              _count(data.get("completed_tasks")),
              failed, pending, partial, observed, discovery_failures)
    status, detail = _freshness(observed, now, max_age_seconds)
    if status == "ok" and any(value is None for value in values):
        status, detail = "degraded", "partial_receipt"
    if status == "ok" and values[2] and values[2] > 0:
        status, detail = "degraded", "pending_failures"
    if status == "ok" and discovery_failures:
        status, detail = "degraded", "discovery_failed"
    return AdapterResult(source="backup", observed_at=now,
                         samples=tuple(_sample(spec[0], value, spec[1], spec[2])
                                       for spec, value in zip(BACKUP_SPECS, values)),
                         status=status, detail=detail)


@dataclass(frozen=True)
class BackupHealthAdapter:
    path: Path
    max_age_seconds: int = 26 * 3600
    read: Callable[[Path], dict] = read_receipt

    def collect(self, now: int) -> AdapterResult:
        try:
            return parse_backup_health(self.read(self.path), now, self.max_age_seconds)
        except FileNotFoundError:
            return _unknown("backup", now, "missing_receipt")
        except (OSError, ValueError, TypeError, RecursionError):
            return _unknown("backup", now, "invalid_receipt", "error")


MEMORY_KEYS = {"MemTotal": "memory_total", "MemAvailable": "memory_available",
               "SwapTotal": "swap_total", "SwapFree": "swap_free"}


def parse_meminfo(text: str) -> dict[str, int | None]:
    result = {metric: None for metric in MEMORY_KEYS.values()}
    seen = set()
    for line in text.splitlines():
        name, _, raw = line.partition(":")
        if name not in MEMORY_KEYS:
            continue
        # Linux exposes these values in kB (1024 bytes), not decimal kB.
        match = re.fullmatch(r"\s*([0-9]{1,16})\s+kB\s*", raw)
        value = _count(int(match.group(1)) * 1024) if match and name not in seen else None
        result[MEMORY_KEYS[name]] = value
        seen.add(name)
    for total, available in (("memory_total", "memory_available"), ("swap_total", "swap_free")):
        if result[total] is not None and result[available] is not None and result[available] > result[total]:
            result[total] = result[available] = None
    return result


@dataclass(frozen=True)
class HostHealthAdapter:
    meminfo_path: Path
    disk_paths: Mapping[str, Path]
    read_text: Callable[[Path, int], str] = _bounded_text
    disk_usage: Callable = shutil.disk_usage
    cpu_count: Callable = os.cpu_count

    def __post_init__(self) -> None:
        if any(dim not in {"root", "data"} for dim in self.disk_paths):
            raise ValueError("unexpected disk dimension")

    def collect(self, now: int) -> AdapterResult:
        try:
            memory = parse_meminfo(self.read_text(self.meminfo_path, MAX_PROC_BYTES))
        except (OSError, ValueError, UnicodeError):
            memory = {metric: None for metric in MEMORY_KEYS.values()}
        metrics = [_sample(f"host.{metric}", value, "bytes") for metric, value in memory.items()]
        try:
            cores = _count(self.cpu_count())
            if cores == 0:
                cores = None
        except OSError:
            cores = None
        metrics.append(_sample("host.cpu_cores", cores, "cores"))
        for dim, path in sorted(self.disk_paths.items()):
            try:
                usage = self.disk_usage(path)
                total, available = _count(usage.total), _count(usage.free)
                if total is not None and available is not None and available > total:
                    total = available = None
            except (OSError, ValueError):
                total = available = None
            metrics.extend((_sample("host.disk_total", total, "bytes", dim),
                            _sample("host.disk_available", available, "bytes", dim)))
        partial = any(metric.value is None for metric in metrics)
        return AdapterResult(source="host", observed_at=now, samples=tuple(metrics),
                             status="degraded" if partial else "ok",
                             detail="partial_host" if partial else None)


SYSTEMD_UNIT = re.compile(r"[a-zA-Z0-9_.@:-]{1,100}\.(?:service|timer)\Z")
SAFE_DIM = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
SERVICE_PROPERTIES = ("LoadState", "ActiveState", "Result", "ExecMainCode", "ExecMainStatus",
                      "ExecMainStartTimestamp", "ExecMainExitTimestamp")
TIMER_PROPERTIES = ("LoadState", "ActiveState", "LastTriggerUSec", "NextElapseUSecRealtime")
ACTIVE_STATES = ("active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance")
FAILURE_RESULTS = ("exit-code", "signal", "core-dump", "watchdog", "timeout", "resources",
                   "start-limit-hit", "protocol", "oom-kill", "exec-condition")


def _show_unit(unit: str, properties: tuple[str, ...]) -> dict[str, str]:
    """Read only fixed unit properties, with no journal/environment output."""
    if not SYSTEMD_UNIT.fullmatch(unit) or properties not in (SERVICE_PROPERTIES, TIMER_PROPERTIES):
        raise ValueError("invalid unit request")
    command = ["systemctl", "show", "--no-pager", "--timestamp=unix",
               *(f"--property={prop}" for prop in properties), "--", unit]
    result = subprocess.run(command, capture_output=True, timeout=READ_TIMEOUT_SECONDS,
                            check=True, text=True, encoding="utf-8")
    if len(result.stdout) > MAX_PROC_BYTES:
        raise ValueError("bounded output exceeded")
    values = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in properties and key not in values:
            values[key] = value
    return values


def _systemd_epoch(raw, latest: int | None = None) -> int | None:
    if not isinstance(raw, str) or len(raw) > 64:
        return None
    # --timestamp=unix emits @seconds, sometimes with fractional seconds.
    match = re.fullmatch(r"@([0-9]{1,15})(?:\.[0-9]{1,6})?", raw)
    if match:
        value = _count(int(match.group(1)))
    else:
        try:
            value = int(datetime.strptime(raw, "%a %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp())
        except (ValueError, OverflowError, OSError):
            return None
    return value if value and 0 < value <= 4_102_444_800 and (latest is None or value <= latest) else None


def _unit_active(state) -> int | None:
    return int(state == "active") if state in ACTIVE_STATES else None


@dataclass(frozen=True)
class UnitSpec:
    dimension: str
    service: str
    timer: str | None = None

    def __post_init__(self) -> None:
        if not SAFE_DIM.fullmatch(self.dimension) or not SYSTEMD_UNIT.fullmatch(self.service) or not self.service.endswith(".service"):
            raise ValueError("invalid monitored service")
        if self.timer is not None and (not SYSTEMD_UNIT.fullmatch(self.timer) or not self.timer.endswith(".timer")):
            raise ValueError("invalid monitored timer")


@dataclass(frozen=True)
class SystemdHealthAdapter:
    units: tuple[UnitSpec, ...]
    show: Callable[[str, tuple[str, ...]], dict[str, str]] = _show_unit
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        if not 1 <= len(self.units) <= MAX_SYSTEMD_UNITS or len({unit.dimension for unit in self.units}) != len(self.units):
            raise ValueError("invalid unit inventory")

    def collect(self, now: int) -> AdapterResult:
        deadline = self.clock() + SYSTEMD_BUDGET_SECONDS
        metrics, read_failed, unit_failure = [], False, False

        def read_unit(unit: str, properties: tuple[str, ...]) -> dict[str, str]:
            nonlocal read_failed
            if self.clock() >= deadline:
                read_failed = True
                return {}
            try:
                result = self.show(unit, properties)
                if result.get("LoadState") != "loaded":
                    read_failed = True
                    return {}
                return result
            except (OSError, ValueError, subprocess.SubprocessError):
                read_failed = True
                return {}

        for spec in self.units:
            service = read_unit(spec.service, SERVICE_PROPERTIES)
            active = service.get("ActiveState")
            start = _systemd_epoch(service.get("ExecMainStartTimestamp"), latest=now)
            end = _systemd_epoch(service.get("ExecMainExitTimestamp"), latest=now)
            # Timer services are normally inactive between completed runs.
            # Never treat a running, never-run or missing invocation as exit 0.
            run_success = None
            if active in ("inactive", "failed") and start is not None and end is not None and end >= start:
                if active == "failed" or service.get("Result") in FAILURE_RESULTS:
                    run_success = 0
                elif service.get("Result") == "success" and service.get("ExecMainCode") in ("exited", "1") and service.get("ExecMainStatus") == "0":
                    run_success = 1
            metrics += [_sample("service.active", _unit_active(active), "boolean", spec.dimension),
                        _sample("service.run_success", run_success, "boolean", spec.dimension),
                        _sample("service.last_start_at", start, "epoch_seconds", spec.dimension),
                        _sample("service.last_end_at", end, "epoch_seconds", spec.dimension)]
            if spec.timer:
                timer = read_unit(spec.timer, TIMER_PROPERTIES)
                timer_active = _unit_active(timer.get("ActiveState"))
                if timer_active == 0:
                    unit_failure = True
                if timer_active is None:
                    read_failed = True
                metrics += [_sample("service.timer_active", _unit_active(timer.get("ActiveState")), "boolean", spec.dimension),
                            _sample("service.last_trigger_at", _systemd_epoch(timer.get("LastTriggerUSec"), latest=now), "epoch_seconds", spec.dimension),
                            _sample("service.next_run_at", _systemd_epoch(timer.get("NextElapseUSecRealtime")), "epoch_seconds", spec.dimension)]
            if active not in ACTIVE_STATES:
                read_failed = True
            if active == "failed" or run_success == 0 or (not spec.timer and active == "inactive"):
                unit_failure = True
        status = "degraded" if read_failed or unit_failure else "ok"
        detail = "partial_units" if read_failed else "unit_failure" if unit_failure else None
        return AdapterResult(source="service", observed_at=now, samples=tuple(metrics),
                             status=status, detail=detail)
