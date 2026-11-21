"""Small, immutable aggregate-only boundary between adapters and storage.

Adapters must never include raw client identifiers, URLs, log lines, secrets or
arbitrary labels. Identifiers are static machine labels from adapter code.
"""

from dataclasses import dataclass
import math
import re

SCHEMA_VERSION = 1
MAX_SOURCES = 32
MAX_SERIES_PER_SOURCE = 128
MAX_SAMPLES = 128
UNITS = frozenset({"count", "bytes", "seconds", "milliseconds", "percent",
                   "ratio", "cores", "boolean", "epoch_seconds"})
KINDS = frozenset({"gauge", "delta", "cumulative"})
STATUSES = frozenset({"ok", "degraded", "error", "unknown", "unavailable", "backoff"})
SLUG = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z", re.ASCII)
METRIC = re.compile(r"[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){0,2}\Z", re.ASCII)
CURSOR = re.compile(r"[0-9]{1,20}(?::[0-9]{1,20}){0,4}\Z", re.ASCII)


def _matches(pattern, value, field):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"invalid {field}")


def check_epoch(value):
    # SQLite integer bounds and an intentionally limited UTC epoch domain.
    if type(value) is not int or not 0 <= value <= 4_102_444_800:
        raise ValueError("invalid UTC epoch")


def check_slug(value, field="slug"):
    _matches(SLUG, value, field)


def check_metric_id(value):
    _matches(METRIC, value, "metric_id")


def check_cursor(value):
    if value is not None:
        _matches(CURSOR, value, "cursor (numeric checkpoint only)")


@dataclass(frozen=True)
class Metric:
    metric_id: str
    value: int | float | None
    unit: str
    dimension: str = "all"
    kind: str = "gauge"

    def __post_init__(self):
        check_metric_id(self.metric_id)
        check_slug(self.dimension, "dimension")
        if not isinstance(self.unit, str) or not isinstance(self.kind, str) or self.unit not in UNITS or self.kind not in KINDS:
            raise ValueError("unsupported unit or metric kind")
        v = self.value
        if v is not None:
            if type(v) not in (int, float) or not 0 <= v <= 2**53 - 1 or not math.isfinite(v):
                raise ValueError("metric value must be a finite nonnegative number or null")
            if self.unit == "boolean" and v not in (0, 1):
                raise ValueError("boolean metric must be zero or one")
            if self.unit in ("count", "bytes", "epoch_seconds") and int(v) != v:
                raise ValueError("integer unit requires integral value")
            if self.unit == "epoch_seconds":
                check_epoch(int(v))
            if self.unit == "percent" and v > 100:
                raise ValueError("percent metric is out of range")
            if self.unit == "ratio" and v > 1:
                raise ValueError("ratio metric is out of range")


@dataclass(frozen=True)
class AdapterResult:
    source: str
    observed_at: int
    samples: tuple[Metric, ...]
    cursor: str | None = None
    status: str = "ok"
    detail: str | None = None

    def __post_init__(self):
        check_slug(self.source, "source")
        check_epoch(self.observed_at)
        check_cursor(self.cursor)
        if not isinstance(self.status, str) or self.status not in STATUSES:
            raise ValueError("unsupported source status")
        if self.detail is not None:
            check_slug(self.detail, "detail")
        if type(self.samples) is not tuple or len(self.samples) > MAX_SAMPLES:
            raise ValueError("samples must be a bounded tuple")
        if any(type(s) is not Metric for s in self.samples):
            raise ValueError("invalid sample")
        keys = {(s.metric_id, s.dimension) for s in self.samples}
        if len(keys) != len(self.samples):
            raise ValueError("duplicate metric series in snapshot")
        if self.status not in {"ok", "degraded", "backoff"} and (self.samples or self.cursor is not None):
            raise ValueError("failed source cannot emit metrics or advance cursor")
        if self.status != "ok" and self.cursor is not None:
            raise ValueError("only an error-free collection can advance its cursor")
        if any(s.kind == "delta" for s in self.samples) and self.cursor is None:
            raise ValueError("delta samples require a committed numeric source checkpoint")
