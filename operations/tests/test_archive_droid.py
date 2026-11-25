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

    def test_waiting_and_completed_tables(self):
        from operations.archive_droid import collect, render
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        state, repo, work, view = root / "state", root / "repo", root / "work", root / "view"
        (repo / "ops/wayback").mkdir(parents=True)
        (state / "registry/promotions").mkdir(parents=True)
        (work).mkdir(parents=True)
        (view).mkdir(parents=True)
        (repo / "ops/wayback/queue.txt").write_text(
            "https://swccg.com/wiki/images/a/aa/Waiting.png\n"
            "https://swccg.com/wiki/images/b/bb/Verified.png\n"
            "https://swccg.com/wiki/images/c/cc/Failed.png\n"
            "https://swccg.com/wiki/images/d/dd/Pending.png\n"
        )
        (repo / "ops/wayback/done.json").write_text(json.dumps({
            "https://swccg.com/wiki/images/a/aa/Waiting.png": {"status": "ok", "last": "2026-11-20T12:00:00Z", "capture": ""},
            "https://swccg.com/wiki/images/b/bb/Verified.png": {
                "status": "verified", "last": "2026-11-25T10:00:00Z",
                "capture": "https://web.archive.org/web/20261125100000/https://swccg.com/wiki/images/b/bb/Verified.png",
                "verify": {"at": "2026-11-25T18:00:00Z"}},
            "https://swccg.com/wiki/images/c/cc/Failed.png": {"status": "failed", "last": "2026-11-21T08:00:00Z", "capture": "", "error": "x"},
            "https://swccg.com/wiki/images/d/dd/Pending.png": {"status": "pending", "last": "2026-11-22T09:00:00Z", "job_id": "spn2-x"},
            "https://swccg.com/wiki/images/e/ee/OldVerified.png": {
                "status": "verified", "last": "2026-11-01T00:00:00Z",
                "capture": "https://web.archive.org/web/20261101000000/https://swccg.com/wiki/images/e/ee/OldVerified.png"},
        }))
        (state / "registry/promotions/aa.json").write_text(json.dumps({
            "source_url": "https://swccg.com/wiki/images/a/aa/Waiting.png",
            "queued_at": "2026-11-19T15:00:00+00:00"}))
        (state / "linker.json").write_text(json.dumps({
            "File:Verified.png|https://web.archive.org/web/20261125100000/https://swccg.com/wiki/images/b/bb/Verified.png": "already"}))
        (work / "sweep-index.json").write_text(json.dumps({"tickets": {
            core.norm_url("https://swccg.com/wiki/images/a/aa/Waiting.png"): 1310,
            core.norm_url("https://swccg.com/wiki/images/b/bb/Verified.png"): 1310}}))
        args = mock.Mock(state=state, repo=repo, work=work, view=view)
        paths = collect.Paths(args)
        data = collect.build_now(paths)
        self.assertEqual([r["stage"] for r in data["queue"]], ["ok", "submitted"])
        self.assertEqual([r["stage"] for r in data["problems"]], ["failed"])
        self.assertEqual(len(data["completed"]), 2)
        self.assertEqual(data["completed"][0]["verified_at"], "2026-11-25T18:00:00Z")  # newest first
        self.assertIsNone(data["completed"][1]["verified_at"])  # no verify.at -> unknown, not last
        self.assertEqual(data["completed"][0]["preservation"], "yes")
        self.assertEqual(data["completed"][1]["preservation"], "unknown")
        self.assertEqual(data["queue"][0]["queued_at"], "2026-11-19T15:00:00+00:00")
        self.assertIsNone(data["queue"][1].get("queued_at"))
        render.write_now(view, data)
        html = (view / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="queue"', html)
        self.assertIn('id="problems"', html)
        self.assertIn('id="completed"', html)
        self.assertIn('id="verified"', html)
        self.assertIn("Queue (2)", html)
        self.assertIn("Problems (1)", html)
        self.assertIn("Completed / Verified (2)", html)
        self.assertIn("Waiting.png", html)
        self.assertIn("Pending.png", html)
        self.assertNotIn("Failed.png", html[html.find('id="queue"'):html.find('id="problems"')])  # not in waiting table
        self.assertIn("Failed.png", html[html.find('id="problems"'):html.find('id="completed"')])
        # Verified.png belongs in completed, not the waiting queue
        qsec = html[html.find('id="queue"'):html.find('id="problems"')]
        self.assertNotIn("Verified.png", qsec)
        self.assertIn("Verified.png", html[html.find('id="completed"'):])
        self.assertIn("unknown", html)  # date or preservation gaps
        self.assertIn("Wayback", html)
        self.assertIn("19 Nov 2026", html)  # queued_at 15:00 UTC -> 11:00 UTC-4
        self.assertTrue((view / "completed/1.html").exists())
        self.assertNotIn("web.archive.org/web/20261101000000", render.when_known(None))
        self.assertEqual(render.when_known(None), "unknown")
        self.assertEqual(render.when_known(""), "unknown")

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
