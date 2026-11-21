from email.message import Message
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest

from operations.contracts import AdapterResult, Metric
from operations.private_http import DashboardApplication, HEADERS, PrivateSocketServer
from operations.store import Store


def request_headers(**overrides):
    headers = Message()
    values = {"Host": "127.0.0.1:8765", **overrides}
    for key, value in values.items():
        if value is not None:
            headers[key] = value
    return headers


class ReadApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "aggregate.db"
        self.store = Store(self.path)
        self.store.init()
        self.now = 1_700_000_000
        self.store.record(AdapterResult("host", self.now, (Metric("host.cpu_cores", 4, "cores"),)))
        self.app = DashboardApplication(self.store.snapshot, lambda: self.now)

    def test_reads_recompute_freshness_no_db_mutation(self):
        original = self.path.read_bytes()
        first = self.app.handle("GET", "/", request_headers())
        self.assertEqual(200, first[0])
        self.assertIn(b"4 cores", first[2])
        self.assertIn(b'badge good">Observed', first[2])
        self.now += 3600
        second = self.app.handle("GET", "/", request_headers())
        self.assertEqual(200, second[0])
        self.assertIn("Stale · last check observed".encode(), second[2])
        self.assertNotIn(b'badge good">Observed', second[2])
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual("no-store, private, max-age=0", HEADERS["Cache-Control"])

    def test_only_fixed_read_route(self):
        for method, path, status in (("GET", "/snapshot.json", 404), ("GET", "/?metric=arbitrary", 404), ("GET", "/aggregate.db", 404), ("POST", "/", 405), ("DELETE", "/", 405), ("OPTIONS", "/", 405)):
            self.assertEqual(status, self.app.handle(method, path, request_headers())[0])

    def test_rejects_nonlocal_ambiguous_and_cross_site(self):
        bad = [request_headers(Host="other.example"), request_headers(Host=None),
               request_headers(**{"Origin": "http://other.example"}),
               request_headers(**{"Sec-Fetch-Site": "cross-site"}),
               request_headers(Host="127.0.0.1:99999")]
        dupe = request_headers()
        dupe["Host"] = "localhost"
        bad.append(dupe)
        for headers in bad:
            self.assertIn(self.app.handle("GET", "/", headers)[0], (400, 403))

    def test_rejects_request_body(self):
        self.assertEqual(400, self.app.handle("GET", "/", request_headers(**{"Content-Length":"1"}))[0])
        self.assertEqual(400, self.app.handle("GET", "/", request_headers(**{"Transfer-Encoding":"chunked"}))[0])

    def test_gateway_requires_strict_204_decision(self):
        app=DashboardApplication(self.store.snapshot,lambda:self.now,require_gateway_decision=True)
        for value in (None,'200','401','503','204, 204'):
            self.assertEqual(403,app.handle('GET','/',request_headers(**{'X-SWCCG-Staff-Decision':value}))[0])
        self.assertEqual(200,app.handle('GET','/',request_headers(**{'X-SWCCG-Staff-Decision':'204'}))[0])

    def test_missing_db_is_unavailable_not_created(self):
        missing = self.path.with_name("absent.db")
        app = DashboardApplication(Store(missing).snapshot, lambda: self.now)
        status, media, body = app.handle("GET", "/", request_headers())
        self.assertEqual(503, status)
        self.assertEqual(b"Dashboard data unavailable\n", body)
        self.assertNotIn(str(missing).encode(), body)
        self.assertFalse(missing.exists())


@unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"), "private listener is POSIX only")
class PrivateSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "listener.sock"
        self.app = DashboardApplication(lambda now: {"schema_version": 1, "generated_at": now, "sources": []}, lambda: 1_700_000_000)

    def _request(self, data):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(self.path))
            client.sendall(data)
            chunks = []
            while chunk := client.recv(8192):
                chunks.append(chunk)
            return b"".join(chunks)

    @unittest.skipUnless(hasattr(socket,'SO_PEERCRED'),'Linux peer credentials required')
    def test_gateway_requires_exact_group_and_checks_peer(self):
        self.directory.chmod(0o750)
        with PrivateSocketServer(self.path,self.app,gateway_peer_uid=os.geteuid()) as server:
            self.assertEqual(0o660,stat.S_IMODE(self.path.stat().st_mode))
            thread=threading.Thread(target=server.handle_request);thread.start()
            self.assertIn(b'200 OK',self._request(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'));thread.join(5)

    def test_socket_headers_fixed_route_and_cleanup(self):
        with PrivateSocketServer(self.path, self.app) as server:
            self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))
            self.assertEqual(socket.AF_UNIX, server.socket.family)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                response = self._request(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n\r\n")
                self.assertIn(b"200 OK", response)
                self.assertIn(b"Cache-Control: no-store", response)
                self.assertIn(b"Content-Security-Policy:", response)
                self.assertIn(b"Operations overview", response)
                head = self._request(b"HEAD / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                self.assertEqual(b"", head.split(b"\r\n\r\n", 1)[1])
                rejected = self._request(b"POST / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                self.assertIn(b"405 Method Not Allowed", rejected)
                self.assertIn(b"Cache-Control: no-store", rejected)
            finally:
                server.shutdown()
                thread.join(5)
        self.assertFalse(self.path.exists())

    def test_refuses_nonprivate_parent_and_existing_path(self):
        self.directory.chmod(0o755)
        with self.assertRaises(ValueError):
            PrivateSocketServer(self.path, self.app)
        self.directory.chmod(0o700)
        self.path.write_text("must remain", encoding="utf-8")
        with self.assertRaises(ValueError):
            PrivateSocketServer(self.path, self.app)
        self.assertEqual("must remain", self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
