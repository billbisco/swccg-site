"""Pure helpers: URL normalisation, stage classification, approval-log diffs.

Nothing here touches the filesystem or network; see collect.py for I/O.
"""
from __future__ import annotations

import re
from urllib.parse import unquote

HOLD_MARK = "Bill all-agent Archive.org hold 2026-11-14"

# Exactly one stage per wiki file; earlier in the list wins.
STAGES = (
    ("linked", "Verified copy and Preservation link on the File page"),
    ("verified-no-link", "Verified Wayback copy, File page not linked yet"),
    ("problem", "Failed, retry or unverified capture"),
    ("queued", "In queue.txt or submitted, not verified yet"),
    ("approved-not-queued", "Approved, waiting to be promoted into the queue"),
    ("ready-unapproved", "Ready, not in approved-objects.json"),
    ("held", "Still stamped with the 14 Nov 2026 hold (#1358 decides)"),
    ("not-ready", "Registered, not ready for another reason"),
    ("unregistered", "On the wiki, no preservation registry entry"),
)
STAGE_ORDER = [s for s, _ in STAGES]
STAGE_RANK = {s: i for i, s in enumerate(STAGE_ORDER)}

DONE_STAGE = {
    "verified": "verified-no-link",
    "ok": "queued",
    "pending": "queued",
    "failed": "problem",
    "retry": "problem",
    "unverified": "problem",
}
LINK_BAD = frozenset({"error", "missing", "no-anchor", "dry", "todo", "skip", "skipped"})

_IMG = re.compile(r"/(?:wiki/)?images/((?:archive/)?[0-9a-f]/[0-9a-f]{2}/.+)$")
_FILE = re.compile(r"/(?:wiki/|index\.php\?title=)(?:File|Image):(.+)$", re.I)
_TICKET = re.compile(r"(\d{1,6})\s*$")


def norm_title(name: str) -> str:
    name = unquote(name or "").replace(" ", "_").strip("_")
    if name[:5].lower() == "file:":
        name = name[5:]
    return name[:1].upper() + name[1:]


def norm_url(url: str) -> str:
    """Collapse the host/path spellings the worker uses for one wiki object."""
    if not url:
        return ""
    u = unquote(url.strip()).split("#", 1)[0]
    m = _IMG.search(u)
    if m and ("swccg.com" in u):
        return "img:" + m.group(1).replace(" ", "_")
    m = _FILE.search(u)
    if m and ("swccg.com" in u):
        return "file:" + norm_title(m.group(1))
    u = re.sub(r"^https?://", "", u)
    return "url:" + u.rstrip("/")


def ticket_of(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else None
    m = _TICKET.search(str(value).rstrip("/"))
    return int(m.group(1)) if m else None


def object_stage(*, ready: bool | None, reason: str, approved: bool,
                 queued: bool, done_status: str | None) -> str:
    """Stage of one registry object (no File-page link knowledge)."""
    if done_status in DONE_STAGE:
        return DONE_STAGE[done_status]
    if queued:
        return "queued"
    if approved:
        return "approved-not-queued"
    if ready:
        return "ready-unapproved"
    if HOLD_MARK in (reason or ""):
        return "held"
    return "not-ready"


def best(stages) -> str:
    stages = [s for s in stages if s in STAGE_RANK]
    return min(stages, key=STAGE_RANK.__getitem__) if stages else "unregistered"


def file_stage(object_stages, *, linked: bool, done_statuses=()) -> str:
    """One stage for a wiki file from all its objects and URL forms."""
    cands = list(object_stages) + [DONE_STAGE[s] for s in done_statuses if s in DONE_STAGE]
    stage = best(cands)
    if linked and stage in ("verified-no-link",):
        return "linked"
    if linked and stage == "unregistered":
        # Linked by hand or by an older pass; still counts as preserved.
        return "linked"
    return stage


def scope_index(approved: dict) -> dict[str, dict]:
    """object_id -> scope (ticket, reason, reviewed_at) for exact-id scopes."""
    out: dict[str, dict] = {}
    for sc in approved.get("additional_approval_scopes") or []:
        ids = sc.get("exact_object_ids")
        if not isinstance(ids, list):
            continue
        meta = {"ticket": ticket_of(sc.get("ticket")), "reason": str(sc.get("reason") or "")[:300],
                "reviewed_at": sc.get("reviewed_at")}
        for oid in ids:
            out.setdefault(str(oid), meta)
    return out


def approval_diff(before: dict | None, after: dict, *, at: str, source: str) -> list[dict]:
    """Append-only log entries for IDs added/removed between two approval files."""
    old = set(map(str, (before or {}).get("object_ids") or []))
    new = set(map(str, after.get("object_ids") or []))
    idx_after, idx_before = scope_index(after), scope_index(before or {})
    groups: dict[tuple, dict] = {}
    for kind, ids, idx in (("added", new - old, idx_after), ("removed", old - new, idx_before)):
        for oid in ids:
            meta = idx.get(oid) or {"ticket": None, "reason": "", "reviewed_at": None}
            key = (kind, meta["ticket"], meta["reviewed_at"])
            g = groups.setdefault(key, {"at": at, "source": source, "change": kind,
                                         "ticket": meta["ticket"], "count": 0,
                                         "reviewed_at": meta["reviewed_at"], "reason": meta["reason"]})
            g["count"] += 1
    out = sorted(groups.values(), key=lambda e: (e["change"], e["ticket"] or 0))
    for e in out:
        e["total_after"] = len(new)
    return out
