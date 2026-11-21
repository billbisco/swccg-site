"""Synthetic tests for the GEMP daily rollup projection, no database access."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from operations.analytics import EVENT_IDS, GempDailyAdapter, parse_gemp_daily


NOW = 1_700_000_000  # Synthetic date, no production counts.
ALL = frozenset(EVENT_IDS)


def receipt(validated=ALL, days=60, observed=NOW - 10):
    end = datetime.fromtimestamp(observed, timezone.utc).date()
    return {
        "schema": 1, "source": "gemp_daily", "environment": "play", "observed_at": observed,
        "validated_events": sorted(validated),
        "days": [
            {"day": (end - timedelta(days=i)).isoformat(),
             "metrics": {"_rolled": 1, "game_start": i, "play_click": i * 2}}
            for i in range(1, days + 1)
        ],
    }


def value(result, metric, dimension="all"):
    return next(m.value for m in result.samples if m.metric_id == metric and m.dimension == dimension)


class GempReceiptTest(unittest.TestCase):
    def test_comparisons_use_completed_utc_days_and_equal_prior_windows(self):
        result = parse_gemp_daily(receipt(), NOW, ALL)
        self.assertEqual("ok", result.status)
        self.assertEqual(1, value(result, "player.instrumented_bot_game_start", "completed_1d"))
        self.assertEqual(2, value(result, "player.instrumented_bot_game_start", "prior_1d"))
        self.assertEqual(sum(range(1, 8)), value(result, "player.instrumented_bot_game_start", "completed_7d"))
        self.assertEqual(sum(range(8, 15)), value(result, "player.instrumented_bot_game_start", "prior_7d"))
        self.assertEqual(sum(range(1, 31)), value(result, "player.instrumented_bot_game_start", "completed_30d"))
        self.assertEqual(sum(range(31, 61)), value(result, "player.instrumented_bot_game_start", "prior_30d"))
        self.assertEqual(30, value(result, "player.covered_days", "prior_30d"))
        self.assertEqual(0, value(result, "player.instrumented_game_end", "completed_1d"))
        midnight = NOW // 86400 * 86400
        self.assertEqual(midnight, value(result, "player.window_end_at"))
        self.assertEqual(NOW - 10, value(result, "player.source_checked_at"))
        self.assertTrue(all(m.kind == "gauge" for m in result.samples))

    def test_missing_marker_or_day_never_zero_fills(self):
        data = receipt()
        del data["days"][0]
        del data["days"][3]["metrics"]["_rolled"]
        result = parse_gemp_daily(data, NOW, ALL)
        self.assertEqual(("degraded", "partial_days"), (result.status, result.detail))
        self.assertIsNone(value(result, "player.instrumented_play_click", "completed_1d"))
        self.assertEqual(0, value(result, "player.covered_days", "completed_1d"))
        self.assertEqual(5, value(result, "player.covered_days", "completed_7d"))
        self.assertIsNone(value(result, "player.instrumented_play_click", "completed_7d"))
        self.assertEqual(sum(range(8, 15)) * 2, value(result, "player.instrumented_play_click", "prior_7d"))

    def test_unvalidated_events_are_unknown_even_if_positive_or_marker_present(self):
        supported = frozenset({"play_click"})
        result = parse_gemp_daily(receipt(supported), NOW, supported)
        self.assertEqual(("degraded", "event_coverage_unverified"), (result.status, result.detail))
        self.assertEqual(2, value(result, "player.instrumented_play_click", "completed_1d"))
        self.assertIsNone(value(result, "player.instrumented_bot_game_start", "completed_1d"))
        self.assertIsNone(value(result, "player.instrumented_game_end", "completed_1d"))
        self.assertEqual(1, value(result, "player.validated_event_types"))
        no_verified_emitters = parse_gemp_daily(receipt(frozenset()), NOW, frozenset())
        self.assertIsNone(value(no_verified_emitters, "player.instrumented_play_click", "completed_1d"))

    def test_receipt_cannot_expand_deployment_capability(self):
        result = parse_gemp_daily(receipt(ALL), NOW, frozenset())
        self.assertEqual(("error", "invalid_receipt", ()), (result.status, result.detail, result.samples))

    def test_partial_current_day_and_stage_are_rejected(self):
        data = receipt()
        data["days"][-1]["day"] = datetime.fromtimestamp(NOW, timezone.utc).date().isoformat()
        self.assertEqual("error", parse_gemp_daily(data, NOW, ALL).status)
        data = receipt()
        data["environment"] = "test"
        self.assertEqual("error", parse_gemp_daily(data, NOW, ALL).status)

    def test_future_receipt_and_duplicate_day_rejected(self):
        self.assertEqual("error", parse_gemp_daily(receipt(observed=NOW + 1), NOW, ALL).status)
        data = receipt()
        data["days"][1] = data["days"][0]
        self.assertEqual("error", parse_gemp_daily(data, NOW, ALL).status)

    def test_strict_aggregate_envelope_and_metric_keys_do_not_leak(self):
        for field, raw in (("unapproved_label", 10), ("game_start", True), ("game_start", -1),
                           ("game_start", 2**31), ("_rolled", 0), ("_rolled", True)):
            with self.subTest(field=field, raw=raw):
                data = receipt()
                data["days"][0]["metrics"][field] = raw
                result = parse_gemp_daily(data, NOW, ALL)
                self.assertEqual("error", result.status)
                self.assertNotIn("unapproved_label", repr(result))
                self.assertEqual((), result.samples)
        data = receipt()
        data["unsupported"] = "untrusted content"
        result = parse_gemp_daily(data, NOW, ALL)
        self.assertEqual("error", result.status)
        self.assertNotIn("untrusted content", repr(result))

    def test_stale_receipt_keeps_source_timestamp_and_coverage_unknown(self):
        result = parse_gemp_daily(receipt(observed=NOW - 2 * 86400), NOW, ALL)
        self.assertEqual(("degraded", "source_old"), (result.status, result.detail))
        self.assertEqual(NOW, result.observed_at)
        self.assertEqual(NOW - 2 * 86400, value(result, "player.source_checked_at"))
        self.assertIsNone(value(result, "player.instrumented_play_click", "completed_1d"))

    def test_rollup_missing_days_beyond_source_retention_returns_partial(self):
        result = parse_gemp_daily(receipt(days=1), NOW, ALL)
        self.assertEqual(2, value(result, "player.instrumented_play_click", "completed_1d"))
        self.assertIsNone(value(result, "player.instrumented_play_click", "completed_30d"))
        self.assertEqual(1, value(result, "player.covered_days", "completed_30d"))
        self.assertEqual(30, value(result, "player.expected_days", "completed_30d"))

    def test_missing_unreadable_and_malformed_receipt_are_not_empty_zero_days(self):
        def missing(path):
            raise FileNotFoundError("private source path")
        adapter = GempDailyAdapter(Path("synthetic"), ALL, read=missing)
        result = adapter.collect(NOW)
        self.assertEqual(("unavailable", (), "missing_receipt"), (result.status, result.samples, result.detail))
        self.assertNotIn("private source path", repr(result))
        adapter = GempDailyAdapter(Path("synthetic"), ALL, read=lambda path: [])
        self.assertEqual("error", adapter.collect(NOW).status)

    def test_config_allowlist_is_static(self):
        with self.assertRaises(ValueError):
            GempDailyAdapter(Path("synthetic"), frozenset({"any_account_label"}))


if __name__ == "__main__":
    unittest.main()
