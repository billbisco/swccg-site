import copy
import json
import unittest
from pathlib import Path

from operations.dashboard import render_dashboard


FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "dashboard-mock.json"


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_render_separates_provider_health_and_failed_run(self):
        rendered = render_dashboard(self.snapshot)
        self.assertIn("Verified archive captures</h2><strong>123</strong>", rendered)
        self.assertIn('class="tile danger"><h2>Archive latest run succeeded</h2><strong>No</strong>', rendered)
        self.assertIn("Archive provider healthy</h2><strong>Yes</strong>", rendered)
        self.assertIn("Provider healthy", rendered)
        self.assertIn("Last verified progress", rendered)
        self.assertIn("Git backup", rendered)
        self.assertIn('class="bad-value">No</td>', rendered)
        self.assertIn("Missing data is not zero", rendered)
        self.assertIn("Historical comparisons", rendered)
        self.assertNotIn("MOCK PREVIEW", rendered)

    def test_recovery_panel_uses_distinct_fixed_cohort(self):
        archive=self.snapshot['sources'][0]
        for key,n in {'total':70,'no_job':30,'auth':32,'other':8,'verified':5,'verified_manpc':3,'in_progress':10,'review':20,'waiting':35}.items():
            archive['metrics'].append({'metric_id':'archive.recovery_'+key,'value':n,'unit':'count','dimension':'all','kind':'gauge','observed_at':1700000000})
        page=render_dashboard(self.snapshot)
        self.assertIn('5 of 70 verified',page)
        self.assertIn('MANPC accepted: 3',page)

    def test_preview_is_labeled_and_has_no_external_dependencies(self):
        rendered = render_dashboard(self.snapshot, preview=True)
        self.assertIn("MOCK PREVIEW", rendered)
        self.assertIn('name="robots" content="noindex,nofollow"', rendered)
        self.assertNotIn("<script", rendered)
        self.assertNotIn("http://", rendered)
        self.assertNotIn("https://", rendered)
        self.assertNotIn("<link", rendered)

    def test_stale_source_has_explicit_warning_and_keeps_timestamp(self):
        archive = self.snapshot["sources"][0]
        archive.update(status="degraded", detail="source_old", stale=True)
        rendered = render_dashboard(self.snapshot)
        self.assertIn("Stale · last check degraded", rendered)
        self.assertIn('class="tile warn"><h2>Verified archive captures', rendered)
        self.assertIn('class="tile warn"><h2>Archive latest run succeeded', rendered)

    def test_only_allowlisted_data_displayed(self):
        archive = self.snapshot["sources"][0]
        archive["detail"] = "<script>alert('unexpected private detail')</script>"
        archive["cursor"] = "unexpected private cursor"
        archive["metrics"].append({"metric_id": "archive.unrecognized_private","dimension": "all","value": 9123456,"unit":"count","kind":"gauge","observed_at":1700000000})
        archive["metrics"].append({"metric_id": "archive.pending","dimension": "private_client_label","value": 9123455,"unit":"count","kind":"gauge","observed_at":1700000000})
        self.snapshot["sources"].append({"source":"arbitrary_private_path","metrics":[]})
        rendered = render_dashboard(self.snapshot)
        for unexpected in ("<script>", "unexpected private detail", "unexpected private cursor", "unrecognized_private", "private_client_label", "9,123,456", "9,123,455", "arbitrary_private_path"):
            self.assertNotIn(unexpected, rendered)

    def test_invalid_value_is_unavailable_real_zero_is_preserved(self):
        self.snapshot["sources"][0]["metrics"][0]["value"] = float("nan")
        rendered = render_dashboard(self.snapshot)
        self.assertIn("Verified archive captures</h2><strong>Unavailable</strong>", rendered)
        alternate = copy.deepcopy(self.snapshot)
        alternate["sources"][0]["metrics"][0]["value"] = 0
        self.assertIn("Verified archive captures</h2><strong>0</strong>", render_dashboard(alternate))

    def test_empty_or_unavailable_source_is_not_zero(self):
        page = render_dashboard({"schema_version":1,"generated_at":None,"sources":[]})
        self.assertIn("Fresh source checks</h2><strong>Unavailable</strong>", page)
        self.assertIn("Verified archive captures</h2><strong>Unavailable</strong>", page)
        self.assertIn("No aggregate has been collected", page)

    def test_rejects_schema_or_duplicate_source(self):
        for value in (None, {}, {"schema_version":2,"sources":[]}, {"schema_version":True,"sources":[]}):
            with self.assertRaises(ValueError):
                render_dashboard(value)
        self.snapshot["sources"].append(self.snapshot["sources"][0])
        with self.assertRaises(ValueError):
            render_dashboard(self.snapshot)

    def test_provider_backoff_and_normal_pause_are_not_crash(self):
        archive = self.snapshot["sources"][0]
        archive["status"] = "backoff"
        for metric in archive["metrics"]:
            if metric["metric_id"] == "archive.mode_healthy":
                metric["value"] = 0
        for metric_id in ("archive.mode_open", "archive.run_paused"):
            archive["metrics"].append({"metric_id": metric_id, "value": 1, "unit":"boolean", "dimension":"all", "kind":"gauge", "observed_at":1700000000})
        page = render_dashboard(self.snapshot)
        self.assertIn('class="tile warn"><h2>Archive latest run succeeded</h2><strong>Automatic cooldown</strong>', page)
        self.assertIn('class="tile warn"><h2>Archive provider healthy</h2><strong>Backoff</strong>', page)
        self.assertNotIn('class="tile danger"', page)


if __name__ == "__main__":
    unittest.main()
