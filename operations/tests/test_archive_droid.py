import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from operations.archive_droid import collect, core


class CoreTests(unittest.TestCase):
    def test_norm_url_spellings_collapse(self):
        a = core.norm_url("https://wiki.swccg.com/images/6/6b/A_b.png")
        b = core.norm_url("https://swccg.com/wiki/images/6/6b/A b.png")
        self.assertEqual(a, b)
        self.assertEqual(core.norm_url("https://swccg.com/wiki/File:A_b.png"),
                         core.norm_url("https://wiki.swccg.com/wiki/File:A%20b.png"))

    def test_ticket_of(self):
        self.assertEqual(core.ticket_of("https://github.com/swccg-guild/work/issues/382"), 382)
        self.assertEqual(core.ticket_of(1310), 1310)
        self.assertIsNone(core.ticket_of(None))
        self.assertIsNone(core.ticket_of(True))

    def test_object_stage_precedence(self):
        s = lambda **k: core.object_stage(**{**dict(ready=False, reason="", approved=False, queued=False, done_status=None), **k})
        self.assertEqual(s(reason=core.HOLD_MARK + "; do not submit"), "held")
        self.assertEqual(s(ready=True), "ready-unapproved")
        self.assertEqual(s(ready=True, approved=True), "approved-not-queued")
        self.assertEqual(s(approved=True, queued=True), "queued")
        self.assertEqual(s(done_status="failed"), "problem")
        self.assertEqual(s(done_status="verified"), "verified-no-link")

    def test_file_stage_exactly_one(self):
        self.assertEqual(core.file_stage([], linked=False), "unregistered")
        self.assertEqual(core.file_stage(["held", "queued"], linked=False), "queued")
        self.assertEqual(core.file_stage(["not-ready"], linked=False, done_statuses=["verified"]), "verified-no-link")
        self.assertEqual(core.file_stage(["verified-no-link"], linked=True), "linked")
        self.assertEqual(core.file_stage(["queued"], linked=True), "queued")

    def test_approval_diff_groups_by_ticket(self):
        before = {"object_ids": ["a"], "additional_approval_scopes": []}
        after = {"object_ids": ["a", "b", "c", "d"], "additional_approval_scopes": [
            {"ticket": 1330, "exact_object_ids": ["b", "c"], "reason": "r", "reviewed_at": "2026-11-24T20:41:23Z"}]}
        out = core.approval_diff(before, after, at="2026-11-24T20:41:23Z", source="t")
        by = {(e["ticket"], e["change"]): e["count"] for e in out}
        self.assertEqual(by, {(1330, "added"): 2, (None, "added"): 1})
        self.assertTrue(all(e["total_after"] == 4 for e in out))


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "state/registry/requests").mkdir(parents=True)
        (root / "repo/ops/wayback").mkdir(parents=True)
        self.approved = root / "state/registry/approved-objects.json"
        self.approved.write_text(json.dumps({"object_ids": ["x"], "additional_approval_scopes": [
            {"ticket": 994, "exact_object_ids": ["x"], "reason": "first"}]}))
        args = mock.Mock(state=root / "state", repo=root / "repo", work=root / "work", view=root / "view")
        self.p = collect.Paths(args)

    def tearDown(self):
        self.tmp.cleanup()

    def test_hook_is_append_only_and_never_writes_approvals(self):
        before = self.approved.read_bytes()
        self.assertEqual(collect.approvals_hook(self.p), 1)  # backfill
        self.assertEqual(collect.approvals_hook(self.p), 0)  # idempotent
        self.approved.write_text(json.dumps({"object_ids": ["x", "y"], "additional_approval_scopes": [
            {"ticket": 994, "exact_object_ids": ["x"]}, {"ticket": 1360, "exact_object_ids": ["y"]}]}))
        after = self.approved.read_bytes()
        self.assertEqual(collect.approvals_hook(self.p), 1)
        log = [json.loads(l) for l in self.p.applog.read_text().splitlines()]
        self.assertEqual([(e["ticket"], e["change"], e["count"]) for e in log],
                         [(994, "added", 1), (1360, "added", 1)])
        self.assertEqual(self.approved.read_bytes(), after)
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
