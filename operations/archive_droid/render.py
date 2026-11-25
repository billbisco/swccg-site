"""Static, script-free HTML for the unlisted, read-only Archive Droid page."""
from __future__ import annotations

import html
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
"""


def e(x) -> str:
    return html.escape("" if x is None else str(x))


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


def write_now(view: Path, d: dict) -> None:
    h, cap, svc = d.get("health") or {}, d.get("capacity") or {}, d.get("service") or {}
    cur = d.get("current") or {}
    stages = d.get("stages") or {}
    warn = d.get("warnings") or []
    waiting = []
    if h.get("reason"):
        waiting.append(f"Worker: {e(h['reason'])}")
    anq = stages.get("approved-not-queued")
    if anq:
        waiting.append(f"{anq:,} approved files are waiting to be promoted (the worker promotes 2 per run).")
    ru = stages.get("ready-unapproved")
    if ru:
        waiting.append(f"{ru:,} ready files have no approval yet.")
    held = stages.get("held")
    if held:
        waiting.append(f'{held:,} files still carry the 14 Nov hold stamp ({tlink(1358)} decides).')
    tiles = [
        ("Health", e(h.get("mode") or "unknown")),
        ("Last run started", _sys_when(svc.get("ExecMainStartTimestamp"))),
        ("Last run ended", _sys_when(svc.get("ExecMainExitTimestamp")) + f' <span class="muted">{e(svc.get("Result") or "")}</span>'),
        ("Next run", _next_run(d)),
        ("Next submission eligible", when(d.get("next_submission_at") or h.get("next_eligible_attempt_at"))),
        ("Last verified capture", when(h.get("last_verified_progress_at"))),
        ("Captures today", f'{cap.get("daily_captures") or 0:,} / {cap.get("daily_captures_limit") or 0:,}'),
        ("Account slots free", e(cap.get("available"))),
        ("Queue length", f'{d.get("queue_len", 0):,}'),
        ("Approved object IDs", f'{d.get("approved_ids", 0):,}'),
    ]
    tile_html = "".join(f'<div class="tile"><span>{e(k)}</span><b>{v}</b></div>' for k, v in tiles)
    cur_html = (f'<p><b>{e(cur.get("event"))}</b> <a href="{e(cur.get("url"))}">{e(cur.get("url"))}</a> at {when(cur.get("at"))}</p>'
                if cur else '<p class="muted">No recent item in the worker journal.</p>')
    warn_html = ("".join(f'<li class="warn">{e(w)}</li>' for w in warn) if warn else '<li class="okc">No threshold breached.</li>')
    stage_rows = "".join(f'<tr><td>{e(s)}</td><td>{stages.get(s, 0):,}</td><td class="muted">{e(desc)}</td></tr>'
                         for s, desc in core.STAGES) if stages else '<tr><td colspan=3 class="muted">Waiting for the first nightly sweep.</td></tr>'
    # Queue
    q = d.get("queue") or []
    qrows = "".join(
        f'<tr class="{"cur" if r.get("current") else ""}"><td>{r["pos"]:,}</td><td><a href="{e(r["url"])}">{e(r["url"])}</a></td>'
        f'<td>{tlink(r.get("ticket"))}</td><td>{e(r.get("stage"))}</td></tr>' for r in q[:1000])
    more = f'<p class="muted">Showing the first 1,000 of {len(q):,} rows.</p>' if len(q) > 1000 else ""
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
<nav><a href="#now">Now</a><a href="#queue">Queue</a><a href="#approved">Recently approved</a><a href="#missing">Missing a Wayback link</a></nav>
<section class="panel" id="now"><h2>Now</h2><div class="grid">{tile_html}</div>
<h3>Current or last item</h3>{cur_html}
<h3>Why it is waiting</h3><ul>{"".join(f"<li>{w}</li>" for w in waiting) or "<li>Nothing is waiting.</li>"}</ul>
<h3>Stall checks</h3><ul>{warn_html}</ul>
<h3>Every wiki file, by stage</h3><table><tr><th>Stage</th><th>Files</th><th>Meaning</th></tr>{stage_rows}</table></section>
<section class="panel" id="queue"><h2>Queue ({len(q):,})</h2><p class="muted">queue.txt order. ▶ marks the item the worker most recently submitted or checked. Stage is the done.json status ("new" = not tried yet).</p>
<div class="scroll"><table><tr><th>#</th><th>File or URL</th><th>Ticket</th><th>Stage</th></tr>{qrows}</table></div>{more}</section>
<section class="panel" id="approved"><h2>Recently approved</h2><p class="muted">Append-only log. Past entries were rebuilt from approved-objects.json scopes, its dated backups and their file times. New changes are recorded by a hook when the file changes. The log never edits what is approved.</p>
<div class="scroll"><table><tr><th>When</th><th>Ticket</th><th>Change</th><th>Total after</th><th>Source</th><th>Reason</th></tr>{arows}</table></div></section>
<section class="panel" id="missing"><h2>Missing a Wayback link</h2><p class="muted">Every wiki file without a verified copy and Preservation link, plus registered snapshots not yet verified, grouped by originating ticket. Rebuilt nightly; generated {when(groups.get("generated_at"))}.</p>
<table><tr><th>Ticket</th><th>Items</th></tr>{mrows}</table></section>
"""
    atomic(view / "index.html", page("Archive Droid", body, refresh=300))


def _thumb(it) -> str:
    title = it.get("title")
    link = it.get("desc") or (f"{WIKI}/wiki/File:{quote(title)}" if title else it.get("url"))
    if title and (it.get("mime") or "").startswith("image/"):
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
                f'<li><a href="{e(it.get("desc") or it.get("url"))}">{e(it.get("title") or it.get("url"))}</a> '
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
