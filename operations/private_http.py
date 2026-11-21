"""Private read-only dashboard over an owner-only Unix-domain socket.

Intentionally has no TCP/public bind option or browser authentication. The
interim trust boundary is the approved SSH account and filesystem permissions.
Use an SSH local forward bound to 127.0.0.1 on a trusted administrator PC.
Do not attach a public reverse proxy to this interim listener. A future public
admin route requires a separately verified, strict fresh-staff authorization
gateway that cannot be reached directly by unauthenticated users.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler
import os
from pathlib import Path
import re
import signal
import socketserver
import sqlite3
import stat
import sys
import time
from typing import Callable

from .dashboard import render_dashboard
from .store import Store


MAX_PAGE_BYTES = 512 * 1024
REQUEST_TIMEOUT_SECONDS = 4
LOCAL_HOST = re.compile(r"(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?\Z", re.ASCII)
HEADERS = {
    "Cache-Control": "no-store, private, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Connection": "close",
}


def _error(status: int, message: str) -> tuple[int, str, bytes]:
    return status, "text/plain; charset=utf-8", (message + "\n").encode("utf-8")


class DashboardApplication:
    """Just the aggregate HTML read, callable independently of the listener."""

    def __init__(self, snapshot: Callable[[int], dict], clock: Callable[[], float] = time.time):
        self.snapshot = snapshot
        self.clock = clock

    @staticmethod
    def _single(headers, name: str) -> str | None:
        values = headers.get_all(name, [])
        if len(values) > 1:
            raise ValueError("ambiguous request header")
        return values[0] if values else None

    def handle(self, method: str, path: str, headers) -> tuple[int, str, bytes]:
        if method not in ("GET", "HEAD"):
            return _error(405, "Method not allowed")
        if path != "/":
            return _error(404, "Not found")
        # Refuse nonlocal Host (including browser DNS rebinding) and cross-site
        # browser fetches. These are supplementary, not the SSH trust boundary.
        try:
            host = self._single(headers, "Host")
            origin = self._single(headers, "Origin")
            site = self._single(headers, "Sec-Fetch-Site")
            length = self._single(headers, "Content-Length")
            transfer = self._single(headers, "Transfer-Encoding")
        except ValueError:
            return _error(400, "Bad request")
        if host is None or LOCAL_HOST.fullmatch(host) is None:
            return _error(403, "Not allowed")
        if ":" in host and not 1 <= int(host.rsplit(":", 1)[1]) <= 65535:
            return _error(400, "Bad request")
        if (origin is not None and origin != "http://" + host) or site not in (None, "none", "same-origin"):
            return _error(403, "Not allowed")
        if transfer is not None or length not in (None, "0"):
            return _error(400, "Bad request")
        try:
            # Recompute age against the current clock on EVERY read. Never
            # serve a cached artifact marked fresh when the collector died.
            # Store.snapshot opens an actual read-only SQLite connection.
            body = render_dashboard(self.snapshot(int(self.clock()))).encode("utf-8")
            if len(body) > MAX_PAGE_BYTES:
                raise ValueError("aggregate render exceeded bound")
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return _error(503, "Dashboard data unavailable")
        return 200, "text/html; charset=utf-8", body


class _Handler(BaseHTTPRequestHandler):
    server_version = "OperationsDashboard"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, format, *args):
        # This is a private aggregate view, not another request log. Never log
        # paths, headers, raw identifiers, user agent or exception messages.
        return

    def _respond(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if status == 405:
            self.send_header("Allow", "GET, HEAD")
        for key, value in HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)

    def _read(self):
        self._respond(*self.server.application.handle(self.command, self.path, self.headers))

    do_GET = _read
    do_HEAD = _read

    def _reject(self):
        self._respond(*_error(405, "Method not allowed"))

    do_POST = _reject
    do_PUT = _reject
    do_PATCH = _reject
    do_DELETE = _reject
    do_OPTIONS = _reject
    do_TRACE = _reject
    do_CONNECT = _reject

    def send_error(self, code, message=None, explain=None):
        # Override parser and unsupported-method default error pages so no
        # request text, parser diagnostic, or environment value is reflected.
        self._respond(*_error(405, "Method not allowed") if code == 501 else _error(code, "Bad request"))


_UnixServer = getattr(socketserver, "UnixStreamServer", object)


class PrivateSocketServer(_UnixServer):
    """Single request at a time, private socket, no TCP listener."""

    request_queue_size = 4

    def __init__(self, socket_path: Path, application: DashboardApplication):
        if os.name != "posix" or _UnixServer is object:
            raise ValueError("private Unix listener requires POSIX")
        requested = Path(socket_path)
        if not requested.is_absolute() or requested.name in ("", ".", ".."):
            raise ValueError("absolute private socket path required")
        parent = requested.parent.resolve(strict=True)
        info = parent.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("socket directory must be owner-only")
        path = parent / requested.name
        # Never delete an existing endpoint (including a symlink or stale
        # socket); its operator must explicitly reconcile it first.
        if os.path.lexists(path):
            raise ValueError("socket path already exists")
        self.application = application
        self.socket_path = path
        self.socket_identity = None
        super().__init__(str(path), _Handler, bind_and_activate=False)
        try:
            self.server_bind()
            info = path.lstat()
            self.socket_identity = (info.st_dev, info.st_ino)
            os.chmod(path, 0o600)
            self.server_activate()
        except BaseException:
            self.server_close()
            raise

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(REQUEST_TIMEOUT_SECONDS)
        return connection, address

    def handle_error(self, request, client_address):
        # Do not emit full tracebacks of requests or path/DB details. No
        # secrets are included in error status returned by the application.
        return

    def server_close(self):
        super().server_close()
        if self.socket_identity is None:
            return
        try:
            current = self.socket_path.lstat()
            if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == self.socket_identity:
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.socket_identity = None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="existing aggregate SQLite DB (read only)")
    parser.add_argument("--socket", type=Path, required=True, help="absolute path in existing owner-only directory")
    args = parser.parse_args(argv)
    terminate = None
    try:
        store = Store(args.db)
        with PrivateSocketServer(args.socket, DashboardApplication(store.snapshot)) as server:
            # Graceful systemd/SSH stop releases only our exact socket. For an
            # abrupt crash, let the operator or approved RuntimeDirectory
            # lifecycle reconcile its dedicated directory; never auto-unlink.
            terminate = signal.getsignal(signal.SIGTERM)
            def stop(_sig, _frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, stop)
            server.serve_forever(poll_interval=1)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError):
        print("Private dashboard listener could not start; check approved private socket configuration.", file=sys.stderr)
        return 1
    finally:
        if terminate is not None:
            signal.signal(signal.SIGTERM, terminate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
