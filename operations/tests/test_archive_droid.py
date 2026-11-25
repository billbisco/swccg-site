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


class RenderTests(unittest.TestCase):
    def test_pages_are_noindex(self):
        from operations.archive_droid import render
        html = render.page("Archive Droid", "<p>ok</p>", refresh=300)
        self.assertIn('<meta name="robots" content="noindex, nofollow">', html)
        self.assertNotIn("rejected_credential", html)

    def test_write_now_is_unlisted_and_has_four_blocks(self):
        from operations.archive_droid import render
        view = Path(self.tmp.name) if hasattr(self, "tmp") else None
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        view = Path(tmp.name)
        render.write_now(view, {
            "generated_at": "2026-11-25T18:00:00+00:00",
            "health": {"mode": "healthy", "reason": "account capacity unavailable"},
            "capacity": {"available": 3, "daily_captures": 1, "daily_captures_limit": 30000},
            "service": {}, "timer": {},
            "queue": [{"pos": 1, "url": "https://swccg.com/wiki/File:Example.png", "ticket": 1310, "stage": "verified", "current": True}],
            "queue_len": 1,
            "approvals_log": [{"at": "2026-11-25T16:53:29+00:00", "ticket": 1339, "change": "added", "count": 1, "total_after": 1711, "source": "backfill", "reason": "approved capturing Route-C snapshots"}],
            "stages": {"linked": 1, "held": 2},
            "warnings": [],
        })
        html = (view / "index.html").read_text(encoding="utf-8")
        self.assertIn('<meta name="robots" content="noindex, nofollow">', html)
        self.assertIn('id="now"', html)
        self.assertIn('id="queue"', html)
        self.assertIn('id="approved"', html)
        self.assertIn('id="missing"', html)
        self.assertIn("Unlisted, read-only", html)
        self.assertNotIn("rejected_credential", html)
        self.assertNotIn("billbisco", html.lower())

    def test_private_repo_urls_are_redacted(self):
        from operations.archive_droid import render
        private = "https://github.com/billbisco/ai-team-skills/blob/abc/shared/RULINGS.md"
        raw = "https://raw.githubusercontent.com/billbisco/swccg-cardmaker/abc/assets/x.png"
        public = "https://github.com/swccg-guild/work/issues/1360"
        self.assertEqual(render.safe_link(private), '<span class="muted">private source</span>')
        self.assertEqual(render.safe_link(raw, "x.png"), '<span class="muted">private source</span>')
        self.assertIn('href="https://github.com/swccg-guild/work/issues/1360"', render.safe_link(public))
        self.assertNotIn("billbisco", render.safe_link(private))
        self.assertNotIn("ai-team-skills", render.safe_link(private))
        html = render.page("t", "<p>" + render.safe_link(private) + render.safe_link(public) + "</p>")
        self.assertNotIn("billbisco", html)
        self.assertNotIn("ai-team-skills", html)


if __name__ == "__main__":
    unittest.main()
