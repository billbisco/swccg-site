"""Static, script-free HTML for the unlisted, read-only Archive Droid page."""
from __future__ import annotations

import html
import re
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from operations.archive_droid import core

CARACAS = timezone(timedelta(hours=-4))
TICKET_URL = "https://github.com/swccg-guild/work/issues/{}"
WIKI = "https://wiki.swccg.com"
PAGE_SIZE = 200
BASE = "/admin/operations/archive-droid/"

HOW_QUEUE = """
<div class="how" id="how-queue">
<h3>How this queue works (for every AI)</h3>
<p>This queue is the worker's ordered list of URLs waiting to be saved and verified on the Wayback Machine. Every AI adds new work the same way: register it with the sanctioned helper and a ticket number. The worker then auto-approves it and appends it to the back of the queue. Do not edit the queue by hand.</p>
<p><b>To add an item</b> from the swccg-wiki repo:</p>
<code class="cmd">python3 tools/preservation_enqueue.py REQUEST.json --registry /root/.local/state/swccg-wayback/registry</code>
<p>The request must be <code>schema_version</code> 1 and include subject, source (<code>url</code>, <code>sha256</code>, <code>bytes</code>, and <code>checked_evidence</code>), readiness, owner, continuation, <code>approval_scope</code> with the work <b>ticket number</b>, and <code>backup_receipt</code> (plus <code>source_backup_receipt</code> when the source and the page are different). Do not put credentials in the request. Field rules are in <code>ops/PRESERVATION-REGISTRATION.md</code> and in the swccg-wiki skill's <code>archiving.md</code>.</p>
<p><b>What that command actually does.</b> It sends metadata to the server registration inbox only. It does not submit a capture, and it does not itself append a line to <code>queue.txt</code>. The receipt always says <code>submitted: false</code>. Unready items (missing backup, missing source proof, or an unready stage) stay in the registry and do not enter this queue.</p>
<p><b>Approval and the back of the queue.</b> Approval is automatic (Bill, 25 Nov 2026). The worker auto-approves every registration that is ready (backup receipt and source proof in place) and carries a work ticket number, within about an hour. It records each approval with its date and ticket in <a href="#approved">Recently approved</a>. Ready items without a ticket stay unapproved and are flagged on this page. The stale 14 Nov 2026 hold stamp was released on 26 Nov 2026 (<a href="https://github.com/swccg-guild/work/issues/1375">#1375</a>); only exact-byte duplicates and files gone from the wiki were kept out. Items not yet ready are not approved. AIs do not hand-write approvals or edit <code>approved-objects.json</code>. Each worker run <b>sizes its own promotion</b>: it promotes enough approved jobs to keep a few hours of fresh work in the queue (see <a href="#speed">Speed</a>) and, when they pass the public-byte checks, <b>appends their URLs to the back of queue.txt</b> in one commit. Existing lines are not reordered. After your story's items are registered with a ticket, that story's archiving step is done; Archive Droid and standing ticket <a href="https://github.com/swccg-guild/work/issues/1359">#1359</a> own approval, promotion, saving, verification and linking.</p>
<p><b>Save, verify, and Preservation links.</b> The worker then submits queued URLs to archive.org one at a time, confirms each job, and records the result in <code>done.json</code>. It sets its own pace from archive.org's answers: faster after clean runs, slower after refusals, 429s or cooldowns (it always honors Retry-After). Outside sources with no other copy (forum, res.starwarsccg.org, other sites) go before our own files that already have a verified archive.org item copy. After a capture is <code>verified</code>, the linker adds a Preservation link on the matching wiki File page, or a Preserved copy on a citing page for snapshots.</p>
<p><b>Archive.org item copies count.</b> Our own files are also uploaded straight into our archive.org items (<code>swccg-wiki-images-0</code>…<code>-f</code>, <code>-archive</code>, <code>swccg-com-snapshots</code>). A file whose SHA-1, as archive.org reports it, matches ours is an archive.org backup, and the linker links that copy on its File page, so a File page does not wait for a Wayback capture (<a href="https://github.com/swccg-guild/work/issues/1375">#1375</a> D2).</p>
<p><b>How to read Stage in this table:</b> <i>new</i> — in the queue, not yet tried; <i>submitted</i> — sent to archive.org, waiting; <i>ok</i> — a capture exists, verification still pending; <i>unverified</i> — a capture loaded but is not yet accepted as a match. Failed and retry items are in <a href="#problems">Problems</a>. Verified items are in <a href="#completed">Completed / Verified</a>.</p>
<p><b>Do not:</b> edit <code>queue.txt</code> or <code>done.json</code> by hand; reorder the queue; make a direct archive.org save (including <code>web.archive.org/save</code>) outside this pipeline; write approvals by hand; register work without a ticket number.</p>
<p>Details: <code>archiving.md</code> and <a href="https://github.com/swccg-guild/work/issues/1359">#1359</a>.</p>
</div>
"""

CSS = """
:root{--bg:#0d1726;--panel:#14223a;--line:#24385a;--text:#e8eefb;--muted:#9fb0cc;--accent:#f0c040;--bad:#ff8080;--ok:#7fd88f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}
main{max-width:1200px;margin:auto;padding:28px 20px}a{color:#8cc4ff}h1{margin:0 0 4px}h2{margin:0 0 10px;font-size:1.15rem}
.sub{color:var(--muted);margin:0 0 18px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:0 0 18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}.tile{border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.tile b{display:block;font-size:1.2rem}.tile span{color:var(--muted);font-size:.8rem}table{width:100%;border-collapse:collapse;font-size:.86rem}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}
tr.cur{background:#3a3210}tr.cur td:first-child::before{content:"\\25B6 ";color:var(--accent)}.warn{color:var(--bad)}.okc{color:var(--ok)}
.muted{color:var(--muted)}.thumbs{display:flex;flex-wrap:wrap;gap:10px}.thumb{width:140px;font-size:.72rem;word-break:break-all}
.thumb img{width:140px;height:140px;object-fit:contain;background:#0a1220;border:1px solid var(--line);border-radius:6px;display:block}
nav a{margin-right:14px}.scroll{max-height:520px;overflow:auto}code{font-size:.8rem}
.how{border:1px solid var(--accent);border-radius:8px;padding:12px 16px;margin:0 0 14px;background:#1a2740}
.how h3{margin:0 0 8px;font-size:1.05rem}
.how p{margin:0 0 8px}
.how ul{margin:0 0 8px;padding-left:1.2em}
.how li{margin:0 0 4px}
.how .cmd{display:block;margin:6px 0;white-space:pre-wrap}
"""


def e(x) -> str:
    return html.escape("" if x is None else str(x))

# Hosted on a personal or private GitHub account — never write the URL into the page.
_PRIVATE_REPO = re.compile(
    r"(?i)(?:(?:^|://)(?:www\.)?(?:github\.com|raw\.githubusercontent\.com)/billbisco(?:/|$)"
    r"|/snapshots/github/(?:[^/?#]+/)*billbisco(?:/|$|\.)"
    r"|%2Fbillbisco(?:%2F|$))"
)


def safe_link(url, label=None) -> str:
    """Link to a public URL, or a label with no href if the URL names a private repo."""
    if not url:
        return e(label or "")
    if _PRIVATE_REPO.search(str(url)):
        return '<span class="muted">private source</span>'
    text = label if label is not None else url
    return f'<a href="{e(url)}">{e(text)}</a>'




def when(value) -> str:
    if value in (None, "", "n/a"):
        return "—"
    try:
        if isinstance(value, (int, float)):
            t = datetime.fromtimestamp(value, timezone.utc)
        else:
            t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except ValueError:
        return e(value)
    return e(t.astimezone(CARACAS).strftime("%a %d %b %Y %H:%M")) + " <span class=muted>(UTC-4)</span>"


def when_known(value) -> str:
    """Format a real timestamp, or 'unknown' when there isn't one. Never invents a date."""
    if value in (None, "", "n/a"):
        return "unknown"
    text = when(value)
    return "unknown" if text == "—" else text


def added_cell(r) -> str:
    """Date added with its source. Never invents a date."""
    at = r.get("added_at")
    src = r.get("added_source") or ""
    field = r.get("added_field") or ""
    if not at:
        return "unknown"
    date = when_known(at)
    if date == "unknown":
        return "unknown"
    if src == "on-or-before":
        label = "on or before"
    elif src == "first-seen":
        label = "first seen by page"
    elif src == "registry":
        label = f"registry ({field})" if field else "registry"
    else:
        label = src or "unknown"
    return f'{date} <span class="muted">{e(label)}</span>'


def tlink(ticket) -> str:
    if ticket in (None, "", "untraced"):
        return '<span class="muted">untraced</span>'
    return f'<a href="{TICKET_URL.format(int(ticket))}">#{int(ticket)}</a>'


def page(title: str, body: str, *, refresh: int | None = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">{meta}'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex, nofollow">'
            f'<meta name="referrer" content="no-referrer"><title>{e(title)}</title>'
            f'<style>{CSS}</style></head><body><main>{body}</main></body></html>')


def atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ad-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _next_run(data) -> str:
    svc, tmr = data.get("service") or {}, data.get("timer") or {}
    if svc.get("ActiveState") in ("active", "activating"):
        return "running now"
    end = svc.get("ExecMainExitTimestamp")
    try:
        p = end.split()
        t = datetime.strptime(f"{p[1]} {p[2]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) + timedelta(minutes=1)
        return when(t.isoformat()) + ' <span class="muted">(1 min after the last run ends)</span>'
    except (AttributeError, IndexError, ValueError):
        return e(tmr.get("NextElapseUSecRealtime") or "—")


def _sys_when(v):
    try:
        p = v.split()
        return when(datetime.strptime(f"{p[1]} {p[2]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat())
    except (AttributeError, IndexError, ValueError):
        return "—"



def _pos(r) -> str:
    pval = r.get("pos")
    return f"{int(pval):,}" if isinstance(pval, int) and pval > 0 else "—"


def _waiting_rows(rows, *, problems: bool = False) -> str:
    out = []
    for r in rows:
        cls = " ".join(x for x in ("cur" if r.get("current") else "", "warn" if problems else "") if x)
        attr = f' class="{cls}"' if cls else ""
        out.append(
            f'<tr{attr}><td>{_pos(r)}</td><td>{safe_link(r.get("url"))}</td>'
            f'<td>{tlink(r.get("ticket"))}</td><td>{e(r.get("stage"))}</td>'
            f'<td>{added_cell(r)}</td><td>{when_known(r.get("last"))}</td></tr>'
        )
    return "".join(out) or '<tr><td colspan=6 class="muted">None.</td></tr>'


def _completed_rows(rows, **_k) -> str:
    out = []
    for r in rows:
        cap = r.get("capture") or ""
        cap_html = safe_link(cap, "Wayback") if cap else "unknown"
        cls = ' class="cur"' if r.get("current") else ""
        out.append(
            f'<tr{cls}><td>{when_known(r.get("verified_at"))}</td><td>{safe_link(r.get("url"))}</td>'
            f'<td>{tlink(r.get("ticket"))}</td><td>{cap_html}</td>'
            f'<td>{e(r.get("preservation") or "unknown")}</td></tr>'
        )
    return "".join(out) or '<tr><td colspan=5 class="muted">None yet.</td></tr>'


def _pager(folder: str, n: int, pages: int, total: int, noun: str) -> str:
    if pages <= 1:
        return f'<p class="muted">{total:,} {noun} items.</p>'
    links = " ".join(
        (f"<b>{i}</b>" if i == n else f'<a href="{folder}/{i}.html">{i}</a>')
        for i in range(1, pages + 1)
    )
    return (f'<p class="muted">Showing {(n-1)*PAGE_SIZE + 1:,}–{min(n*PAGE_SIZE, total):,} of {total:,} {noun} items. '
            f"Pages: {links}</p>")


def write_list_pages(view: Path, folder: str, title: str, rows: list, blurb: str, rowfn, headers, **rowkw) -> None:
    out = view / folder
    out.mkdir(parents=True, exist_ok=True)
    pages = max(1, -(-len(rows) // PAGE_SIZE)) if rows else 1
    keep = set()
    heads = "".join(f"<th>{e(h)}</th>" for h in headers)
    for n in range(1, pages + 1):
        chunk = rows[(n - 1) * PAGE_SIZE:n * PAGE_SIZE]
        # These files live inside folder/, so pager links are ./N.html.
        nav = _pager(".", n, pages, len(rows), title.lower())
        body = (
            f'<h1>{e(title)} ({len(rows):,})</h1>'
            f'<p class="sub">{e(blurb)} <a href="../#{folder}">← Archive Droid</a></p>'
            f"{nav}"
            f'<div class="panel"><div class="scroll"><table><tr>{heads}</tr>{rowfn(chunk, **rowkw)}</table></div></div>'
            f"{nav}"
        )
        name = f"{n}.html"
        keep.add(name)
        atomic(out / name, page(f"Archive Droid {title} p.{n}", body))
    for oldp in out.glob("*.html"):
        if oldp.name not in keep:
            oldp.unlink()


def _num(v, fmt="{:,}") -> str:
    if isinstance(v, bool) or v is None:
        return "—"
    if isinstance(v, (int, float)):
        return e(fmt.format(v))
    return e(v)


def speed_html(d: dict) -> str:
    """Archive Droid sets its own speed (#1375 D4): pace-state.json + pace-log.jsonl tail."""
    sp = d.get("speed") if isinstance(d.get("speed"), dict) else None
    if not sp:
        return ('<p>Archive Droid sets its own speed (<a href="https://github.com/swccg-guild/work/issues/1375">#1375</a>). '
                '<span class="muted">Self-tuning starts with the next worker run; no pace state recorded yet.</span></p>')
    b = sp.get("bounds") if isinstance(sp.get("bounds"), dict) else {}
    lr = sp.get("last_run") if isinstance(sp.get("last_run"), dict) else {}
    pr = sp.get("promotion") if isinstance(sp.get("promotion"), dict) else {}
    tiles = [
        ("Submits allowed per run", f'{_num(sp.get("allowance"))} <span class="muted">(bounds {_num(b.get("min"))}–{_num(b.get("max"))})</span>'),
        ("Gap between submits", f'{_num(sp.get("gap_s"), "{:g}")} s <span class="muted">(floor {_num(b.get("gap_min_s"), "{:g}")} s)</span>'),
        ("Last run", f'{_num(lr.get("submits"))} submits, {_num(lr.get("refusals"))} refusals'
                     f'{", rate-limited" if lr.get("rate_limited") else ""}'
                     f'<span class="muted">{"; used its whole allowance" if lr.get("used_full_allowance") else ""}; ended {when(lr.get("ended"))}</span>'),
        ("Promotion last run", f'{_num(pr.get("promoted"))} jobs <span class="muted">(buffer {_num(pr.get("buffer_before"))} → target {_num(pr.get("target_buffer"))}, cap {_num(pr.get("cap"))}, {_num(pr.get("seconds"), "{:.0f}")} s)</span>'),
    ]
    tile_html = "".join(f'<div class="tile"><span>{e(k)}</span><b>{v}</b></div>' for k, v in tiles)
    log = list(reversed(d.get("speed_log") or []))
    lrows = "".join(
        f'<tr><td>{when(x.get("at"))}</td><td>{_num(x.get("from"))} → {_num(x.get("to"))}</td>'
        f'<td>{_num(x.get("gap_from"), "{:g}")} s → {_num(x.get("gap_to"), "{:g}")} s</td><td class="muted">{e(x.get("reason"))}</td></tr>'
        for x in log if isinstance(x, dict)) or '<tr><td colspan=4 class="muted">No pace change yet.</td></tr>'
    return (f'<p>Archive Droid sets its own speed (<a href="https://github.com/swccg-guild/work/issues/1375">#1375</a>): '
            f'it raises the allowance after clean runs and backs off on refusals, 429s and cooldowns. '
            f'Why it is at this pace now: <b>{e(sp.get("reason") or "—")}</b> <span class="muted">(updated {when(sp.get("updated_at") or lr.get("ended"))})</span></p>'
            f'<div class="grid">{tile_html}</div>'
            f'<h3>Recent pace changes</h3><table><tr><th>When</th><th>Submits per run</th><th>Gap</th><th>Why</th></tr>{lrows}</table>')


def ia_html(d: dict) -> str:
    ia = d.get("ia") if isinstance(d.get("ia"), dict) else {}
    if not ia.get("available"):
        return '<p class="muted">No archive.org item ledger found.</p>'
    g = ia.get("groups") or {}
    rows = "".join(f'<tr><td>{e(k)}</td><td>{_num(v[0])}</td><td>{_num(v[1])}</td></tr>'
                   for k, v in g.items() if isinstance(v, list) and len(v) == 2)
    links = ia.get("links") if isinstance(ia.get("links"), dict) else None
    if links:
        lk = (f'<p>File pages linked to their item copy: <b>{_num(links.get("linked"))}</b> '
              f'<span class="muted">(verified current wiki files {_num(links.get("ia_verified_current"))}, still to link {_num(links.get("todo"))}, '
              f'skipped because the SHA-1 differs {_num(links.get("skipped_sha_mismatch"))}, edits last run {_num(links.get("last_run_edits"))})</span></p>')
    else:
        lk = '<p class="muted">File-page links to item copies start once the linker update is deployed (#1375 D2).</p>'
    return (f'<p>Our own files uploaded straight into archive.org items: <b>{_num(ia.get("uploaded"))}</b>, '
            f'of which <b>{_num(ia.get("verified"))}</b> have the SHA-1 archive.org reports matching ours. These count as the archive.org backup.</p>'
            f'<table><tr><th>Item group</th><th>Uploaded</th><th>SHA-1 verified</th></tr>{rows}</table>{lk}')


def write_now(view: Path, d: dict) -> None:
    h, cap, svc = d.get("health") or {}, d.get("capacity") or {}, d.get("service") or {}
    cur = d.get("current") or {}
    stages = d.get("stages") or {}
    warn = d.get("warnings") or []
    waiting = []
    speed = d.get("speed") if isinstance(d.get("speed"), dict) else None
    promo = (speed or {}).get("promotion") if isinstance((speed or {}).get("promotion"), dict) else None
    if h.get("reason"):
        why = f"Worker: {e(h['reason'])}"
        if h.get("reason") == "account capacity unavailable":
            ev = h.get("provider_event")
            why += (f" — at its last check ({when(h.get('checked_at'))}) the worker got no capacity answer from archive.org"
                    + (f" (<code>{e(ev)}</code>)" if ev else "") + ", so it waits. The free slots tile is archive.org's last real answer, "
                    f"from {when(h.get('capacity_checked_at'))}.")
        waiting.append(why)
    anq = stages.get("approved-not-queued")
    if anq:
        if promo:
            waiting.append(f"{anq:,} approved files are waiting to be promoted. Last run promoted {int(promo.get('promoted') or 0):,} "
                           f"(queue buffer {int(promo.get('buffer_before') or 0):,} fresh items, target {int(promo.get('target_buffer') or 0):,}, "
                           f"cap {int(promo.get('cap') or 0):,} per run).")
        else:
            waiting.append(f"{anq:,} approved files are waiting to be promoted. The worker sizes promotion itself; "
                           "self-tuning starts with the next worker run.")
    ru = stages.get("ready-unapproved")
    if ru:
        waiting.append(f"{ru:,} ready files are not approved yet. The worker auto-approves ready items that have a ticket number; the rest need one.")
    hm = d.get("hold_marked")
    if isinstance(hm, int):
        waiting.append(f'{hm:,} registry objects still carry the 14 Nov 2026 hold stamp (policy scan {when(d.get("policy_checked_at"))}). '
                       f'The stamp was released on 26 Nov ({tlink(1375)}); 491 were kept out on purpose: 488 exact-byte duplicates and 3 files gone from the wiki.')
    tiles = [
        ("Health", e(h.get("mode") or "unknown")),
        ("Last run started", _sys_when(svc.get("ExecMainStartTimestamp"))),
        ("Last run ended", _sys_when(svc.get("ExecMainExitTimestamp")) + f' <span class="muted">{e(svc.get("Result") or "")}</span>'),
        ("Next run", _next_run(d)),
        ("Next submission eligible", when(d.get("next_submission_at") or h.get("next_eligible_attempt_at"))),
        ("Last verified capture", when(h.get("last_verified_progress_at"))),
        ("Captures today", f'{cap.get("daily_captures") or 0:,} / {cap.get("daily_captures_limit") or 0:,}'),
        ("Account slots free", e(cap.get("available")) + f' <span class="muted">as of {when(h.get("capacity_checked_at"))}</span>'),
        ("Waiting in queue", f'{d.get("queue_len", 0):,}'),
        ("Problems", f'{len(d.get("problems") or []):,}'),
        ("Verified captures", f'{len(d.get("completed") or []):,}'),
        ("Approved object IDs", f'{d.get("approved_ids", 0):,} <span class="muted">({d.get("approved_policy", 0):,} auto)</span>'),
    ]
    tile_html = "".join(f'<div class="tile"><span>{e(k)}</span><b>{v}</b></div>' for k, v in tiles)
    cur_html = (f'<p><b>{e(cur.get("event"))}</b> {safe_link(cur.get("url"))} at {when(cur.get("at"))}</p>'
                if cur else '<p class="muted">Between items: no save or check line in the recent worker journal.</p>')
    warn_html = ("".join(f'<li class="warn">{e(w)}</li>' for w in warn) if warn else '<li class="okc">No threshold breached.</li>')
    iab = d.get("ia_by_stage") or {}
    stage_rows = "".join(f'<tr><td>{e(s)}</td><td>{stages.get(s, 0):,}</td>'
                         f'<td>{(f"{iab[s]:,}" if isinstance(iab.get(s), int) else "—")}</td><td class="muted">{e(desc)}</td></tr>'
                         for s, desc in core.STAGES) if stages else '<tr><td colspan=4 class="muted">Waiting for the first nightly sweep.</td></tr>'
    # Queue / Problems / Completed
    q = d.get("queue") or []
    problems = d.get("problems") or []
    completed = d.get("completed") or []
    qrows = _waiting_rows(q[:1000])
    more = f'<p class="muted">Showing the first 1,000 of {len(q):,} waiting rows.</p>' if len(q) > 1000 else ""
    prows = _waiting_rows(problems[:PAGE_SIZE], problems=True)
    p_pages = max(1, -(-len(problems) // PAGE_SIZE)) if problems else 1
    pmore = _pager("problems", 1, p_pages, len(problems), "problem") if len(problems) > PAGE_SIZE else ""
    crows = _completed_rows(completed[:PAGE_SIZE])
    c_pages = max(1, -(-len(completed) // PAGE_SIZE)) if completed else 1
    cmore = _pager("completed", 1, c_pages, len(completed), "verified") if len(completed) > PAGE_SIZE else ""
    # Approvals
    log = sorted(d.get("approvals_log") or [], key=lambda x: x.get("at") or "", reverse=True)
    arows = "".join(
        f'<tr><td>{when(x.get("at"))}</td><td>{tlink(x.get("ticket"))}</td><td>{e(x.get("change"))} {int(x.get("count") or 0):,}</td>'
        f'<td>{int(x.get("total_after") or 0):,}</td><td class="muted">{e(x.get("source"))}</td><td class="muted">{e((x.get("reason") or "")[:180])}</td></tr>'
        for x in log[:200]) or '<tr><td colspan=6 class="muted">No approvals recorded.</td></tr>'
    # Missing summary
    groups = {}
    try:
        groups = json.loads((view / "missing" / "groups.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    mrows = "".join(
        f'<tr><td>{tlink(g if g != "untraced" else None)}</td><td><a href="missing/{e(g)}-1.html">{n:,} items</a></td></tr>'
        for g, n in groups.get("groups", {}).items()) or '<tr><td colspan=2 class="muted">Waiting for the first nightly sweep.</td></tr>'
    body = f"""
<h1>Archive Droid</h1>
<p class="sub">Unlisted, read-only. Generated {when(d.get("generated_at"))}; this page reloads every 5 minutes.
Nightly sweep: {when(d.get("sweep_generated_at"))}. Standing ticket {tlink(1359)}. <a href="/admin/operations/">← Operations overview</a></p>
<nav><a href="#now">Now</a><a href="#queue">Queue</a><a href="#problems">Problems</a><a href="#completed">Completed</a><a href="#approved">Recently approved</a><a href="#missing">Missing a Wayback link</a></nav>
<section class="panel" id="now"><h2>Now</h2><div class="grid">{tile_html}</div>
<h3 id="speed">Speed</h3>{speed_html(d)}
<h3 id="items">Archive.org item copies</h3>{ia_html(d)}
<h3>Current or last item</h3>{cur_html}
<h3>Why it is waiting</h3><ul>{"".join(f"<li>{w}</li>" for w in waiting) or "<li>Nothing is waiting.</li>"}</ul>
<h3>Stall checks</h3><ul>{warn_html}</ul>
<h3>Every wiki file, by stage</h3><table><tr><th>Stage</th><th>Files</th><th>With a verified item copy</th><th>Meaning</th></tr>{stage_rows}</table>
<p class="muted">"With a verified item copy": files in that stage whose current version is already SHA-1 verified in our archive.org items (from the nightly sweep).</p></section>
<section class="panel" id="queue"><h2>Queue ({len(q):,})</h2>
{HOW_QUEUE}
<p class="muted">Still waiting, in worker queue order (new, submitted, ok, unverified). Verified items are in <a href="#completed">Completed</a>; failed and retry are in <a href="#problems">Problems</a>. ▶ marks the item the worker most recently submitted or checked. <b>Date added</b> is First seen by page: the first time this page's 5-minute collector recorded the item. The earliest first-seen records start today. Items already in the queue then are marked <i>on or before</i> that time, unless a real registry time (registered_at or request time) exists. The source of each date is shown; dates are never invented. Last tried is done.json <code>last</code>. Times are UTC-4.</p>
<div class="scroll"><table><tr><th>#</th><th>File or URL</th><th>Ticket</th><th>Stage</th><th>Date added</th><th>Last tried</th></tr>{qrows}</table></div>{more}</section>
<section class="panel" id="problems"><h2>Problems ({len(problems):,})</h2><p class="muted">Failed and retry items. Queue-order first, then any that have left queue.txt. Date added is First seen by page, with the same sources as the Queue table. Last tried is done.json <code>last</code>.</p>
<div class="scroll"><table><tr><th>#</th><th>File or URL</th><th>Ticket</th><th>Stage</th><th>Date added</th><th>Last tried</th></tr>{prows}</table></div>{pmore}</section>
<section class="panel" id="completed"><h2 id="verified">Completed / Verified ({len(completed):,})</h2><p class="muted">Newest verified capture first. Verified date is done.json <code>verify.at</code> (UTC-4). The Wayback column is the recorded capture URL; Preservation link: <i>yes</i> when the linker recorded a link on the File page (Wayback or item copy) or on the citing pages for a forum snapshot; <i>not yet</i> when one is still due; <i>not needed</i> for kinds that have no File page (wiki pages, outside documents).</p>
<div class="scroll"><table><tr><th>Verified date (UTC-4)</th><th>File or URL</th><th>Ticket</th><th>archive.org</th><th>Preservation link</th></tr>{crows}</table></div>{cmore}</section>
<section class="panel" id="approved"><h2>Recently approved</h2><p class="muted">Append-only log. Operator approvals come from approved-objects.json (past entries rebuilt from its scopes, dated backups and file times; new changes recorded by a hook). Since 25 Nov 2026 the worker also auto-approves ready registrations that carry a work ticket number; those rows show Source "worker policy". This page never edits what is approved.</p>
<div class="scroll"><table><tr><th>When</th><th>Ticket</th><th>Change</th><th>Total after</th><th>Source</th><th>Reason</th></tr>{arows}</table></div></section>
<section class="panel" id="missing"><h2>Missing a Wayback link</h2><p class="muted">Every wiki file without a verified copy and Preservation link, plus registered snapshots not yet verified, grouped by originating ticket. Rebuilt nightly; generated {when(groups.get("generated_at"))}.</p>
<table><tr><th>Ticket</th><th>Items</th></tr>{mrows}</table></section>
"""
    atomic(view / "index.html", page("Archive Droid", body, refresh=300))
    write_list_pages(view, "problems", "Problems", problems,
                     "Failed and retry items. Queue-order first, then any that have left queue.txt. Date added is First seen by page.",
                     _waiting_rows, ("#", "File or URL", "Ticket", "Stage", "Date added", "Last tried"),
                     problems=True)
    write_list_pages(view, "completed", "Completed / Verified", completed,
                     "Newest verified capture first. Verified date is done.json verify.at (UTC-4).",
                     _completed_rows, ("Verified date (UTC-4)", "File or URL", "Ticket", "archive.org", "Preservation link"))


def _thumb(it) -> str:
    title = it.get("title")
    link = it.get("desc") or (f"{WIKI}/wiki/File:{quote(title)}" if title else it.get("url"))
    if title and (it.get("mime") or "").startswith("image/"):
        # Never point a thumb at a private repo, even if the title looks like a File: name.
        if link and _PRIVATE_REPO.search(str(link)):
            return ""
        src = f"{WIKI}/wiki/Special:FilePath/{quote(title)}?width=140"
        return (f'<div class="thumb"><a href="{e(link)}"><img loading="lazy" src="{e(src)}" alt=""></a>'
                f'<a href="{e(link)}">{e(title)}</a><br><span class="muted">{e(it.get("stage"))}</span></div>')
    return ""


def write_missing(view: Path, groups: dict, summary: dict) -> None:
    out = view / "missing"
    out.mkdir(parents=True, exist_ok=True)
    keep = set()
    for g, items in groups.items():
        items = sorted(items, key=lambda x: (x.get("stage") or "", x.get("title") or x.get("url") or ""))
        pages = max(1, -(-len(items) // PAGE_SIZE))
        for n in range(1, pages + 1):
            chunk = items[(n - 1) * PAGE_SIZE:n * PAGE_SIZE]
            thumbs = "".join(_thumb(it) for it in chunk)
            texts = "".join(
                f'<li>{safe_link(it.get("desc") or it.get("url"), it.get("title") or it.get("url"))} '
                f'<span class="muted">{e(it.get("stage"))}</span></li>'
                for it in chunk if not _thumb(it))
            nav = " ".join(f'<a href="{e(g)}-{i}.html">{i}</a>' if i != n else f"<b>{i}</b>" for i in range(1, pages + 1))
            body = (f'<h1>Missing a Wayback link: {tlink(g if g != "untraced" else None)}</h1>'
                    f'<p class="sub">{len(items):,} items. Page {n} of {pages}. Generated {when(summary.get("generated_at"))}. '
                    f'<a href="../">← Archive Droid</a></p><p>{nav}</p>'
                    + (f'<div class="panel"><div class="thumbs">{thumbs}</div></div>' if thumbs else "")
                    + (f'<div class="panel"><ul>{texts}</ul></div>' if texts else "")
                    + f"<p>{nav}</p>")
            name = f"{g}-{n}.html"
            keep.add(name)
            atomic(out / name, page(f"Archive Droid missing {g}", body))
    for old in out.glob("*.html"):
        if old.name not in keep:
            old.unlink()
    atomic(out / "groups.json", json.dumps({"generated_at": summary.get("generated_at"),
                                            "groups": {g: len(v) for g, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}}))
