import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from operations import cli
from operations.contracts import AdapterResult, Metric
from operations.store import BUCKET_SECONDS, DAY, DAILY_DAYS, RAW_DAYS, ConflictError, Store


START = 1_700_006_400  # UTC midnight, aligned to DAY
assert START % DAY == 0


def result(at, value=1, *, source="example", cursor=None, status="ok", kind="gauge", detail=None):
    samples = () if status == "error" else (Metric("example.count", value, "count", kind=kind),)
    return AdapterResult(source, at, samples, cursor, status, detail)


class ContractTests(unittest.TestCase):
    def test_numeric_zero_and_unknown_distinct(self):
        self.assertEqual(Metric("test.value", 0, "count").value, 0)
        self.assertIsNone(Metric("test.value", None, "count").value)

    def test_invalid_fields_values_and_private_like_inputs_rejected(self):
        cases = [lambda: Metric("https://example.invalid/path?secret=x", 1, "count"),
                 lambda: Metric("requests", 1, "count", "127.0.0.1"),
                 lambda: Metric("requests", 1, "secret_token"),
                 lambda: Metric("requests", float("nan"), "count"),
                 lambda: Metric("requests", float("inf"), "count"),
                 lambda: Metric("requests", -1, "count"),
                 lambda: Metric("requests", 1.5, "count"),
                 lambda: Metric("requests", True, "count"),
                 lambda: AdapterResult("source", START, (), cursor="private/token"),
                 lambda: AdapterResult("source", START, (), detail="private@example.invalid"),
                 lambda: AdapterResult("source", START, (), status="bogus"),
                 lambda: AdapterResult("source", START, (Metric("one", 1, "count"),) * 2),
                 lambda: AdapterResult("source", START, (Metric("one", 1, "count"),), status="error"),
                 lambda: AdapterResult("source", START, (), cursor="1", status="degraded"),
                 lambda: result(START, kind="delta")]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                case()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "metrics.sqlite"
        self.store = Store(self.path)
        self.store.init()

    def test_read_is_noncreating_and_schema_version_enforced(self):
        missing = self.path.with_name("missing.sqlite")
        with self.assertRaises(sqlite3.Error):
            Store(missing).snapshot(START)
        self.assertFalse(missing.exists())
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version=9")
        with self.assertRaises(ValueError):
            self.store.snapshot(START)

    def test_exact_retry_and_conflicting_retry(self):
        record = result(START, 5, cursor="1:20", kind="delta")
        self.assertTrue(self.store.record(record, expected_cursor=None))
        self.assertFalse(self.store.record(record, expected_cursor=None))
        with self.assertRaises(ConflictError):
            self.store.record(result(START, 4, cursor="1:21", kind="delta"), expected_cursor="1:20")
        p = self.store.history("example", "example.count", "all", START, START+BUCKET_SECONDS)["points"][0]
        self.assertEqual(p["value"], 5)
        self.assertEqual(p["observations"], 1)

    def test_atomic_cursor_and_metric_failure_rolls_back_every_change(self):
        self.store.record(result(START, 4, cursor="1:20", kind="delta"), expected_cursor=None)
        bad = AdapterResult("example", START+1, (Metric("another.count", 8, "count"),
                                                  Metric("example.count", 3, "bytes", kind="delta")), "1:30")
        with self.assertRaises(ConflictError):
            self.store.record(bad, expected_cursor="1:20")
        self.assertEqual(self.store.cursor("example"), "1:20")
        snapshot = self.store.snapshot(START+1)["sources"][0]
        self.assertEqual(snapshot["observed_at"], START)
        self.assertEqual([m["metric_id"] for m in snapshot["metrics"]], ["example.count"])
        self.assertNotIn("cursor", snapshot)
        with self.assertRaises(ConflictError):
            self.store.record(result(START+2, 1, cursor="1:40", kind="delta"), expected_cursor="1:30")
        with self.assertRaises(ConflictError):
            self.store.record(result(START+2, 1, cursor="1:20", kind="delta"), expected_cursor="1:20")
        self.assertEqual(self.store.cursor("example"), "1:20")

    def test_delta_sum_and_zero_vs_unknown(self):
        self.store.record(result(START, 0, cursor="1", kind="delta"), expected_cursor=None)
        self.store.record(result(START+1, 4, cursor="2", kind="delta"), expected_cursor="1")
        self.store.record(result(START+2, 2, cursor="3", kind="delta"), expected_cursor="2")
        self.store.record(result(START+BUCKET_SECONDS, 0, cursor="4", kind="delta"), expected_cursor="3")
        self.store.record(result(START+2*BUCKET_SECONDS, None, cursor="5", kind="delta"), expected_cursor="4")
        h = self.store.history("example", "example.count", "all", START, START+4*BUCKET_SECONDS)
        self.assertEqual([p["value"] for p in h["points"]], [6, 0, None])
        self.assertEqual(h["observed_points"], 3)
        self.assertEqual(h["expected_points"], 4)
        self.assertFalse(h["coverage_complete"])

    def test_failed_poll_reports_unknown_keeps_cursor_and_last_success(self):
        self.store.record(result(START, 0, cursor="123", kind="delta"), expected_cursor=None)
        self.store.record(result(START+10, status="error", detail="adapter_error"))
        source = self.store.snapshot(START+10)["sources"][0]
        self.assertEqual(source["status"], "error")
        self.assertEqual(source["last_success_at"], START)
        self.assertIsNone(source["metrics"][0]["value"])
        self.assertEqual(self.store.cursor("example"), "123")
        self.assertTrue(self.store.snapshot(START+901)["sources"][0]["stale"])
        self.store.record(result(START+902, 0, cursor="124", kind="delta"), expected_cursor="123")
        recovered = self.store.snapshot(START+902)["sources"][0]
        self.assertEqual(recovered["status"], "ok")
        self.assertFalse(recovered["stale"])
        self.assertEqual(recovered["metrics"][0]["value"], 0)

    def test_source_freshness_and_intentional_backoff(self):
        self.store.record(result(START, source="backup"))
        self.store.record(result(START, source="archive", status="backoff"))
        views = {s["source"]: s for s in self.store.snapshot(START+1000)["sources"]}
        self.assertFalse(views["archive"]["stale"])
        self.assertFalse(views["backup"]["stale"])
        self.store.record(result(START+1001, source="archive", status="degraded", detail="source_old"))
        archive = next(s for s in self.store.snapshot(START+1001)["sources"] if s["source"] == "archive")
        self.assertTrue(archive["stale"])
        self.assertEqual(archive["last_success_at"], START)
        self.store.record(result(START+1002, source="archive", status="degraded", detail="worker_failed"))
        archive = next(s for s in self.store.snapshot(START+1002)["sources"] if s["source"] == "archive")
        self.assertFalse(archive["stale"])
        self.assertEqual(archive["status"], "degraded")
        self.assertEqual(archive["last_success_at"], START+1002)

    def test_retention_does_not_resurrect_expired_values_or_partial_deltas(self):
        self.store.record(result(START, 7, cursor="1", kind="delta"), expected_cursor=None)
        self.store.record(result(START+BUCKET_SECONDS, 4, cursor="2", kind="delta"), expected_cursor="1")
        self.store.record(result(START+DAY, 8, cursor="3", kind="delta"), expected_cursor="2")
        gauge = AdapterResult("separate", START+3, (Metric("host.count", 5, "count"),))
        self.store.record(gauge)
        day = self.store.history("example", "example.count", "all", START, START+DAY, resolution="daily")["points"][0]
        self.assertIsNone(day["value"])
        self.assertFalse(day["coverage_complete"])
        self.assertEqual(day["observed_buckets"], 2)
        now = START+(RAW_DAYS+1)*DAY
        self.assertEqual(self.store.maintain(now)["rolled_up"], 2)
        self.assertEqual(self.store.maintain(now)["rolled_up"], 0)
        after = self.store.history("example", "example.count", "all", START, START+DAY, resolution="daily")["points"][0]
        self.assertEqual(after, day)
        self.assertIsNone(next(s for s in self.store.snapshot(now)["sources"] if s["source"]=="separate")["metrics"][0]["value"])
        with self.assertRaises(ConflictError):
            self.store.record(result(START+4, source="separate"))
        with self.assertRaises(ConflictError):
            self.store.maintain(now-DAY)
        self.store.maintain(START+(DAILY_DAYS+2)*DAY)
        self.assertEqual(self.store.history("example", "example.count", "all", START, START+DAY, resolution="daily")["points"], [])

    def test_complete_daily_delta_sums_all_buckets_and_rolls_once(self):
        previous = None
        for i in range(DAY//BUCKET_SECONDS):
            nxt = str(i+1)
            self.store.record(result(START+i*BUCKET_SECONDS, 1, cursor=nxt, kind="delta"), expected_cursor=previous)
            previous = nxt
        before = self.store.history("example", "example.count", "all", START, START+DAY, resolution="daily")
        self.assertEqual(before["points"][0]["value"], 96)
        self.assertTrue(before["coverage_complete"])
        self.store.maintain(START+(RAW_DAYS+1)*DAY)
        self.assertEqual(self.store.history("example", "example.count", "all", START, START+DAY, resolution="daily"), before)

    def test_source_cardinality_limit_rollback(self):
        with patch("operations.store.MAX_SERIES_PER_SOURCE", 1):
            with self.assertRaises(ValueError):
                self.store.record(AdapterResult("source", START, (Metric("first", 1, "count"), Metric("second", 1, "count"))))
        self.assertEqual(self.store.snapshot(START)["sources"], [])

    def test_adapter_error_is_sanitized_and_next_source_continues(self):
        class BadAdapter:
            def collect(self, now):
                raise RuntimeError("private@example.invalid 127.0.0.1 secret")

        class GoodAdapter:
            def collect(self, now):
                return result(now, source="good", value=0)

        cli.collect_adapters(self.store, [("bad", BadAdapter()), ("good", GoodAdapter())], START)
        snapshot = self.store.snapshot(START)
        self.assertEqual(snapshot["sources"][0]["detail"], "adapter_error")
        self.assertEqual(snapshot["sources"][1]["metrics"][0]["value"], 0)
        self.assertNotIn("private@example.invalid", json.dumps(snapshot))
        self.assertNotIn("secret", json.dumps(snapshot))

    def test_one_shot_cli_outputs_private_sanitized_snapshot_and_html(self):
        meminfo = self.path.parent / "meminfo"
        meminfo.write_text("MemTotal: 8192 kB\nMemAvailable: 4096 kB\nSwapTotal: 4096 kB\nSwapFree: 2048 kB\n")
        html = self.path.parent / "dashboard.html"
        snapshot = self.path.parent / "snapshot.json"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = cli.main(["run", "--db", str(self.path), "--now", str(START), "--meminfo", str(meminfo),
                             "--root-disk", str(self.path.parent), "--output", str(html), "--snapshot-out", str(snapshot)])
        self.assertEqual(code, 0)
        data = json.loads(snapshot.read_text())
        self.assertEqual(data, json.loads(stdout.getvalue()))
        self.assertEqual(data["sources"][0]["source"], "host")
        self.assertTrue(html.read_text(encoding="utf-8").lstrip().lower().startswith("<!doctype html>"))
        self.assertNotIn("cursor", snapshot.read_text())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["run", "--db", str(self.path), "--now", str(START+1), "--meminfo", str(meminfo),
                                       "--output", str(meminfo)]), 1)
            self.assertEqual(cli.main(["run", "--db", str(meminfo), "--now", str(START+1), "--meminfo", str(meminfo)]), 1)
        self.assertIn("MemTotal", meminfo.read_text())

    def test_ingestion_json_rejects_duplicate_and_overlarge_input(self):
        with self.assertRaises(ValueError):
            cli.read_result(io.BytesIO(b'{"source":"a","source":"b","samples":[]}'))
        with self.assertRaises(ValueError):
            cli.read_result(io.BytesIO(b" "*(cli.MAX_INPUT_BYTES+1)))


if __name__ == "__main__":
    unittest.main()
