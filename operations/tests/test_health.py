import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from operations.health import (
    ArchiveHealthAdapter, BackupHealthAdapter, HostHealthAdapter, SystemdHealthAdapter,
    UnitSpec, SERVICE_PROPERTIES, TIMER_PROPERTIES, MAX_RECEIPT_BYTES, _show_unit,
    parse_archive_health, parse_backup_health, parse_meminfo, read_receipt,
)


NOW = 1_700_000_000  # 2023-11-14 22:13:20 UTC, synthetic source fixtures only.
CHECKED = "2023-11-14T22:13:00Z"
EARLIER = "2023-11-14T22:12:00.123456+00:00"


def samples(result):
    return {(m.metric_id, m.dimension): m.value for m in result.samples}


def value(result, name, dim="all"):
    return samples(result)[name, dim]


def archive_fixture(**overrides):
    data = {
        "schema": 1, "checked_at": CHECKED, "owner": "server-wayback-queue",
        "mode": "healthy", "outcome": "completed", "ledger_status_counts": {"verified": 8, "pending": 2},
        "last_verified_progress_at": EARLIER, "last_submission_attempt_at": EARLIER,
        "next_eligible_attempt_at": None, "awaiting_canary_verification": False,
    }
    data.update(overrides)
    return data


def backup_fixture(**overrides):
    data = {"schema_version": 2, "observed_at": CHECKED, "last_actual_verified_backup_at": EARLIER,
            "completed_tasks": 2, "pending_task_count": 3, "failed_pending_task_count": 0,
            "task_count_partial": False, "discovery_failures": []}
    data.update(overrides)
    return data


class ArchiveCensusTests(unittest.TestCase):
    def test_window_failure_and_manpc_counts_and_mismatch(self):
        ledger = {
            "private_url_1": {"status": "verified", "verify": {"at": EARLIER, "lane": "manpc"}},
            "private_url_2": {"status": "failed", "last": "2023-11-01T00:00:00Z", "error": "connection refused"},
            "private_url_3": {"status": "failed", "error": "401"},
            "private_url_4": {"status": "unverified", "capture": "private", "verify": {"defer": "manpc"}},
        }
        fixture = archive_fixture(ledger_status_counts={"verified": 1, "failed": 2, "unverified": 1}, reason="provider unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ledger.json"
            p.write_text(json.dumps(ledger))
            result = parse_archive_health(fixture, NOW, 600, p)
            for name, count in {"verified_24h": 1, "verified_7d": 1, "failed_transient": 1, "failed_due": 1, "failed_other": 1, "verified_manpc": 1, "manpc_candidates": 1, "manpc_deferred": 1, "reason_provider_unavailable": 1}.items():
                self.assertEqual(value(result, "archive." + name), count, name)
            self.assertNotIn("private_url", str(result))
            fixture["ledger_status_counts"]["failed"] = 3
            self.assertIsNone(value(parse_archive_health(fixture, NOW, 600, p), "archive.verified_24h"))
            fixture["ledger_status_counts"]["failed"] = 2
            ledger["private_url_1"]["verify"].pop("at")
            p.write_text(json.dumps(ledger))
            result = parse_archive_health(fixture, NOW, 600, p)
            self.assertEqual(value(result, "archive.verified_7d"), 0)
            self.assertEqual(value(result, "archive.verified_undated"), 1)


class ArchiveHealthTest(unittest.TestCase):
    def test_process_failed_with_healthy_provider_and_verified_progress(self):
        data = archive_fixture(outcome="failed", reason="untrusted diagnostic with private marker")
        data["ledger_status_counts"]["unrecognized label"] = 3
        result = parse_archive_health(data, NOW, 30)
        self.assertEqual(("degraded", "worker_failed"), (result.status, result.detail))
        self.assertEqual(1, value(result, "archive.mode_healthy"))
        self.assertEqual(0, value(result, "archive.run_success"))
        self.assertEqual(8, value(result, "archive.verified"))
        self.assertEqual(0, value(result, "archive.failed"))
        self.assertEqual(3, value(result, "archive.unknown"))
        self.assertEqual(NOW - 80, value(result, "archive.last_verified_at"))
        self.assertNotIn("private marker", repr(result))
        self.assertNotIn("unrecognized label", repr(result))

    def test_backoff_and_auth_block_are_not_worker_success(self):
        result = parse_archive_health(archive_fixture(mode="open", outcome="paused", next_eligible_attempt_at="2023-11-14T23:00:00Z"), NOW, 30)
        self.assertEqual("backoff", result.status)
        self.assertEqual(1, value(result, "archive.mode_open"))
        self.assertEqual(1, value(result, "archive.run_paused"))
        self.assertEqual(0, value(result, "archive.run_success"))
        self.assertGreater(value(result, "archive.next_eligible_at"), NOW)
        blocked = parse_archive_health(archive_fixture(mode="auth_blocked", outcome="paused"), NOW, 30)
        self.assertEqual(("degraded", "provider_blocked"), (blocked.status, blocked.detail))
        self.assertIsNone(value(blocked, "archive.next_eligible_at"))

    def test_stale_source_time_distinct_from_collection_time(self):
        result = parse_archive_health(archive_fixture(), NOW, 10)
        self.assertEqual(("degraded", "source_old"), (result.status, result.detail))
        self.assertEqual(NOW, result.observed_at)
        self.assertEqual(NOW - 20, value(result, "archive.source_checked_at"))

    def test_invalid_or_missing_count_map_is_not_zero(self):
        for counts in (None, {"verified": True}, {"verified": -2}, {"verified": 2.5}, {"verified": 2**53}):
            with self.subTest(counts=counts):
                result = parse_archive_health(archive_fixture(ledger_status_counts=counts), NOW, 30)
                self.assertEqual("degraded", result.status)
                self.assertIsNone(value(result, "archive.verified"))
                self.assertIsNone(value(result, "archive.pending"))

    def test_invalid_mode_or_source_timestamp(self):
        result = parse_archive_health(archive_fixture(mode="diagnostic-data"), NOW, 30)
        self.assertIsNone(value(result, "archive.mode_healthy"))
        self.assertNotIn("diagnostic-data", repr(result))
        for timestamp in (None, "2023-11-14T22:13:00", "2023-11-14T23:00:00Z", "not-a-timestamp"):
            with self.subTest(timestamp=timestamp):
                result = parse_archive_health(archive_fixture(checked_at=timestamp), NOW, 30)
                self.assertEqual("error", result.status)
                self.assertEqual((), result.samples)

    def test_missing_file_and_unreadable_file_have_no_metrics(self):
        missing = ArchiveHealthAdapter(Path("fixture"), read=lambda path: self.raise_io(FileNotFoundError())).collect(NOW)
        unreadable = ArchiveHealthAdapter(Path("fixture"), read=lambda path: self.raise_io(PermissionError("private path marker"))).collect(NOW)
        self.assertEqual(("unavailable", "missing_receipt", ()), (missing.status, missing.detail, missing.samples))
        self.assertEqual(("error", "invalid_receipt", ()), (unreadable.status, unreadable.detail, unreadable.samples))
        self.assertNotIn("private path marker", repr(unreadable))

    @staticmethod
    def raise_io(error):
        raise error


class BackupHealthTest(unittest.TestCase):
    def test_partial_coverage_never_makes_lower_bound_a_total(self):
        result = parse_backup_health(backup_fixture(task_count_partial=True, failed_pending_task_count=4), NOW, 30)
        self.assertEqual(("degraded", "partial_receipt"), (result.status, result.detail))
        self.assertIsNone(value(result, "backup.failed_pending_tasks"))
        self.assertIsNone(value(result, "backup.pending_tasks"))
        self.assertEqual(2, value(result, "backup.completed_tasks"))
        self.assertEqual(1, value(result, "backup.coverage_partial"))
        self.assertEqual(NOW - 80, value(result, "backup.last_verified_at"))

    def test_real_verified_progress_with_remaining_failures(self):
        result = parse_backup_health(backup_fixture(failed_pending_task_count=2), NOW, 30)
        self.assertEqual(("degraded", "pending_failures"), (result.status, result.detail))
        self.assertEqual(2, value(result, "backup.failed_pending_tasks"))
        self.assertEqual(NOW - 80, value(result, "backup.last_verified_at"))

    def test_discovery_failure_and_inconsistent_totals_degrade(self):
        result = parse_backup_health(backup_fixture(discovery_failures=[{"reason": "private diagnostic"}]), NOW, 30)
        self.assertEqual(("degraded", "discovery_failed"), (result.status, result.detail))
        self.assertEqual(1, value(result, "backup.discovery_failures"))
        self.assertNotIn("private diagnostic", repr(result))
        inconsistent = parse_backup_health(backup_fixture(failed_pending_task_count=4), NOW, 30)
        self.assertEqual("degraded", inconsistent.status)
        self.assertIsNone(value(inconsistent, "backup.pending_tasks"))
        self.assertIsNone(value(inconsistent, "backup.failed_pending_tasks"))

    def test_backup_receipt_requires_schema_and_source_time(self):
        for data in (backup_fixture(schema_version=1), backup_fixture(observed_at="invalid")):
            result = BackupHealthAdapter(Path("fixture"), read=lambda path: data).collect(NOW)
            self.assertEqual(("error", ()), (result.status, result.samples))

    def test_backup_source_old_not_newly_verified_when_polled(self):
        result = parse_backup_health(backup_fixture(), NOW, 10)
        self.assertEqual("source_old", result.detail)
        self.assertEqual(NOW - 20, value(result, "backup.source_checked_at"))
        self.assertEqual(NOW - 80, value(result, "backup.last_verified_at"))


class HostHealthTest(unittest.TestCase):
    def test_binary_units_and_configured_disks_only(self):
        mem = "MemTotal:  4096 kB\nMemAvailable: 1024 kB\nSwapTotal: 512 kB\nSwapFree: 128 kB\nIgnored: 20 kB\n"
        adapter = HostHealthAdapter(Path("fixture"), {"root": Path("disk")},
                                    read_text=lambda path, cap: mem,
                                    disk_usage=lambda path: SimpleNamespace(total=10000, free=3000),
                                    cpu_count=lambda: 4)
        result = adapter.collect(NOW)
        self.assertEqual("ok", result.status)
        self.assertEqual(4096 * 1024, value(result, "host.memory_total"))
        self.assertEqual(1024 * 1024, value(result, "host.memory_available"))
        self.assertEqual(3000, value(result, "host.disk_available", "root"))
        self.assertEqual(4, value(result, "host.cpu_cores"))
        self.assertNotIn(("host.disk_available", "data"), samples(result))

    def test_missing_duplicate_and_impossible_values(self):
        result = parse_meminfo("MemTotal: 10 kB\nMemAvailable: 20 kB\nSwapTotal: 12 kB\nSwapFree: 8 kB\nSwapFree: 5 kB\n")
        self.assertIsNone(result["memory_total"])
        self.assertIsNone(result["memory_available"])
        self.assertIsNone(result["swap_free"])
        self.assertIsNone(parse_meminfo("MemTotal: 0 MB")["memory_total"])

    def test_unavailable_disk_not_zero(self):
        adapter = HostHealthAdapter(Path("fixture"), {"data": Path("disk")},
                                    read_text=lambda path, cap: "",
                                    disk_usage=lambda path: ArchiveHealthTest.raise_io(PermissionError()),
                                    cpu_count=lambda: None)
        result = adapter.collect(NOW)
        self.assertEqual("degraded", result.status)
        self.assertIsNone(value(result, "host.disk_total", "data"))
        self.assertIsNone(value(result, "host.disk_available", "data"))
        with self.assertRaises(ValueError):
            HostHealthAdapter(Path("fixture"), {"arbitrary-identifying-label": Path("disk")})


def service_fixture(**overrides):
    data = {"LoadState": "loaded", "ActiveState": "inactive", "Result": "success",
            "ExecMainCode": "1", "ExecMainStatus": "0", "ExecMainStartTimestamp": f"@{NOW - 80}",
            "ExecMainExitTimestamp": f"@{NOW - 20}"}
    data.update(overrides)
    return data


class ServiceHealthTest(unittest.TestCase):
    def test_failed_service_active_timer_vs_completed_timer(self):
        units = (UnitSpec("owner", "owner.service", "owner.timer"), UnitSpec("backup", "backup.service", "backup.timer"))
        def show(unit, properties):
            if properties == TIMER_PROPERTIES:
                return {"LoadState": "loaded", "ActiveState": "active", "LastTriggerUSec": f"@{NOW - 80}",
                        "NextElapseUSecRealtime": f"@{NOW + 60}"}
            return service_fixture(ActiveState="failed", Result="exit-code", ExecMainStatus="1") if unit == "owner.service" else service_fixture()
        result = SystemdHealthAdapter(units, show=show).collect(NOW)
        self.assertEqual(("degraded", "unit_failure"), (result.status, result.detail))
        self.assertEqual(0, value(result, "service.run_success", "owner"))
        self.assertEqual(1, value(result, "service.timer_active", "owner"))
        self.assertEqual(1, value(result, "service.run_success", "backup"))
        self.assertEqual(0, value(result, "service.active", "backup"))

    def test_running_never_run_and_missing_units_unknown(self):
        fixtures = [service_fixture(ActiveState="activating"),
                    service_fixture(ExecMainStartTimestamp="", ExecMainExitTimestamp=""),
                    service_fixture(LoadState="not-found"),
                    service_fixture(ExecMainExitTimestamp=f"@{NOW - 100}")]
        for f in fixtures:
            with self.subTest(f=f):
                result = SystemdHealthAdapter((UnitSpec("worker", "worker.service"),), show=lambda unit, props: f).collect(NOW)
                self.assertIsNone(value(result, "service.run_success", "worker"))

    def test_timeout_and_unknown_timer_schedule(self):
        def show(unit, props):
            if props == SERVICE_PROPERTIES:
                raise subprocess.TimeoutExpired("secret private process args", 2)
            return {"LoadState": "loaded", "ActiveState": "active", "LastTriggerUSec": f"@{NOW - 30}",
                    "NextElapseUSecRealtime": ""}
        result = SystemdHealthAdapter((UnitSpec("task", "task.service", "task.timer"),), show=show).collect(NOW)
        self.assertIsNone(value(result, "service.active", "task"))
        self.assertIsNone(value(result, "service.next_run_at", "task"))
        self.assertEqual(1, value(result, "service.timer_active", "task"))
        self.assertEqual("degraded", result.status)
        self.assertNotIn("private process args", repr(result))

    def test_total_read_budget_and_unit_count(self):
        units = (UnitSpec("one", "one.service", "one.timer"), UnitSpec("two", "two.service"))
        with patch("operations.health.SYSTEMD_BUDGET_SECONDS", 1):
            # At the first check budget has already elapsed. No subprocess.
            ticks = iter([0, 2, 3, 4])
            result = SystemdHealthAdapter(units, show=lambda *a: self.fail("must not read beyond budget"), clock=lambda: next(ticks)).collect(NOW)
            self.assertEqual("degraded", result.status)
            self.assertTrue(all(m.value is None for m in result.samples))
        with self.assertRaises(ValueError):
            SystemdHealthAdapter(tuple(UnitSpec(f"unit{i}", f"unit{i}.service") for i in range(19)))
        with self.assertRaises(ValueError):
            UnitSpec("not/sluggish", "unit.service")

    def test_systemctl_read_is_fixed_argv_and_allowlisted_properties(self):
        with patch("operations.health.subprocess.run") as command:
            command.return_value = SimpleNamespace(stdout="ActiveState=failed\nLoadState=loaded\nOther=private-data\n")
            result = _show_unit("worker.service", SERVICE_PROPERTIES)
            args, kwargs = command.call_args
            self.assertEqual(["systemctl", "show"], args[0][:2])
            self.assertEqual(["--", "worker.service"], args[0][-2:])
            self.assertEqual(2, kwargs["timeout"])
            self.assertNotIn("Other", result)
        with self.assertRaises(ValueError):
            _show_unit("worker.service", ("Environment",))


class ReceiptTest(unittest.TestCase):
    def test_bounded_receipts_and_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps({"a": 1}), encoding="utf-8")
            self.assertEqual({"a": 1}, read_receipt(path))
            path.write_text('{"a": 1,"a": 2}', encoding="utf-8")
            with self.assertRaises(ValueError):
                read_receipt(path)
            path.write_bytes(b" " * (MAX_RECEIPT_BYTES + 1))
            with self.assertRaises(ValueError):
                read_receipt(path)


if __name__ == "__main__":
    unittest.main()
