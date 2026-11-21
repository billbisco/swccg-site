"""Fixed one-shot exporter/collector entry point for the hardened Linux units.

The root-only process reads precisely two owner receipts and publishes a
bounded aggregate envelope; its unprivileged consumer cannot read those
receipts or other root state. No command sends data across a network.
"""
from __future__ import annotations

from argparse import ArgumentParser
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

# The generated units invoke this exact root-owned script with Python -I -B.
# -I excludes ambient user paths; only the installed repository supplies code.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from operations.cli import collect_adapters
from operations.contracts import AdapterResult, Metric, check_epoch
from operations.dashboard import render_dashboard
from operations.health import (ARCHIVE_SPECS, BACKUP_SPECS, ArchiveHealthAdapter,
                               BackupHealthAdapter, HostHealthAdapter,
                               SystemdHealthAdapter, UnitSpec, read_receipt)
from operations.store import Store
from operations.live_metrics import SECURITY_SPECS,PLAYER_SPECS,AUDIENCE_SPECS,project_network,forum_projection,site_projection

EXPORT_SCHEMA = 1
EXPORT_MAX_AGE = 180
SPECS = {"archive": ARCHIVE_SPECS, "backup": BACKUP_SPECS,"security":SECURITY_SPECS,"player":PLAYER_SPECS,"audience":AUDIENCE_SPECS}
REQUIRED_SOURCES={'archive','backup'}
SAFE_DETAILS = frozenset({None, "missing_receipt", "invalid_receipt", "source_old",
                          "partial_receipt", "provider_backoff", "provider_blocked",
                          "worker_failed", "worker_paused", "pending_failures",
                          "discovery_failed", "adapter_error","source_limit","source_changed"})


def private_write(path: Path, text: str, mode: int = 0o640):
    """Atomic replacement; temporary file is private until data is complete."""
    path = Path(path)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=".ops-", delete=False) as stream:
            pending = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(pending, mode)
        os.replace(pending, path)
        # Flush the directory entry on Linux before announcing completion.
        if os.name == "posix":
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def _validate_result(result: AdapterResult, now: int) -> AdapterResult:
    if (type(result) is not AdapterResult or result.source not in SPECS
            or result.observed_at != now or result.cursor is not None
            or result.detail not in SAFE_DETAILS):
        raise ValueError("invalid aggregate export result")
    expected = set(SPECS[result.source])
    actual = {(x.metric_id, x.unit, x.dimension) for x in result.samples}
    if (any(x.kind != "gauge" for x in result.samples)
            or not actual.issubset(expected)
            or (result.status in {"ok", "degraded", "backoff"} and actual != expected)):
        raise ValueError("export result violates fixed receipt field allowlist")
    return result


def _safe_collect(adapter, source, now):
    try:
        result = _validate_result(adapter.collect(now), now)
        if result.source != source:
            raise ValueError("unexpected source")
        return result
    except Exception:
        return AdapterResult(source, now, (), status="error", detail="adapter_error")


def export_receipts(archive: Path, backup: Path, target: Path, now: int, ledger: Path | None = None, manpc: Path | None = None, network: Path | None = None, forum: Path | None = None, forum_previous: Path | None = None, site_audience: Path | None = None):
    check_epoch(now)
    if any(_aliases(target, p) for p in (archive, backup, ledger, manpc,network,forum,forum_previous,site_audience) if p is not None):
        raise ValueError("aggregate export may not overwrite an input receipt")
    results = [_safe_collect(ArchiveHealthAdapter(archive, ledger=ledger, manpc=manpc), "archive", now),
               _safe_collect(BackupHealthAdapter(backup), "backup", now)]
    if network is not None:
        results += [_validate_result(project_network(network,source,now),now) for source in ('security','player')]
    if forum is not None and forum_previous is not None:results.append(_validate_result(site_projection(forum_projection(forum,forum_previous,now),site_audience,now),now))
    payload = dict(schema_version=EXPORT_SCHEMA, exported_at=now,
                   results=[asdict(x) for x in results])
    private_write(target, json.dumps(payload, allow_nan=False, separators=(",", ":")) + "\n")
    return payload


def _unavailable_export(now, detail):
    return tuple(AdapterResult(source, now, (), status="unavailable", detail=detail) for source in SPECS)


def consume_export(path: Path, now: int):
    """Accept only an exact current exporter envelope; fail closed on extras."""
    check_epoch(now)
    try:
        data = read_receipt(path)
        if set(data) != {"schema_version", "exported_at", "results"} or type(data["schema_version"]) is not int or data["schema_version"] != EXPORT_SCHEMA:
            raise ValueError("invalid export envelope")
        exported_at = data["exported_at"]
        check_epoch(exported_at)
        if not 0 <= now - exported_at <= EXPORT_MAX_AGE:
            return _unavailable_export(now, "export_stale")
        rows = data["results"]
        if type(rows) is not list or not len(REQUIRED_SOURCES)<=len(rows)<=len(SPECS):
            raise ValueError("invalid export source count")
        results = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"source", "observed_at", "samples", "cursor", "status", "detail"}:
                raise ValueError("invalid result fields")
            if type(row["samples"]) is not list or len(row["samples"]) > 128:
                raise ValueError("invalid sample list")
            samples = []
            for sample in row["samples"]:
                if not isinstance(sample, dict) or set(sample) != {"metric_id", "value", "unit", "dimension", "kind"}:
                    raise ValueError("invalid sample fields")
                samples.append(Metric(**sample))
            result = _validate_result(AdapterResult(**dict(row, samples=tuple(samples))), exported_at)
            # The collector transaction gets the current poll time. The source
            # receipt time and verified progress remain untouched as metrics.
            results.append(AdapterResult(result.source, now, result.samples,
                                         status=result.status, detail=result.detail))
        if not REQUIRED_SOURCES <= {x.source for x in results} or len({x.source for x in results})!=len(results):
            raise ValueError("export source duplication")
        return tuple(results)
    except FileNotFoundError:
        return _unavailable_export(now, "export_missing")
    except (OSError, ValueError, TypeError, RecursionError):
        return _unavailable_export(now, "export_invalid")


def _aliases(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    return a == b or (a.exists() and b.exists() and a.samefile(b))


@contextmanager
def exclusive_run(lock: Path):
    """Prevent overlap beyond systemd's normal singleton service semantics."""
    if os.name != "posix":
        # Production deployment is Linux only. Windows fixture execution never
        # exercises scheduling; no cross-process safety promise on that host.
        yield
        return
    import fcntl
    fd = os.open(lock, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def run_collection(args, now: int, clock=None):
    check_epoch(now)
    inputs = (args.export, args.meminfo)
    outputs = (args.db, args.snapshot, args.output, args.heartbeat, args.lock)
    if any(_aliases(a,b) for a in outputs for b in inputs) or any(
            _aliases(a,b) for i,a in enumerate(outputs) for b in outputs[i+1:]):
        raise ValueError("collector input/output collision")
    with exclusive_run(args.lock):
        store = Store(args.db)
        store.init()
        store.maintain(now)
        for result in consume_export(args.export, now):
            store.record(result)
        units = tuple(UnitSpec(*text.split(",")) for text in args.unit)
        adapters = [("host", HostHealthAdapter(args.meminfo, {"root": args.root_disk, "data": args.data_disk}))]
        if units:
            adapters.append(("service", SystemdHealthAdapter(units)))
        collect_adapters(store, adapters, now)
        snapshot = store.snapshot(now)
        private_write(args.snapshot, json.dumps(snapshot, allow_nan=False, separators=(",", ":")) + "\n")
        private_write(args.output, render_dashboard(snapshot))
        # Collector-level completion heartbeat is deliberately separate from
        # source-level outcome. An independent watchdog can check its age.
        completed_at = now if clock is None else int(clock())
        check_epoch(completed_at)
        if completed_at < now:
            raise ValueError("collector clock moved backward")
        private_write(args.heartbeat, json.dumps(dict(schema_version=1, completed_at=completed_at)) + "\n")
        return snapshot


def heartbeat_status(path, now, max_age):
    check_epoch(now)
    if type(max_age) is not int or not 1 <= max_age <= 86_400:
        raise ValueError("invalid heartbeat maximum age")
    try:
        data = read_receipt(path)
        if (set(data) != {"schema_version", "completed_at"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1):
            raise ValueError("invalid heartbeat schema")
        completed = data["completed_at"]
        check_epoch(completed)
        if completed > now:
            raise ValueError("heartbeat from future")
        return dict(status="ok" if now-completed <= max_age else "stale", completed_at=completed, age_seconds=now-completed)
    except (OSError, ValueError, TypeError, RecursionError):
        return dict(status="unavailable", completed_at=None, age_seconds=None)


def parser():
    p = ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="mode", required=True)
    export = sub.add_parser("export")
    for name in ("archive", "backup", "target"):
        export.add_argument("--"+name, required=True, type=Path)
    export.add_argument("--ledger", type=Path)
    export.add_argument("--manpc", type=Path)
    export.add_argument("--network",type=Path)
    export.add_argument("--forum",type=Path)
    export.add_argument("--forum-previous",type=Path)
    export.add_argument('--site-audience',type=Path)
    collect = sub.add_parser("collect")
    for name in ("export", "db", "snapshot", "output", "heartbeat", "lock", "meminfo", "root-disk", "data-disk"):
        collect.add_argument("--"+name, required=True, type=Path)
    collect.add_argument("--unit", action="append", default=[])
    check = sub.add_parser("check-heartbeat", help="independent read-only collector freshness check")
    check.add_argument("--heartbeat", required=True, type=Path)
    check.add_argument("--max-age", type=int, default=900)
    serve = sub.add_parser("serve-private", help="read-only owner-only Unix socket listener")
    serve.add_argument("--db", required=True, type=Path)
    serve.add_argument("--socket", required=True, type=Path)
    serve.add_argument('--gateway-peer-uid',type=int)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    now = int(time.time())
    try:
        if args.mode == "export":
            export_receipts(args.archive, args.backup, args.target, now, args.ledger, args.manpc,args.network,args.forum,args.forum_previous,args.site_audience)
        elif args.mode == "check-heartbeat":
            status = heartbeat_status(args.heartbeat, now, args.max_age)
            print(json.dumps(status))
            return 0 if status["status"] == "ok" else 1
        elif args.mode == "serve-private":
            from operations.private_http import main as serve
            options=["--db", str(args.db), "--socket", str(args.socket)]
            if args.gateway_peer_uid is not None:options+=['--gateway-peer-uid',str(args.gateway_peer_uid)]
            return serve(options)
        else:
            run_collection(args, now, clock=time.time)
    except (OSError, ValueError, TypeError, sqlite3.Error):
        print("operations deployment command failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
