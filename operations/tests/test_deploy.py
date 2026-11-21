from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from operations.deploy.render_units import render_bundle
from operations.deploy.runner import (EXPORT_MAX_AGE, consume_export, export_receipts,
                                      exclusive_run, heartbeat_status, run_collection)

NOW = 1_700_006_400
STAMP = datetime.fromtimestamp(NOW, timezone.utc).isoformat()


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.archive = self.path / "archive.json"
        self.backup = self.path / "backup.json"
        self.export = self.path / "export.json"
        self.archive.write_text(json.dumps(dict(schema=1, owner="server-wayback-queue", checked_at=STAMP,
                                                mode="healthy", outcome="failed", ledger_status_counts={"verified":3,"pending":2},
                                                unrelated_private_field="secret-value", last_verified_progress_at=STAMP)))
        self.backup.write_text(json.dumps(dict(schema_version=2, observed_at=STAMP, last_actual_verified_backup_at=STAMP,
                                               task_count_partial=False, failed_pending_task_count=0, pending_task_count=1,
                                               completed_tasks=4, discovery_failures=[], private_field="secret-value")))

    def test_export_strictly_allowlisted_and_freshness(self):
        export_receipts(self.archive,self.backup,self.export,NOW)
        self.assertNotIn("secret-value",self.export.read_text())
        self.assertNotIn(str(self.archive),self.export.read_text())
        if os.name == "posix":
            self.assertEqual(self.export.stat().st_mode & 0o777, 0o640)
        results = {s.source:s for s in consume_export(self.export,NOW+10)}
        self.assertEqual(results["archive"].status,"degraded")
        self.assertEqual(results["archive"].observed_at,NOW+10)
        self.assertEqual(next(m.value for m in results["archive"].samples if m.metric_id == "archive.source_checked_at"),NOW)
        self.assertEqual(next(m.value for m in results["backup"].samples if m.metric_id == "backup.completed_tasks"),4)
        self.assertTrue(all(s.status=="unavailable" and s.detail=="export_stale" and not s.samples for s in consume_export(self.export,NOW+EXPORT_MAX_AGE+1)))

    def test_export_input_alias_prevented(self):
        previous = self.archive.read_bytes()
        with self.assertRaises(ValueError):
            export_receipts(self.archive,self.backup,self.archive,NOW)
        self.assertEqual(self.archive.read_bytes(), previous)

    def test_arbitrary_export_field_rejected_without_receipt_leak(self):
        payload = export_receipts(self.archive,self.backup,self.export,NOW)
        payload["results"][0]["samples"][0]["metric_id"] = "private.secret_value"
        self.export.write_text(json.dumps(payload))
        self.assertTrue(all(s.detail=="export_invalid" and not s.samples for s in consume_export(self.export,NOW)))

    def test_deployed_runner_uses_distinct_store_and_view_and_heartbeat(self):
        state = self.path / "private"
        view = self.path / "view"
        state.mkdir(); view.mkdir()
        meminfo = self.path / "memory.txt"
        meminfo.write_text("MemTotal: 8192 kB\nMemAvailable: 4096 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
        export_receipts(self.archive,self.backup,self.export,NOW)
        args = SimpleNamespace(export=self.export,db=state/"metrics.sqlite",lock=state/"collector.lock",
                               snapshot=view/"snapshot.json",output=view/"dashboard.html",heartbeat=view/"heartbeat.json",
                               meminfo=meminfo,root_disk=self.path,data_disk=self.path,unit=[])
        view_data = run_collection(args,NOW,clock=lambda:NOW+2)
        self.assertEqual({s["source"] for s in view_data["sources"]},{"archive","backup","host"})
        self.assertEqual(json.loads(args.snapshot.read_text()),view_data)
        self.assertTrue(args.output.read_text(encoding="utf8").lstrip().lower().startswith("<!doctype html>"))
        self.assertEqual(json.loads(args.heartbeat.read_text())["completed_at"],NOW+2)
        self.assertFalse((view/"metrics.sqlite").exists())
        self.assertEqual(heartbeat_status(args.heartbeat,NOW+901,900)["status"],"ok")
        self.assertEqual(heartbeat_status(args.heartbeat,NOW+903,900)["status"],"stale")
        self.assertEqual(heartbeat_status(args.heartbeat,NOW,900)["status"],"unavailable")
        # If the root exporter stops, the collector still emits host and
        # marks the stale receipt unknown; never silently reuse a prior count.
        stale = run_collection(args,NOW+300)
        archive = next(s for s in stale["sources"] if s["source"]=="archive")
        self.assertEqual(archive["status"],"unavailable")
        self.assertTrue(all(m["value"] is None for m in archive["metrics"]))
        self.assertEqual(archive["last_success_at"],NOW)

    @unittest.skipUnless(os.name=="posix","production locking uses Linux flock")
    def test_nonoverlapping_run(self):
        lock = self.path/"collector.lock"
        with exclusive_run(lock):
            with self.assertRaises(BlockingIOError):
                with exclusive_run(lock):
                    self.fail("second process entered lock")


class UnitGenerationTests(unittest.TestCase):
    def config(self,**changes):
        return dict(dict(code_dir="/opt/operations",store_dir="/mnt/data/private",view_dir="/mnt/data/view",
                         archive_source="/root/receipts/archive.json",backup_source="/var/lib/private/backup.json",
                         data_disk="/mnt/data",units=["archive,archive.service,archive.timer"]),**changes)

    def test_hardened_singleton_and_no_raw_receipts_for_collector(self):
        bundle = render_bundle(**self.config())
        exp = bundle["swccg-operations-export.service"]
        col = bundle["swccg-operations-collector.service"]
        timer = bundle["swccg-operations-collector.timer"]
        self.assertIn("User=root\n",exp)
        self.assertIn("BindReadOnlyPaths=-/root/receipts/archive.json:",exp)
        self.assertIn("User=swccg-operations\n",col)
        self.assertIn("Wants=swccg-operations-export.service",col)
        self.assertNotIn("/root/receipts/archive.json",col)
        for service in (exp,col):
            self.assertIn("PrivateNetwork=yes",service)
            self.assertIn("ProtectSystem=strict",service)
            self.assertIn("CapabilityBoundingSet=\n",service)
            self.assertIn("MemoryMax=100M",service)
            self.assertIn("StandardOutput=null",service)
            self.assertIn("/usr/bin/python3 -I -B",service)
        self.assertIn("OnCalendar=*-*-* *:0/5:00",timer)
        self.assertIn("Persistent=true",timer)

    def test_invalid_or_overlapping_configuration_rejected(self):
        for changes in ({"view_dir":"/mnt/data/private/view"},{"view_dir":"/tmp/view"},
                        {"code_dir":"/opt/../bad"},{"archive_source":"/root/a\nExecStart=evil"},
                        {"user":"root"},{"units":["archive,archive.timer"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                render_bundle(**self.config(**changes))


if __name__ == "__main__":
    unittest.main()
