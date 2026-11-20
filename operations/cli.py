"""One-shot operations collection/export CLI. No scheduling or external traffic."""

from argparse import ArgumentParser
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

from .contracts import AdapterResult, Metric
from .store import Store, UNSET

MAX_INPUT_BYTES = 64 * 1024


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, indent=2) + "\n"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_result(stream):
    raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds aggregate snapshot limit")
    data = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
        raise ValueError("expected aggregate snapshot object")
    data["samples"] = tuple(Metric(**sample) for sample in data["samples"])
    return AdapterResult(**data)


def atomic_write(path, value):
    """Replace an explicitly selected output with a complete private artifact."""
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                     prefix=".operations-", delete=False) as stream:
        pending = Path(stream.name)
        try:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            pending.unlink(missing_ok=True)
            raise
    try:
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def collect_adapters(store, adapters, now):
    """Isolate adapter read errors without persisting raw exception messages.

    Storage errors propagate: failed commits must not be disguised as successful
    collection. Adapters taking log cursors use their own expected_cursor path.
    """
    for source, adapter in adapters:
        try:
            result = adapter.collect(now)
            if type(result) is not AdapterResult or result.source != source or result.observed_at != now:
                raise ValueError("adapter returned unexpected source or collection timestamp")
        except Exception:
            result = AdapterResult(source, now, (), status="error", detail="adapter_error")
        store.record(result)


def _unit(text):
    from .health import UnitSpec
    parts = text.split(",")
    if len(parts) not in (2, 3):
        raise ValueError("unit specification must be dimension,service[,timer]")
    return UnitSpec(*parts)


def configured_adapters(args):
    from .health import ArchiveHealthAdapter, BackupHealthAdapter, HostHealthAdapter, SystemdHealthAdapter
    adapters = []
    if args.archive_health:
        adapters.append(("archive", ArchiveHealthAdapter(args.archive_health)))
    if args.backup_health:
        adapters.append(("backup", BackupHealthAdapter(args.backup_health)))
    disks = {name: path for name, path in (("root", args.root_disk), ("data", args.data_disk)) if path is not None}
    if args.meminfo:
        adapters.append(("host", HostHealthAdapter(args.meminfo, disks)))
    elif disks:
        raise ValueError("disk paths require an explicitly configured memory source")
    if args.unit:
        adapters.append(("service", SystemdHealthAdapter(tuple(_unit(x) for x in args.unit))))
    if not adapters:
        raise ValueError("configure at least one source")
    return adapters


def _validate_outputs(args):
    db = Path(args.db).resolve()
    source_paths = {Path(x).resolve() for x in (args.archive_health, args.backup_health, args.meminfo)
                    if x is not None}
    inputs = source_paths | {db}
    outputs = [Path(x).resolve() for x in (args.output, args.snapshot_out) if x is not None]
    def aliases(a, b):
        return a == b or (a.exists() and b.exists() and a.samefile(b))
    if (any(aliases(db, p) for p in source_paths) or
            any(aliases(a, b) for a in outputs for b in inputs) or
            any(aliases(a, b) for n,a in enumerate(outputs) for b in outputs[n+1:])):
        raise ValueError("output files must be distinct from database and input receipts")


def parser():
    root = ArgumentParser(description=__doc__)
    subs = root.add_subparsers(dest="command", required=True)
    for name in ("init", "record", "snapshot", "history", "maintain", "run"):
        p = subs.add_parser(name)
        p.add_argument("--db", type=Path, required=True)
        if name in {"snapshot", "maintain", "run"}:
            p.add_argument("--now", type=int, help="UTC epoch seconds (defaults to current time)")
        if name == "record":
            p.add_argument("--expected-cursor", type=str, default=UNSET,
                           help="prior numeric checkpoint, or an empty string for initial NULL")
        if name == "snapshot":
            p.add_argument("--freshness", type=int)
        if name == "history":
            p.add_argument("--source", required=True)
            p.add_argument("--metric", required=True)
            p.add_argument("--dimension", default="all")
            p.add_argument("--start", type=int, required=True)
            p.add_argument("--end", type=int, required=True)
            p.add_argument("--resolution", choices=("bucket", "daily"), default="bucket")
        if name == "run":
            p.add_argument("--archive-health", type=Path)
            p.add_argument("--backup-health", type=Path)
            p.add_argument("--meminfo", type=Path)
            p.add_argument("--root-disk", type=Path)
            p.add_argument("--data-disk", type=Path)
            p.add_argument("--unit", action="append", default=[], help="dimension,service[,timer]")
            p.add_argument("--snapshot-out", type=Path)
            p.add_argument("--output", type=Path, help="private HTML snapshot output; stdout remains aggregate JSON")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    store = Store(args.db)
    now = getattr(args, "now", None)
    if now is None:
        now = int(time.time())
    try:
        if args.command == "init":
            store.init()
            return 0
        if args.command == "record":
            result = read_result(sys.stdin.buffer)
            cursor = None if args.expected_cursor == "" else args.expected_cursor
            print(_json({"committed": store.record(result, expected_cursor=cursor)}), end="")
        elif args.command == "snapshot":
            print(_json(store.snapshot(now, args.freshness)), end="")
        elif args.command == "history":
            print(_json(store.history(args.source, args.metric, args.dimension, args.start, args.end,
                                      resolution=args.resolution)), end="")
        elif args.command == "maintain":
            print(_json(store.maintain(now)), end="")
        elif args.command == "run":
            _validate_outputs(args)
            adapters = configured_adapters(args)
            store.init()
            store.maintain(now)
            collect_adapters(store, adapters, now)
            snapshot = store.snapshot(now)
            encoded = _json(snapshot)
            if args.snapshot_out:
                atomic_write(args.snapshot_out, encoded)
            if args.output:
                from .dashboard import render_dashboard
                atomic_write(args.output, render_dashboard(snapshot))
            print(encoded, end="")
    except (ValueError, TypeError, OSError, sqlite3.Error):
        # Never emit source content, cursor, path or exception args. The
        # command's status is separate from each source's explicit status.
        print("operations command failed; check configuration and source-independent diagnostics", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
