"""Transactional SQLite aggregate store with bounded source and series counts.

Only the collector writes. Readers open read-only SQLite connections. All
adapter validation occurs before a transaction; samples + cursor + freshness
commit as a unit. No service, shell command, network or raw log access here.
"""

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .contracts import (AdapterResult, SCHEMA_VERSION, MAX_SOURCES,
                        MAX_SERIES_PER_SOURCE, check_cursor, check_epoch,
                        check_metric_id, check_slug)

BUCKET_SECONDS = 900
DAY = 86400
RAW_DAYS = 30
DAILY_DAYS = 400
DEFAULT_FRESHNESS = 900
SOURCE_FRESHNESS = {"archive": 1800, "backup": 93600}
MAX_DB_BYTES = 250 * 1024 * 1024
UNSET = object()

SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    source TEXT PRIMARY KEY,
    observed_at INTEGER NOT NULL,
    last_success_at INTEGER,
    status TEXT NOT NULL,
    detail TEXT,
    cursor TEXT,
    digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS series (
    source TEXT NOT NULL REFERENCES sources(source),
    metric_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    kind TEXT NOT NULL,
    unit TEXT NOT NULL,
    PRIMARY KEY(source, metric_id, dimension)
);
CREATE TABLE IF NOT EXISTS samples (
    source TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    bucket INTEGER NOT NULL,
    observed_at INTEGER NOT NULL,
    value REAL,
    observations INTEGER NOT NULL,
    PRIMARY KEY(source, metric_id, dimension, bucket),
    FOREIGN KEY(source, metric_id, dimension) REFERENCES series(source, metric_id, dimension)
);
CREATE INDEX IF NOT EXISTS samples_bucket ON samples(bucket);
CREATE TABLE IF NOT EXISTS daily (
    source TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    day INTEGER NOT NULL,
    observed_at INTEGER NOT NULL,
    value REAL,
    observations INTEGER NOT NULL,
    observed_buckets INTEGER NOT NULL,
    known_buckets INTEGER NOT NULL,
    PRIMARY KEY(source, metric_id, dimension, day),
    FOREIGN KEY(source, metric_id, dimension) REFERENCES series(source, metric_id, dimension)
);
CREATE INDEX IF NOT EXISTS daily_day ON daily(day);
"""


class ConflictError(ValueError):
    """A retry/checkpoint or immutable metric definition conflicted."""


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()

    @contextmanager
    def _connect(self, *, readonly=False):
        # Never create a DB as a side effect of reading it.
        db = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                             uri=True, timeout=3, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if readonly:
                db.execute("PRAGMA query_only=ON")
            else:
                page_size = db.execute("PRAGMA page_size").fetchone()[0]
                max_pages = MAX_DB_BYTES // page_size
                if db.execute("PRAGMA page_count").fetchone()[0] > max_pages:
                    raise ValueError("operations database exceeds storage budget")
                db.execute(f"PRAGMA max_page_count={max_pages}")
            yield db
        finally:
            db.close()

    @staticmethod
    def _require_version(db):
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("uninitialized or unsupported operations schema")

    def init(self):
        # Explicit initialization only; protect a newly created file even
        # under a permissive default umask. Existing permissions are left to
        # the approved deployment's directory/access policy.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError("unsupported operations schema")
            db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA +
                             f"PRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;")

    @contextmanager
    def _transaction(self):
        with self._connect() as db:
            self._require_version(db)
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def cursor(self, source):
        check_slug(source, "source")
        with self._connect(readonly=True) as db:
            self._require_version(db)
            row = db.execute("SELECT cursor FROM sources WHERE source=?", (source,)).fetchone()
            return row[0] if row else None

    def record(self, result, *, expected_cursor=UNSET):
        """Commit a complete source result or raise without advancing state.

        Timestamps strictly increase per source. Equal timestamp is an
        idempotent retry (no writes), while older observations are rejected.
        Cursor-bearing adapters must supply their prior cursor, even if None.
        This guards replay after interruption and competing processes.
        """
        if type(result) is not AdapterResult:
            raise ValueError("expected AdapterResult")
        if expected_cursor is not UNSET:
            check_cursor(expected_cursor)
        if result.cursor is not None and expected_cursor is UNSET:
            raise ValueError("cursor update requires expected_cursor")
        digest = hashlib.sha256(json.dumps(asdict(result), sort_keys=True,
                                           separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        with self._transaction() as db:
            boundary = db.execute("SELECT value FROM maintenance WHERE key='raw_cutoff'").fetchone()
            if boundary is not None and result.observed_at < boundary[0]:
                raise ConflictError("observation predates retention boundary")
            old = db.execute("SELECT * FROM sources WHERE source=?", (result.source,)).fetchone()
            if old is not None:
                if result.observed_at == old["observed_at"]:
                    if digest == old["digest"]:
                        return False
                    raise ConflictError("conflicting result at same source timestamp")
                if result.observed_at < old["observed_at"]:
                    raise ConflictError("out-of-order source observation")
            elif db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] >= MAX_SOURCES:
                raise ValueError("source limit exceeded")
            old_cursor = old["cursor"] if old is not None else None
            if expected_cursor is not UNSET and old_cursor != expected_cursor:
                raise ConflictError("checkpoint conflict; reread source cursor")
            if result.cursor is not None and result.cursor == old_cursor and any(m.kind == "delta" for m in result.samples):
                raise ConflictError("delta checkpoint must advance")
            # This measures successful source observation, not service/provider
            # success. A readable degraded receipt is fresh unless its actual
            # timestamp has exceeded the source-specific threshold.
            read_ok = result.status in {"ok", "backoff"} or (result.status == "degraded" and result.detail != "source_old")
            success = result.observed_at if read_ok else (old["last_success_at"] if old else None)
            cursor = result.cursor if result.cursor is not None else old_cursor
            db.execute("""INSERT INTO sources VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET observed_at=excluded.observed_at,
                last_success_at=excluded.last_success_at,status=excluded.status,
                detail=excluded.detail,cursor=excluded.cursor,digest=excluded.digest""",
                       (result.source, result.observed_at, success, result.status,
                        result.detail, cursor, digest))
            for m in result.samples:
                self._record_metric(db, result, m)
            return True

    @staticmethod
    def _record_metric(db, result, m):
        key = (result.source, m.metric_id, m.dimension)
        existing = db.execute("""SELECT kind,unit FROM series WHERE source=?
                                 AND metric_id=? AND dimension=?""", key).fetchone()
        if existing is None:
            n = db.execute("SELECT COUNT(*) FROM series WHERE source=?", (result.source,)).fetchone()[0]
            if n >= MAX_SERIES_PER_SOURCE:
                raise ValueError("series limit exceeded")
            db.execute("INSERT INTO series VALUES (?,?,?,?,?)", key + (m.kind, m.unit))
        elif tuple(existing) != (m.kind, m.unit):
            raise ConflictError("metric kind or unit changed; use a new metric_id")
        bucket = result.observed_at // BUCKET_SECONDS * BUCKET_SECONDS
        day = bucket // DAY * DAY
        if db.execute("SELECT 1 FROM daily WHERE source=? AND metric_id=? AND dimension=? AND day=?", key+(day,)).fetchone() is not None:
            raise ConflictError("cannot reopen finalized daily history")
        # A delta is an increment for precisely the newly committed source
        # range. A missing value poisons that bucket; never present an unknown
        # or partial sum as complete. Gauges/cumulative counters take latest.
        expression = "CASE WHEN samples.value IS NULL OR excluded.value IS NULL OR samples.value+excluded.value>9007199254740991 THEN NULL ELSE samples.value+excluded.value END" if m.kind == "delta" else "excluded.value"
        db.execute(f"""INSERT INTO samples VALUES (?,?,?,?,?,?,1)
            ON CONFLICT(source,metric_id,dimension,bucket) DO UPDATE SET
            observed_at=excluded.observed_at,value={expression},observations=samples.observations+1""",
                   key + (bucket, result.observed_at, m.value))

    def snapshot(self, now, freshness=None):
        check_epoch(now)
        if freshness is not None and (type(freshness) is not int or not 1 <= freshness <= DAY * 7):
            raise ValueError("invalid freshness threshold")
        with self._connect(readonly=True) as db:
            self._require_version(db)
            db.execute("BEGIN")
            sources = []
            for row in db.execute("SELECT * FROM sources ORDER BY source"):
                source = row["source"]
                metrics = []
                for spec in db.execute("SELECT * FROM series WHERE source=? ORDER BY metric_id,dimension", (source,)):
                    key = (source, spec["metric_id"], spec["dimension"])
                    latest = db.execute("""SELECT observed_at,value FROM samples WHERE source=?
                        AND metric_id=? AND dimension=? ORDER BY bucket DESC LIMIT 1""", key).fetchone()
                    historical = latest is None
                    if historical:
                        latest = db.execute("""SELECT observed_at,NULL AS value FROM daily WHERE source=?
                            AND metric_id=? AND dimension=? ORDER BY day DESC LIMIT 1""", key).fetchone()
                    if latest is not None:
                        value = latest["value"]
                        # Do not present a stale past value as the current
                        # reading when the adapter omitted that series today.
                        if latest["observed_at"] < row["observed_at"]:
                            value = None
                        metrics.append(dict(metric_id=spec["metric_id"], dimension=spec["dimension"],
                                            kind=spec["kind"], unit=spec["unit"], value=value,
                                            observed_at=latest["observed_at"]))
                success = row["last_success_at"]
                threshold = freshness if freshness is not None else SOURCE_FRESHNESS.get(source, DEFAULT_FRESHNESS)
                sources.append(dict(source=source, observed_at=row["observed_at"],
                                    last_success_at=success, status=row["status"], detail=row["detail"],
                                    stale=row["detail"] == "source_old" or success is None or not 0 <= now-success <= threshold,
                                    metrics=metrics))
            db.execute("COMMIT")
        return dict(schema_version=SCHEMA_VERSION, generated_at=now, sources=sources)

    def history(self, source, metric_id, dimension, start, end, *, resolution="bucket"):
        check_slug(source, "source")
        check_metric_id(metric_id)
        check_slug(dimension, "dimension")
        check_epoch(start)
        check_epoch(end)
        if end <= start or resolution not in {"bucket", "daily"}:
            raise ValueError("invalid history range or resolution")
        interval = BUCKET_SECONDS if resolution == "bucket" else DAY
        if start % interval or end % interval:
            raise ValueError("history range must align with its UTC bucket size")
        key = (source, metric_id, dimension)
        with self._connect(readonly=True) as db:
            self._require_version(db)
            db.execute("BEGIN")  # raw-to-daily retention is concurrent-safe
            spec = db.execute("SELECT kind,unit FROM series WHERE source=? AND metric_id=? AND dimension=?", key).fetchone()
            if spec is None:
                return dict(schema_version=SCHEMA_VERSION, points=[], kind=None, unit=None,
                            expected_points=(end-start)//interval, observed_points=0, coverage_complete=False)
            if resolution == "bucket":
                if end-start > DAY * RAW_DAYS:
                    raise ValueError("bucket history exceeds retention window")
                rows = db.execute("""SELECT bucket AS start,value,observations,observed_at FROM samples
                    WHERE source=? AND metric_id=? AND dimension=? AND bucket>=? AND bucket<? ORDER BY bucket""",
                                  key+(start,end))
                points = [dict(r, observed_buckets=1, known_buckets=int(r["value"] is not None),
                               expected_buckets=1, coverage_complete=r["value"] is not None) for r in rows]
            else:
                if end-start > DAY * DAILY_DAYS:
                    raise ValueError("daily history exceeds retention window")
                # Live daily series derive from retained buckets; finalized
                # old days come from daily. Never sum old and new twice.
                days = {r["day"]: dict(r) for r in db.execute("""SELECT day,value,observations,observed_at,observed_buckets,known_buckets FROM daily
                    WHERE source=? AND metric_id=? AND dimension=? AND day>=? AND day<?""", key+(start,end))}
                for day, point in self._rollups(db, key, start, end, spec["kind"]):
                    if day in days:
                        raise ConflictError("raw and finalized history overlap")
                    days[day] = point
                points = [{"start": d, "value": p["value"], "observations": p["observations"],
                           "observed_at": p["observed_at"], "observed_buckets": p["observed_buckets"],
                           "known_buckets": p["known_buckets"], "expected_buckets": DAY//BUCKET_SECONDS,
                           "coverage_complete": p["known_buckets"] == DAY//BUCKET_SECONDS and p["value"] is not None} for d,p in sorted(days.items())]
            expected = (end-start)//interval
            return dict(schema_version=SCHEMA_VERSION, points=points, kind=spec["kind"], unit=spec["unit"],
                        expected_points=expected, observed_points=len(points),
                        coverage_complete=len(points)==expected and all(p["coverage_complete"] for p in points))

    @staticmethod
    def _rollups(db, key, start, end, kind):
        # Only full-day starts are included. Gauge/cumulative last observed;
        # delta sums all present bucket values; null bucket poisons that day.
        rows = db.execute("""SELECT bucket,value,observed_at,observations FROM samples WHERE source=?
            AND metric_id=? AND dimension=? AND bucket>=? AND bucket<? ORDER BY bucket""", key+(start,end))
        points = {}
        for r in rows:
            day = r["bucket"] // DAY * DAY
            p = points.get(day)
            value = r["value"]
            if p is not None and kind == "delta":
                value = None if value is None or p["value"] is None else p["value"] + value
                if value is not None and value > 2**53-1:
                    value = None
            points[day] = dict(day=day, value=value, observed_at=r["observed_at"],
                               observations=r["observations"] + (p["observations"] if p else 0),
                               observed_buckets=1 + (p["observed_buckets"] if p else 0),
                               known_buckets=int(r["value"] is not None) + (p["known_buckets"] if p else 0))
        # A daily delta is not the complete day total unless every bucket is
        # present and known. A gauge remains a last-observed snapshot, with
        # its coverage and timestamp explicitly exposed to the caller.
        if kind == "delta":
            for point in points.values():
                if point["known_buckets"] != DAY//BUCKET_SECONDS:
                    point["value"] = None
        return points.items()

    def maintain(self, now):
        """Finalize expired complete UTC days, prune old buckets and rollups.

        Does not VACUUM nor touch external sources; transaction is atomic.
        Call from the existing non-overlapping collector schedule.
        """
        check_epoch(now)
        midnight = now // DAY * DAY
        raw_cutoff = max(0, midnight - RAW_DAYS*DAY)
        daily_cutoff = max(0, midnight - DAILY_DAYS*DAY)
        with self._transaction() as db:
            previous = db.execute("SELECT value FROM maintenance WHERE key='raw_cutoff'").fetchone()
            if previous is not None and raw_cutoff < previous[0]:
                raise ConflictError("retention boundary cannot move backward")
            if previous is not None and raw_cutoff == previous[0]:
                return dict(rolled_up=0, removed_buckets=0, removed_daily=0)
            rolled = 0
            for spec in db.execute("SELECT * FROM series ORDER BY source,metric_id,dimension"):
                key = (spec["source"], spec["metric_id"], spec["dimension"])
                for day,p in self._rollups(db, key, daily_cutoff, raw_cutoff, spec["kind"]):
                    db.execute("INSERT INTO daily VALUES (?,?,?,?,?,?,?,?,?)", key+(day,p["observed_at"],p["value"],p["observations"],p["observed_buckets"],p["known_buckets"]))
                    rolled += 1
            removed = db.execute("DELETE FROM samples WHERE bucket<?", (raw_cutoff,)).rowcount
            old_daily = db.execute("DELETE FROM daily WHERE day<?", (daily_cutoff,)).rowcount
            db.execute("INSERT INTO maintenance VALUES ('raw_cutoff',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (raw_cutoff,))
        return dict(rolled_up=rolled, removed_buckets=removed, removed_daily=old_daily)
