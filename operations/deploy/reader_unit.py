"""Generate separately approved read-only SSH-tunnel listener service."""
from argparse import ArgumentParser
from pathlib import Path
import sys

from .render_units import EXPORT_DIR, HARDENING, linux_path, overlap, _name

RUNTIME_DIR = "/run/swccg-operations-reader"
UNIT_NAME = "swccg-operations-reader.service"


def render_reader_unit(code_dir, store_dir, reader="swccg-operations-reader",
                       read_group="swccg-operations-read"):
    code_dir, store_dir = linux_path(code_dir), linux_path(store_dir)
    reader, read_group = _name(reader), _name(read_group)
    if reader == "swccg-operations" or read_group == "swccg-operations":
        raise ValueError("reader must use a separate identity and non-writer group")
    paths = (code_dir, store_dir, RUNTIME_DIR, EXPORT_DIR)
    if any(overlap(a,b) for i,a in enumerate(paths) for b in paths[i+1:]):
        raise ValueError("reader store/code/runtime paths overlap")
    return f"""[Unit]
Description=SWCCG private read-only dashboard Unix socket
After=swccg-operations-collector.service
RequiresMountsFor={code_dir} {store_dir}
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User={reader}
Group={read_group}
ProtectHome=yes
RuntimeDirectory=swccg-operations-reader
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=no
InaccessiblePaths=-{EXPORT_DIR}
ReadOnlyPaths={store_dir}
ReadWritePaths={RUNTIME_DIR}
WorkingDirectory={code_dir}
ExecStart=/usr/bin/python3 -I -B {code_dir}/operations/deploy/runner.py serve-private --db {store_dir}/metrics.sqlite --socket {RUNTIME_DIR}/dashboard.sock
Restart=on-failure
RestartSec=10s
TimeoutStopSec=15s
{HARDENING}
[Install]
WantedBy=multi-user.target
"""


def main(argv=None):
    p = ArgumentParser(description=__doc__)
    p.add_argument("--code-dir", required=True)
    p.add_argument("--store-dir", required=True)
    p.add_argument("--reader", default="swccg-operations-reader")
    p.add_argument("--read-group", default="swccg-operations-read")
    p.add_argument("--output", required=True, type=Path, help="existing staging directory")
    args = p.parse_args(argv)
    try:
        text = render_reader_unit(args.code_dir, args.store_dir, args.reader, args.read_group)
        with (args.output / UNIT_NAME).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except (OSError, ValueError):
        print("invalid private listener unit configuration or staging output", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
