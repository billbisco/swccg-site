"""Generate exact, reviewable Linux systemd units; never install or run them."""
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path, PurePosixPath
import re
import sys

from operations.health import MAX_SYSTEMD_UNITS, UnitSpec

NAME = re.compile(r"[a-z][a-z0-9_-]{0,30}\Z")
ABSOLUTE = re.compile(r"/(?:[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)?\Z")
EXPORT_DIR = "/run/swccg-operations-export"
ARCHIVE_INPUT = "/run/swccg-operations-archive-input.json"
BACKUP_INPUT = "/run/swccg-operations-backup-input.json"
LEDGER_INPUT = "/run/swccg-operations-ledger-input.json"
MANPC_INPUT = "/run/swccg-operations-manpc-input.json"


def linux_path(raw, *, allow_root=False):
    if not isinstance(raw, str) or not ABSOLUTE.fullmatch(raw):
        raise ValueError("unit paths must be absolute Linux paths with safe characters")
    path = PurePosixPath(raw)
    if raw != str(path) or ".." in path.parts or "." in path.parts or (raw == "/" and not allow_root):
        raise ValueError("unit paths must be normalized, non-root paths")
    return raw


def overlap(a,b):
    a,b = PurePosixPath(a),PurePosixPath(b)
    return a == b or a in b.parents or b in a.parents


def _name(value):
    if not NAME.fullmatch(value) or value == "root":
        raise ValueError("expected dedicated non-root Unix identity")
    return value


HARDENING = """NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=yes
PrivateDevices=yes
PrivateNetwork=yes
RestrictAddressFamilies=AF_UNIX
ProtectSystem=strict
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
UMask=0077
TasksMax=16
LimitNOFILE=128
MemoryMax=100M
CPUQuota=100%
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=120
StandardOutput=null
StandardError=journal
"""


def render_bundle(*, code_dir, store_dir, view_dir, archive_source, backup_source,
                  data_disk, units, user="swccg-operations", collector_group="swccg-operations", ledger_source=None, manpc_source=None):
    code_dir, store_dir, view_dir, archive_source, backup_source, data_disk = (
        linux_path(v) for v in (code_dir, store_dir, view_dir, archive_source, backup_source, data_disk))
    user, collector_group = _name(user), _name(collector_group)
    ledger_source = linux_path(ledger_source) if ledger_source else None
    manpc_source = linux_path(manpc_source) if manpc_source else None
    paths = (code_dir, store_dir, view_dir, archive_source, backup_source, EXPORT_DIR, ARCHIVE_INPUT, BACKUP_INPUT, LEDGER_INPUT, MANPC_INPUT) + tuple(p for p in (ledger_source,manpc_source) if p)
    ledger_bind = f" -{ledger_source}:{LEDGER_INPUT}" if ledger_source else ""
    ledger_arg = f" --ledger {LEDGER_INPUT}" if ledger_source else ""
    manpc_bind = f" -{manpc_source}:{MANPC_INPUT}" if manpc_source else ""
    manpc_arg = f" --manpc {MANPC_INPUT}" if manpc_source else ""
    if any(overlap(a,b) for i,a in enumerate(paths) for b in paths[i+1:]):
        raise ValueError("code, private state, view, receipt and export paths must not overlap")
    if not (PurePosixPath(data_disk) in PurePosixPath(store_dir).parents and
            PurePosixPath(data_disk) in PurePosixPath(view_dir).parents):
        raise ValueError("private store and view directory must be beneath the explicit data mount")
    specs = tuple(UnitSpec(*text.split(",")) for text in units)
    if (not specs or len(specs) > MAX_SYSTEMD_UNITS or len({s.dimension for s in specs}) != len(specs)
            or sum(7 if s.timer else 4 for s in specs) > 128):
        raise ValueError("provide a bounded distinct unit inventory")
    # The runtime directory has root ownership and collector-group read/traverse
    # access; raw receipts are read only through private per-service bind mounts.
    exporter = f"""[Unit]
Description=SWCCG bounded privileged receipt exporter
RequiresMountsFor={code_dir}

[Service]
Type=oneshot
User=root
Group={collector_group}
RuntimeDirectory=swccg-operations-export
RuntimeDirectoryMode=0750
RuntimeDirectoryPreserve=yes
ProtectHome=tmpfs
BindReadOnlyPaths=-{archive_source}:{ARCHIVE_INPUT} -{backup_source}:{BACKUP_INPUT}{ledger_bind}{manpc_bind}
ReadWritePaths={EXPORT_DIR}
WorkingDirectory={code_dir}
ExecStart=/usr/bin/python3 -I -B {code_dir}/operations/deploy/runner.py export --archive {ARCHIVE_INPUT} --backup {BACKUP_INPUT} --target {EXPORT_DIR}/receipts.json{ledger_arg}{manpc_arg}
{HARDENING}"""
    unit_args = " ".join("--unit " + ",".join(x for x in (s.dimension,s.service,s.timer) if x is not None) for s in specs)
    collector = f"""[Unit]
Description=SWCCG aggregate-only operations collector
Wants=swccg-operations-export.service
After=swccg-operations-export.service
RequiresMountsFor={code_dir} {store_dir} {view_dir} {data_disk}

[Service]
Type=oneshot
User={user}
Group={collector_group}
ProtectHome=yes
ReadOnlyPaths={EXPORT_DIR}
ReadWritePaths={store_dir} {view_dir}
WorkingDirectory={code_dir}
ExecStart=/usr/bin/python3 -I -B {code_dir}/operations/deploy/runner.py collect --export {EXPORT_DIR}/receipts.json --db {store_dir}/metrics.sqlite --lock {store_dir}/collector.lock --snapshot {view_dir}/snapshot.json --output {view_dir}/dashboard.html --heartbeat {view_dir}/heartbeat.json --meminfo /proc/meminfo --root-disk / --data-disk {data_disk} {unit_args}
{HARDENING}"""
    timer = """[Unit]
Description=Collect SWCCG aggregate operations metrics every five minutes

[Timer]
OnCalendar=*-*-* *:0/5:00
AccuracySec=15s
Persistent=true
Unit=swccg-operations-collector.service

[Install]
WantedBy=timers.target
"""
    return {"swccg-operations-export.service": exporter, "swccg-operations-collector.service": collector,
            "swccg-operations-collector.timer": timer}


def main(argv=None):
    p = ArgumentParser(description=__doc__)
    for name in ("code-dir", "store-dir", "view-dir", "archive-source", "backup-source", "data-disk"):
        p.add_argument("--"+name, required=True)
    p.add_argument("--user", default="swccg-operations")
    p.add_argument("--ledger-source")
    p.add_argument("--manpc-source")
    p.add_argument("--collector-group", default="swccg-operations")
    p.add_argument("--unit", action="append", required=True)
    p.add_argument("--output", required=True, type=Path, help="existing staging directory")
    args = p.parse_args(argv)
    try:
        bundle = render_bundle(code_dir=args.code_dir, store_dir=args.store_dir,
                               view_dir=args.view_dir, archive_source=args.archive_source,
                               backup_source=args.backup_source, data_disk=args.data_disk,
                               user=args.user, collector_group=args.collector_group, units=args.unit, ledger_source=args.ledger_source, manpc_source=args.manpc_source)
        # Refuse to overwrite even a staged review file. Installation is a
        # separate explicit operator step after review and systemd validation.
        if not args.output.is_dir() or any((args.output/n).exists() for n in bundle):
            raise ValueError("staging directory missing or unit already exists")
        for name, text in bundle.items():
            with (args.output/name).open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
    except (ValueError, TypeError, OSError):
        print("invalid operations unit configuration or staging output", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
