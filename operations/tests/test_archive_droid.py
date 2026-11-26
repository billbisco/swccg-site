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
        self.assertEqual(data["completed"][1]["preservation"], "not yet")  # File page, link still due
        self.assertEqual(data["queue"][0]["added_at"], "2026-11-19T15:00:00+00:00")
        self.assertEqual(data["queue"][0]["added_source"], "registry")
        self.assertEqual(data["queue"][0]["added_field"], "queued_at")
        self.assertEqual(data["queue"][1]["added_source"], "on-or-before")
        self.assertTrue(data["queue"][1]["added_at"])
        self.assertEqual(data["problems"][0]["added_source"], "on-or-before")
        self.assertEqual(data["added_counts"]["registry"], 1)
        self.assertEqual(data["added_counts"]["on-or-before"], 2)  # pending + failed
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
        self.assertIn("19 Nov 2026", html)  # registry queued_at 15:00 UTC -> 11:00 UTC-4
        self.assertIn("Date added", html)
        self.assertIn("First seen by page", html)
        self.assertIn("How this queue works (for every AI)", html)
        self.assertIn("preservation_enqueue.py", html)
        self.assertIn("auto-approves", html)
        self.assertIn("AIs do not hand-write approvals", html)
        self.assertIn("id=\"how-queue\"", html)
        self.assertIn("Do not", html)
        self.assertIn("on or before", html)
        self.assertIn("registry (queued_at)", html)
        self.assertTrue((view / "completed/1.html").exists())
        self.assertNotIn("web.archive.org/web/20261101000000", render.when_known(None))
        self.assertEqual(render.when_known(None), "unknown")
        self.assertEqual(render.when_known(""), "unknown")


    def test_item_copy_keys_and_link_kinds(self):
        self.assertEqual(core.ia_key("swccg-wiki-images-a", "af/X y.png"),
                         core.norm_url("https://swccg.com/wiki/images/a/af/X_y.png"))
        self.assertEqual(core.ia_key("swccg-wiki-images-archive", "0/00/20201010!A.png"),
                         core.norm_url("https://swccg.com/wiki/images/archive/0/00/20201010!A.png"))
        self.assertEqual(core.ia_key("swccg-com-snapshots", "forum/p1.html"), "url:swccg.com/snapshots/forum/p1.html")
        self.assertIsNone(core.ia_key("other", "x"))
        self.assertEqual(core.link_kind("https://swccg.com/wiki/images/a/af/X.png"), "file")
        self.assertIsNone(core.link_kind("https://swccg.com/wiki/images/archive/a/af/1!X.png"))
        self.assertEqual(core.link_kind("https://swccg.com/snapshots/forum/p123.html"), "snapshot")
        self.assertIsNone(core.link_kind("https://swccg.com/wiki/Some_Page"))

    def test_speed_items_and_hold_blocks(self):
        from operations.archive_droid import collect, render
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        state, repo, work, view = root / "wb", root / "repo", root / "work", root / "view"
        for d in (repo / "ops/wayback", state / "registry", work, view, root / "swccg-ia-items"):
            d.mkdir(parents=True)
        (repo / "ops/wayback/queue.txt").write_text("")
        (repo / "ops/wayback/done.json").write_text(json.dumps({
            "https://swccg.com/wiki/Some_Page": {"status": "verified", "capture": "https://web.archive.org/web/1/x",
                                                 "verify": {"at": "2026-11-26T10:00:00Z"}},
            "https://swccg.com/wiki/images/a/af/Linked.png": {"status": "verified", "capture": "https://web.archive.org/web/2/y",
                                                               "verify": {"at": "2026-11-26T09:00:00Z"}}}))
        (state / "linker.json").write_text(json.dumps({"ia|File:Linked.png": "linked"}))
        (state / "registry/policy-unapproved.json").write_text(json.dumps(
            {"checked_at": "2026-11-26T15:00:00+00:00", "counts": {"held": 491, "approve": 10}, "no_ticket": 0}))
        (state / "pace-state.json").write_text(json.dumps({
            "allowance": 14, "gap_s": 45, "bounds": {"min": 3, "max": 40, "gap_min_s": 20},
            "last_run": {"submits": 12, "refusals": 0, "rate_limited": False, "used_full_allowance": True,
                         "ended": "2026-11-26T15:10:00+00:00"},
            "reason": "clean run used its whole allowance: +2",
            "promotion": {"target_buffer": 300, "buffer_before": 40, "promoted": 120, "cap": 200, "seconds": 61}}))
        (state / "pace-log.jsonl").write_text(json.dumps({"at": "2026-11-26T15:10:00+00:00", "from": 12, "to": 14,
                                                          "gap_from": 60, "gap_to": 45, "reason": "clean run"}) + "\n")
        (root / "swccg-ia-items/ledger.json").write_text(json.dumps({
            "swccg-wiki-images-a/af/Linked.png": {"item": "swccg-wiki-images-a", "name": "af/Linked.png", "verified": "2026-11-25T00:00:00Z"},
            "swccg-com-snapshots/forum/p1.html": {"item": "swccg-com-snapshots", "name": "forum/p1.html"}}))
        data = collect.build_now(collect.Paths(mock.Mock(state=state, repo=repo, work=work, view=view)))
        self.assertEqual(data["hold_marked"], 491)
        self.assertEqual(data["ia"]["uploaded"], 2)
        self.assertEqual(data["ia"]["verified"], 1)
        pres = {r["url"]: r["preservation"] for r in data["completed"]}
        self.assertEqual(pres["https://swccg.com/wiki/images/a/af/Linked.png"], "yes")
        self.assertTrue(pres["https://swccg.com/wiki/Some_Page"].startswith("not needed"))
        render.write_now(view, data)
        html = (view / "index.html").read_text(encoding="utf-8")
        self.assertIn("Archive Droid sets its own speed", html)
        self.assertIn("clean run used its whole allowance", html)
        self.assertIn("Recent pace changes", html)
        self.assertIn("491 registry objects still carry the 14 Nov 2026 hold stamp", html)
        self.assertIn("Archive.org item copies", html)
        self.assertNotIn("promotes 2 per run", html)
        self.assertNotIn("1358</a> decides", html)
        self.assertNotIn("at most two approved jobs", html)
        data["speed"] = None
        render.write_now(view, data)
        self.assertIn("Self-tuning starts with the next worker run", (view / "index.html").read_text(encoding="utf-8"))

    def test_first_seen_log_is_append_only_and_ignores_upload_timestamp(self):
        from operations.archive_droid import collect, render
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        state, repo, work, view = root / "state", root / "repo", root / "work", root / "view"
        (repo / "ops/wayback").mkdir(parents=True)
        (state / "registry/requests/job/obj").mkdir(parents=True)
        (work).mkdir(parents=True)
        (view).mkdir(parents=True)
        already = "https://swccg.com/wiki/images/a/aa/Already.png"
        later = "https://swccg.com/wiki/images/b/bb/Later.png"
        (repo / "ops/wayback/queue.txt").write_text(already + "\n")
        (repo / "ops/wayback/done.json").write_text("{}")
        # Wiki upload_timestamp must not be used as Date added.
        (state / "registry/requests/job/obj/req.json").write_text(json.dumps({
            "schema_version": 1, "subject": {"kind": "wiki_file_version", "title": "Already.png",
                                             "upload_timestamp": "2020-01-01T00:00:00Z",
                                             "original_version_url": already},
            "source": {"url": already, "sha256": "a"*64, "bytes": 1},
            "readiness": {"ready": True, "reason": "x", "stage_bytes": 1},
            "owner": "x", "continuation": "x", "approval_scope": {"ticket": 1},
        }))
        args = mock.Mock(state=state, repo=repo, work=work, view=view)
        paths = collect.Paths(args)
        first = collect.build_now(paths)
        self.assertEqual(first["queue"][0]["added_source"], "on-or-before")
        self.assertNotIn("2020-01-01", first["queue"][0]["added_at"])
        seed_at = first["queue"][0]["added_at"]
        log = (work / "first-seen.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event":"seed-done"', log)
        first_log = log
        # A later registry date for the same URL must not rewrite the log.
        (state / "registry/promotions").mkdir(parents=True)
        (state / "registry/promotions/aa.json").write_text(json.dumps({
            "source_url": already, "registered_at": "2019-06-01T00:00:00Z"}))
        (repo / "ops/wayback/queue.txt").write_text(already + "\n" + later + "\n")
        second = collect.build_now(paths)
        self.assertEqual(second["queue"][0]["added_at"], seed_at)
        self.assertEqual(second["queue"][0]["added_source"], "on-or-before")
        self.assertEqual(second["queue"][1]["added_source"], "first-seen")
        self.assertTrue(second["queue"][1]["added_at"])
        later_log = (work / "first-seen.jsonl").read_text(encoding="utf-8")
        self.assertTrue(later_log.startswith(first_log))  # append-only
        self.assertIn("Later.png", later_log)
        render.write_now(view, second)
        html = (view / "index.html").read_text(encoding="utf-8")
        self.assertIn("first seen by page", html)
        self.assertIn("on or before", html)
        self.assertNotIn("2019-06-01", html)
        self.assertNotIn("2020-01-01", html)
        # Request with a real registered_at, on a fresh state+work dir, is used for seed.
        state2, work2, view2 = root / "state2", root / "work2", root / "view2"
        (state2 / "registry/requests/job/obj").mkdir(parents=True)
        work2.mkdir(); view2.mkdir()
        (state2 / "registry/requests/job/obj/req.json").write_text(json.dumps({
            "schema_version": 1, "registered_at": "2026-11-10T12:00:00Z",
            "subject": {"kind": "wiki_file_version", "title": "Already.png",
                        "upload_timestamp": "2020-01-01T00:00:00Z",
                        "original_version_url": already},
            "source": {"url": already, "sha256": "a"*64, "bytes": 1},
            "readiness": {"ready": True, "reason": "x", "stage_bytes": 1},
            "owner": "x", "continuation": "x", "approval_scope": {"ticket": 1},
        }))
        paths2 = collect.Paths(mock.Mock(state=state2, repo=repo, work=work2, view=view2))
        third = collect.build_now(paths2)
        self.assertEqual(third["queue"][0]["added_source"], "registry")
        self.assertEqual(third["queue"][0]["added_field"], "registered_at")
        self.assertTrue(third["queue"][0]["added_at"].startswith("2026-11-10T12:00:00"))
        self.assertNotIn("2020-01-01", third["queue"][0]["added_at"])


    def test_policy_approvals_show_in_log_and_ticketless_flag(self):
        from operations.archive_droid import collect, render
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        state, repo, work, view = root / "state", root / "repo", root / "work", root / "view"
        for d in (repo / "ops/wayback", state / "registry", work, view):
            d.mkdir(parents=True)
        (repo / "ops/wayback/queue.txt").write_text("")
        (repo / "ops/wayback/done.json").write_text("{}")
        (state / "registry/approved-objects.json").write_text(json.dumps({"object_ids": ["a" * 64]}))
        lines = [{"object_id": "b" * 64, "ticket": 1310, "at": "2026-11-25T21:00:00+00:00"},
                 {"object_id": "c" * 64, "ticket": 1310, "at": "2026-11-25T21:00:00+00:00"},
                 {"object_id": "d" * 64, "ticket": 615, "at": "2026-11-25T21:00:00+00:00"},
                 {"object_id": "b" * 64, "ticket": 1310, "at": "2026-11-25T22:00:00+00:00"}]
        (state / "registry/policy-approvals.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        (state / "registry/policy-unapproved.json").write_text(json.dumps({"no_ticket": 7, "checked_at": "2026-11-25T21:00:00+00:00"}))
        data = collect.build_now(collect.Paths(mock.Mock(state=state, repo=repo, work=work, view=view)))
        self.assertEqual(data["approved_ids"], 4)
        self.assertEqual(data["approved_policy"], 3)
        rows = [r for r in data["approvals_log"] if r.get("source") == "worker policy"]
        self.assertEqual(sorted((r["ticket"], r["count"]) for r in rows), [(615, 1), (1310, 2)])
        self.assertEqual(max(r["total_after"] for r in rows), 4)
        self.assertTrue(any("7 ready registered items have no ticket number" in w for w in data["warnings"]))
        render.write_now(view, data)
        html = (view / "index.html").read_text(encoding="utf-8")
        self.assertIn("worker policy", html)
        self.assertIn("auto-approved 2", html)

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
        snapshot = "https://swccg.com/snapshots/github/owner/billbisco/repo/file.html"
        wayback_snap = "https://web.archive.org/web/20260101000000/" + snapshot
        self.assertEqual(render.safe_link(snapshot), '<span class="muted">private source</span>')
        self.assertEqual(render.safe_link(wayback_snap, "Wayback"), '<span class="muted">private source</span>')
        self.assertNotIn("billbisco", render.safe_link(snapshot))
        self.assertNotIn("billbisco", render.safe_link(wayback_snap, "Wayback"))


if __name__ == "__main__":
    unittest.main()
