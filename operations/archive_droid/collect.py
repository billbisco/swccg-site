"""Archive Droid read-only collector, nightly sweep and approval-log hook.

    python3 -I -B collect.py now        # every 5 min: Now + Queue (+ approval hook fallback)
    python3 -I -B collect.py sweep      # nightly: reconcile wiki x registry x done x linker
    python3 -I -B collect.py approvals  # hook (systemd .path) on approved-objects.json change

Read-only toward the wayback state: it never writes the registry, queue.txt,
done.json, approved-objects.json, control/health files or worker units, and it
never calls archive.org. Its only writes are under --work and --view. The
append-only first-seen log (first-seen.jsonl) lives in --work.
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from operations.archive_droid import core, render  # noqa: E402

STATE = Path("/root/.local/state/swccg-wayback")
REPO = Path("/opt/swccg-ops/repos/swccg-wiki")
WORK = Path("/mnt/HC_Volume_107153504/work/archive-droid")
VIEW = Path("/mnt/HC_Volume_107153504/archive-droid-view")
WIKI_API = "https://swccg.com/wiki/api.php"
BACKUP_GLOBS = ("{registry}/approved-objects.json.bak*",
                "/mnt/HC_Volume_107153504/work/*/approved-objects.json.bak*")
UA = "SWCCG-ArchiveDroid-Sweep/1 (read-only; swccg-guild/work#1360)"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, text: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ad-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class Paths:
    def __init__(self, a):
        self.state, self.repo, self.work, self.view = Path(a.state), Path(a.repo), Path(a.work), Path(a.view)
        self.registry = self.state / "registry"
        self.approved = self.registry / "approved-objects.json"
        self.policy_log = self.registry / "policy-approvals.jsonl"
        self.policy_flagged = self.registry / "policy-unapproved.json"
        self.queue = self.repo / "ops/wayback/queue.txt"
        self.done = self.repo / "ops/wayback/done.json"
        self.health = self.state / "health.json"
        self.control = self.state / "control.json"
        self.linker = self.state / "linker.json"
        self.applog = self.work / "approvals.jsonl"
        self.applast = self.work / "approvals-last.json"
        self.index = self.work / "sweep-index.json"
        self.sweep = self.work / "sweep.json"
        self.now = self.work / "now.json"


# --------------------------------------------------------------------------- approvals
def _ids_state(data: dict) -> dict:
    return {"object_ids": sorted(map(str, data.get("object_ids") or [])),
            "additional_approval_scopes": [
                {k: v for k, v in sc.items() if k in ("ticket", "exact_object_ids", "reason", "reviewed_at")}
                for sc in data.get("additional_approval_scopes") or []]}


def approvals_hook(p: Paths, *, source: str = "hook") -> int:
    """Append entries for approval-file changes since the last snapshot. Idempotent."""
    p.work.mkdir(parents=True, exist_ok=True)
    with open(p.work / "approvals.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        raw = p.approved.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        last = load_json(p.applast)
        if last is None:
            n = backfill(p)
            last = load_json(p.applast)
            if last and last.get("sha256") == sha:
                return n
        if last and last.get("sha256") == sha:
            return 0
        cur = _ids_state(json.loads(raw))
        at = iso(p.approved.stat().st_mtime)
        entries = core.approval_diff(last.get("state") if last else None, cur, at=at, source=source)
        with open(p.applog, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, sort_keys=True) + "\n")
        atomic_write(p.applast, json.dumps({"sha256": sha, "mtime": at, "state": cur}))
        return len(entries)


def backfill(p: Paths) -> int:
    """First run: derive past entries from dated backups, then the current file."""
    files = set()
    for g in BACKUP_GLOBS:
        files.update(glob.glob(g.format(registry=p.registry)))
    states, seen = [], set()
    for f in files:
        try:
            raw = Path(f).read_bytes()
            data = json.loads(raw)
        except (OSError, ValueError):
            continue
        h = hashlib.sha256(json.dumps(_ids_state(data), sort_keys=True).encode()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        states.append((os.path.getmtime(f), _ids_state(data)))
    raw = p.approved.read_bytes()
    cur = _ids_state(json.loads(raw))
    states.append((p.approved.stat().st_mtime, cur))
    states.sort(key=lambda t: t[0])
    entries, prev = [], None
    for mtime, st in states:
        if prev is not None and st["object_ids"] == prev["object_ids"]:
            prev = st
            continue
        for e in core.approval_diff(prev, st, at=iso(mtime), source="backfill"):
            # A scope's own reviewed_at is the better date for its first appearance.
            if e["change"] == "added" and e.get("reviewed_at") and prev is None:
                e["at"] = e["reviewed_at"]
            entries.append(e)
        prev = st
    with open(p.applog, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, sort_keys=True) + "\n")
    atomic_write(p.applast, json.dumps({"sha256": hashlib.sha256(raw).hexdigest(),
                                        "mtime": iso(p.approved.stat().st_mtime), "state": cur}))
    return len(entries)


def read_applog(p: Paths) -> list[dict]:
    out = []
    try:
        with open(p.applog, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


POLICY_REASON = ("Auto-approved by the worker: ready, backup receipt and a work ticket number "
                 "(Bill 2026-11-25, #1359 Q1).")


def read_policy(p: Paths) -> tuple[set[str], list[dict]]:
    """Worker ticket auto-approvals (read-only): (object ids, log rows per run and ticket)."""
    ids: set[str] = set()
    groups: dict[tuple, int] = {}
    try:
        with open(p.policy_log, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                oid = rec.get("object_id") if isinstance(rec, dict) else None
                if not isinstance(oid, str) or oid in ids:
                    continue
                ids.add(oid)
                key = (str(rec.get("at") or ""), core.ticket_of(rec.get("ticket")))
                groups[key] = groups.get(key, 0) + 1
    except OSError:
        pass
    rows = [{"at": at or None, "ticket": ticket, "change": "auto-approved", "count": n,
             "source": "worker policy", "reason": POLICY_REASON}
            for (at, ticket), n in groups.items()]
    return ids, rows


# --------------------------------------------------------------------------- registry
def registry_objects(registry: Path):
    """Yield the latest record of every registry object (read-only walk)."""
    req = registry / "requests"
    for job in os.scandir(req):
        if not job.is_dir():
            continue
        for obj in os.scandir(job.path):
            if not obj.is_dir():
                continue
            best = None
            for root, _ds, fs in os.walk(obj.path):
                for fn in fs:
                    if not fn.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(root, fn), encoding="utf-8") as fh:
                            d = json.load(fh)
                    except (OSError, ValueError):
                        continue
                    if not isinstance(d, dict) or ("readiness" not in d and "subject" not in d):
                        continue
                    g = int(d.get("generation") or 0)
                    if best is None or g >= best[0]:
                        best = (g, d)
            if best:
                yield obj.name, best[1]


def systemd_show(unit: str, props: str) -> dict:
    try:
        out = subprocess.run(["systemctl", "show", unit, "-p", props], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def last_submitted(limit: int = 600) -> dict:
    """Latest SUBMITTED/VERIFY line of the worker (current or last item)."""
    try:
        out = subprocess.run(["journalctl", "-u", "swccg-wayback.service", "-n", str(limit),
                              "-o", "short-iso", "--no-pager"], capture_output=True, text=True,
                             timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    pat = re.compile(r"^(\S+) \S+ \S+: (SUBMITTED|CONFIRM \w+|VERIFY \w+) (https?://\S+)")
    for line in reversed(out.splitlines()):
        m = pat.match(line)
        if m:
            return {"at": m.group(1), "event": m.group(2), "url": m.group(3)}
    return {}


def done_index(done: dict) -> dict[str, dict]:
    out = {}
    for url, rec in (done or {}).items():
        if not isinstance(rec, dict):
            continue
        verify = rec.get("verify") if isinstance(rec.get("verify"), dict) else {}
        out[core.norm_url(url)] = {"url": url, "status": rec.get("status"),
                                   "capture": rec.get("capture") or "", "last": rec.get("last"),
                                   "verified_at": verify.get("at") or None}
    return out


PROBLEM_STATUSES = frozenset({"failed", "retry"})
# Still waiting (not yet verified). pending is shown as "submitted".
WAITING_STATUSES = frozenset({"new", "queued", "pending", "submitted", "ok", "unverified"})


def display_stage(status: str | None) -> str:
    st = status or "new"
    return "submitted" if st == "pending" else st


# Real registry timestamps only. Never use wiki upload_timestamp or file mtimes.
REGISTRY_DATE_FIELDS = ("registered_at", "requested_at", "request_time", "received_at",
                        "recorded_at", "queued_at")


def _as_iso(value) -> str | None:
    """Parse a stored timestamp; return None if it is missing or not a date."""
    if value in (None, "", "n/a"):
        return None
    if isinstance(value, (int, float)):
        if value < 1_000_000_000:  # not a unix time (avoids generation counts)
            return None
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
        except (OSError, OverflowError, ValueError):
            return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.isoformat(timespec="seconds")
    except ValueError:
        return None


def _consider_reg_date(out: dict, url, raw, field: str) -> None:
    at = _as_iso(raw)
    if not url or not at:
        return
    k = core.norm_url(url)
    if k not in out or at < out[k][0]:
        out[k] = (at, field)


def registry_item_dates(registry: Path) -> dict[str, tuple[str, str]]:
    """Earliest real registered_at / request / queued time from the registry, keyed by URL.

    Read-only. Ignores subject.upload_timestamp and filesystem mtimes.
    """
    out: dict[str, tuple[str, str]] = {}
    promo = Path(registry) / "promotions"
    try:
        entries = os.scandir(promo)
    except OSError:
        entries = ()
    for ent in entries:
        if not ent.is_file() or not ent.name.endswith(".json"):
            continue
        data = load_json(Path(ent.path), {}) or {}
        if not isinstance(data, dict):
            continue
        url = data.get("source_url") or data.get("url")
        for field in REGISTRY_DATE_FIELDS:
            if data.get(field):
                _consider_reg_date(out, url, data.get(field), field)
    req = Path(registry) / "requests"
    try:
        jobs = os.scandir(req)
    except OSError:
        jobs = ()
    for job in jobs:
        if not job.is_dir():
            continue
        for obj in os.scandir(job.path):
            if not obj.is_dir():
                continue
            for root, _ds, fs in os.walk(obj.path):
                for fn in fs:
                    if not fn.endswith(".json"):
                        continue
                    data = load_json(Path(root) / fn, {}) or {}
                    if not isinstance(data, dict):
                        continue
                    src = data.get("source") if isinstance(data.get("source"), dict) else {}
                    url = (src.get("url") or (data.get("subject") or {}).get("original_version_url")
                           or data.get("source_url") or data.get("url") or "")
                    for field in REGISTRY_DATE_FIELDS:
                        if data.get(field):
                            _consider_reg_date(out, url, data.get(field), field)
                        if src.get(field):
                            _consider_reg_date(out, url, src.get(field), field)
    return out


def load_first_seen(path: Path) -> tuple[dict, bool]:
    """Append-only log: first item record for a key wins. Returns (by_key, seed_done)."""
    out: dict[str, dict] = {}
    seed_done = False
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("kind") == "meta":
                    if rec.get("event") == "seed-done":
                        seed_done = True
                    continue
                k = rec.get("key")
                if k and k not in out and rec.get("at"):
                    out[k] = rec
    except OSError:
        pass
    return out, seed_done


def append_first_seen(path: Path, records: list[dict]) -> None:
    """Append records to the first-seen log. Never rewrites earlier lines."""
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def stamp_first_seen(p: Paths, rows: list[dict], *, now: datetime) -> dict[str, int]:
    """Record the first time the page collector sees each queue/problem URL.

    The log lives under the collector work directory only. On the first run,
    existing items are seeded with the earliest real registry date if one
    exists, otherwise 'on or before' now. Later new items get an exact
    first-seen time. Existing log records are never changed.
    """
    path = p.work / "first-seen.jsonl"
    seen, seed_done = load_first_seen(path)
    seed_mode = not seed_done
    dates = registry_item_dates(p.registry) if seed_mode else {}
    now_iso = now.isoformat(timespec="seconds")
    new: list[dict] = []
    if seed_mode and not path.exists():
        new.append({"kind": "meta", "event": "started", "at": now_iso})
    for row in rows:
        k = core.norm_url(row.get("url") or "")
        if not k:
            continue
        rec = seen.get(k)
        if rec is None:
            if seed_mode and k in dates:
                at, field = dates[k]
                rec = {"kind": "item", "key": k, "url": row.get("url"), "at": at,
                       "source": "registry", "field": field}
            elif seed_mode:
                rec = {"kind": "item", "key": k, "url": row.get("url"), "at": now_iso,
                       "source": "on-or-before", "field": None}
            else:
                rec = {"kind": "item", "key": k, "url": row.get("url"), "at": now_iso,
                       "source": "first-seen", "field": None}
            new.append(rec)
            seen[k] = rec
        row["added_at"] = rec.get("at")
        row["added_source"] = rec.get("source")
        row["added_field"] = rec.get("field")
    if seed_mode:
        new.append({"kind": "meta", "event": "seed-done", "at": now_iso,
                    "count": sum(1 for rec in seen.values() if rec.get("at"))})
    append_first_seen(path, new)
    counts = {"registry": 0, "on-or-before": 0, "first-seen": 0, "unknown": 0}
    for row in rows:
        src = row.get("added_source") or "unknown"
        counts[src] = counts.get(src, 0) + 1
    return counts


def linker_preservation(linker: dict) -> dict[str, str]:
    """Wayback capture URL -> 'yes' or 'no' from linker.json. Absent means unknown."""
    out: dict[str, str] = {}
    for key, val in (linker or {}).items():
        if not isinstance(key, str) or "|" not in key:
            continue
        cap = key.split("|", 1)[1]
        if not cap:
            continue
        good = str(val) not in core.LINK_BAD
        if good:
            out[cap] = "yes"
        elif cap not in out:
            out[cap] = "no"
    return out


# --------------------------------------------------------------------------- now (5 min)
def build_now(p: Paths) -> dict:
    now = utc_now()
    health = load_json(p.health, {}) or {}
    control = load_json(p.control, {}) or {}
    done = load_json(p.done, {}) or {}
    approved = load_json(p.approved, {}) or {}
    index = load_json(p.index, {}) or {}
    sweep = load_json(p.sweep, {}) or {}
    dix = done_index(done)
    tickets_by_url = index.get("tickets", {})
    try:
        queue = [l.strip() for l in p.queue.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.lstrip().startswith("#")]
    except OSError:
        queue = []
    cur = last_submitted()
    cur_key = core.norm_url(cur.get("url", ""))
    pres = linker_preservation(load_json(p.linker, {}) or {})
    rows = []
    waiting, problems = [], []
    seen_problems: set[str] = set()
    for i, url in enumerate(queue, 1):
        k = core.norm_url(url)
        d = dix.get(k) or {}
        st = d.get("status") or "new"
        row = {"pos": i, "url": url, "ticket": tickets_by_url.get(k), "stage": st,
               "current": bool(cur_key) and k == cur_key,
               "last": d.get("last"),
               "capture": d.get("capture") or "", "verified_at": d.get("verified_at")}
        rows.append(row)
        if st in PROBLEM_STATUSES:
            problems.append({**row, "stage": display_stage(st)})
            seen_problems.add(k)
        elif st != "verified":
            waiting.append({**row, "stage": display_stage(st)})
    # Failed/retry items no longer in queue.txt still belong in Problems (work-order first, then the rest).
    for k, d in dix.items():
        if (d.get("status") or "") not in PROBLEM_STATUSES or k in seen_problems:
            continue
        problems.append({"pos": None, "url": d.get("url") or k, "ticket": tickets_by_url.get(k),
                         "stage": display_stage(d.get("status")), "current": bool(cur_key) and k == cur_key,
                         "last": d.get("last"),
                         "capture": d.get("capture") or "", "verified_at": d.get("verified_at")})
    completed = []
    for k, d in dix.items():
        if d.get("status") != "verified":
            continue
        cap = d.get("capture") or ""
        completed.append({"url": d.get("url") or k, "ticket": tickets_by_url.get(k),
                          "verified_at": d.get("verified_at"), "capture": cap,
                          "preservation": pres.get(cap) or "unknown",
                          "current": bool(cur_key) and k == cur_key})
    completed.sort(key=lambda r: r.get("verified_at") or "", reverse=True)
    added_counts = stamp_first_seen(p, waiting + problems, now=now)
    svc = systemd_show("swccg-wayback.service", "ActiveState,Result,ExecMainStartTimestamp,ExecMainExitTimestamp")
    tmr = systemd_show("swccg-wayback.timer", "ActiveState,LastTriggerUSec,NextElapseUSecRealtime,NextElapseUSecMonotonic")
    status_counts: dict[str, int] = {}
    for d in dix.values():
        status_counts[d["status"] or "unknown"] = status_counts.get(d["status"] or "unknown", 0) + 1
    stages = sweep.get("stage_counts", {})
    cap = health.get("account_capacity") or control.get("account_capacity") or {}
    warnings = alerts(now=now, health=health, rows=rows, sweep=sweep, svc=svc, index_mtime=_mtime(p.index))
    approvals_log = read_applog(p)
    policy_ids, policy_rows = read_policy(p)
    explicit_ids = set(map(str, approved.get("object_ids") or []))
    running = len(explicit_ids)
    for row in sorted(policy_rows, key=lambda r: (r["at"] or "", r["ticket"] or 0)):
        running += row["count"]
        row["total_after"] = running
    approvals_log = sorted(approvals_log + policy_rows, key=lambda r: r.get("at") or "")
    flagged = load_json(p.policy_flagged, {}) or {}
    no_ticket = flagged.get("no_ticket")
    if isinstance(no_ticket, int) and no_ticket > 0:
        warnings.append(f"{no_ticket:,} ready registered items have no ticket number, so the worker "
                        f"did not auto-approve them (checked {flagged.get('checked_at') or 'unknown'}). "
                        "Re-register them with a work ticket number.")
    return {
        "schema": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "health": {k: health.get(k) for k in ("mode", "reason", "outcome", "checked_at",
                                              "last_submission_attempt_at", "last_verified_progress_at",
                                              "next_eligible_attempt_at", "oldest_pending_activity_at",
                                              "provider_event")},
        "next_submission_at": iso(control.get("next_submission_at")) if isinstance(control.get("next_submission_at"), (int, float)) else None,
        "capacity": {k: cap.get(k) for k in ("available", "processing", "daily_captures", "daily_captures_limit")},
        "service": svc, "timer": tmr,
        "current": cur,
        "queue": waiting,
        "queue_len": len(waiting),
        "queue_txt_len": len(rows),
        "problems": problems,
        "completed": completed,
        "ledger": status_counts,
        "approved_ids": len(explicit_ids | policy_ids),
        "approved_explicit": len(explicit_ids),
        "approved_policy": len(policy_ids - explicit_ids),
        "no_ticket": no_ticket if isinstance(no_ticket, int) else None,
        "approval_scopes": [{"ticket": core.ticket_of(sc.get("ticket")),
                             "count": len(sc["exact_object_ids"]) if isinstance(sc.get("exact_object_ids"), list) else sc.get("approved_objects"),
                             "reviewed_at": sc.get("reviewed_at"), "reason": str(sc.get("reason") or "")[:300]}
                            for sc in approved.get("additional_approval_scopes") or []],
        "approvals_log": approvals_log[-300:],
        "approvals_log_total": len(approvals_log),
        "stages": stages,
        "sweep_generated_at": sweep.get("generated_at"),
        "warnings": warnings,
        "added_counts": added_counts,
    }


def _mtime(path: Path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _age_h(value, now: datetime):
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - t).total_seconds() / 3600


def alerts(*, now, health, rows, sweep, svc, index_mtime) -> list[str]:
    """Proposed thresholds (#1359). Page warnings only until Bill picks a destination."""
    w = []
    queued = sum(1 for r in rows if r["stage"] in ("new", "pending", "ok"))
    age = _age_h(health.get("last_verified_progress_at"), now)
    if queued and age is not None and age > 6:
        w.append(f"No new verified capture for {age:.1f} h while {queued:,} items are queued (threshold 6 h).")
    end = _age_h(_systemd_time(svc.get("ExecMainExitTimestamp")), now)
    if svc.get("ActiveState") != "active" and end is not None and end > 0.5:
        w.append(f"Worker has not run for {end*60:.0f} min (threshold 30 min).")
    if svc.get("Result") not in (None, "", "success"):
        w.append(f"Last worker run result: {svc.get('Result')}.")
    oldest = sweep.get("oldest", {})
    for stage, limit_d in (("ready-unapproved", 3), ("approved-not-queued", 2), ("unregistered", 2)):
        a = _age_h(oldest.get(stage), now)
        if a is not None and a > limit_d * 24:
            w.append(f"Oldest {stage} item is {a/24:.1f} days old (threshold {limit_d} days).")
    delta = (sweep.get("delta") or {}).get("problem")
    if isinstance(delta, int) and delta > 50:
        w.append(f"Problem stage grew by {delta} since the previous sweep (threshold 50).")
    if index_mtime is None:
        w.append("No nightly sweep has completed yet.")
    elif (now.timestamp() - index_mtime) > 26 * 3600:
        w.append(f"Nightly sweep is {(now.timestamp()-index_mtime)/3600:.0f} h old (threshold 26 h).")
    return w


def _systemd_time(v):
    if not v or v in ("n/a", "0"):
        return None
    try:
        return datetime.strptime(" ".join(v.split()[1:3]) + " " + v.split()[3], "%Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc).isoformat()
    except (ValueError, IndexError):
        return None


# --------------------------------------------------------------------------- sweep (nightly)
def wiki_allimages(api: str) -> list[dict]:
    out, cont = [], None
    while True:
        q = {"action": "query", "list": "allimages", "ailimit": "500", "format": "json",
             "formatversion": "2", "aiprop": "timestamp|url|size|mime|user"}
        if cont:
            q["aicontinue"] = cont
        req = urllib.request.Request(api + "?" + urllib.parse.urlencode(q), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        out.extend(d.get("query", {}).get("allimages") or [])
        cont = (d.get("continue") or {}).get("aicontinue")
        if not cont:
            return out
        time.sleep(0.1)


def run_sweep(p: Paths, api: str) -> dict:
    now = utc_now()
    approved = load_json(p.approved, {}) or {}
    approved_ids = set(map(str, approved.get("object_ids") or [])) | read_policy(p)[0]
    sidx = core.scope_index(approved)
    dix = done_index(load_json(p.done, {}) or {})
    try:
        qset = {core.norm_url(l) for l in p.queue.read_text(encoding="utf-8").splitlines() if l.strip()}
    except OSError:
        qset = set()
    linker = load_json(p.linker, {}) or {}
    linked_titles, link_values = set(), {}
    for key, val in linker.items():
        title = key.split("|", 1)[0]
        v = str(val)
        link_values[v] = link_values.get(v, 0) + 1
        if v not in core.LINK_BAD:
            linked_titles.add(core.norm_title(title))

    files: dict[str, dict] = {}
    for img in wiki_allimages(api):
        t = core.norm_title(img.get("name") or img.get("title") or "")
        files[t] = {"title": t, "url": img.get("url"), "desc": img.get("descriptionurl"),
                    "mime": img.get("mime") or "", "ts": img.get("timestamp"), "objs": []}

    tickets_by_url, others = {}, []
    kind_counts: dict[str, int] = {}
    reg_stage_counts: dict[str, int] = {}
    for oid, d in registry_objects(p.registry):
        sub, src, rd = d.get("subject") or {}, d.get("source") or {}, d.get("readiness") or {}
        kind = sub.get("kind") or "unknown"
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        url = src.get("url") or sub.get("original_version_url") or sub.get("url") or ""
        k = core.norm_url(url)
        ticket = core.ticket_of((d.get("approval_scope") or {}).get("ticket")) or (sidx.get(oid) or {}).get("ticket")
        st = core.object_stage(ready=rd.get("ready"), reason=str(rd.get("reason") or ""),
                               approved=oid in approved_ids, queued=k in qset,
                               done_status=(dix.get(k) or {}).get("status"))
        reg_stage_counts[st] = reg_stage_counts.get(st, 0) + 1
        if ticket and k:
            tickets_by_url[k] = ticket
        title = core.norm_title(sub.get("title") or "")
        if kind == "wiki_file_version" and title in files:
            files[title]["objs"].append({"oid": oid, "stage": st, "ticket": ticket,
                                         "ts": sub.get("upload_timestamp"),
                                         "current": "/images/archive/" not in url})
        elif kind == "source_snapshot" and st not in ("verified-no-link",):
            others.append({"url": url, "ticket": ticket, "stage": st})

    stage_counts = {s: 0 for s in core.STAGE_ORDER}
    oldest: dict[str, str] = {}
    groups: dict[str, list] = {}
    for t, f in files.items():
        keys = [core.norm_url(f["url"] or ""), "file:" + t]
        statuses = [(dix.get(k) or {}).get("status") for k in keys]
        stage = core.file_stage([o["stage"] for o in f["objs"]], linked=t in linked_titles,
                                done_statuses=[s for s in statuses if s])
        cur = [o for o in f["objs"] if o["current"]] or f["objs"]
        ticket = next((o["ticket"] for o in cur if o["ticket"]), None)
        f["stage"], f["ticket"] = stage, ticket
        stage_counts[stage] += 1
        if f["ts"] and (stage not in oldest or f["ts"] < oldest[stage]):
            oldest[stage] = f["ts"]
        if stage != "linked":
            groups.setdefault(str(ticket) if ticket else "untraced", []).append(
                {"title": t, "url": f["url"], "desc": f["desc"], "mime": f["mime"],
                 "stage": stage, "ts": f["ts"]})
    prev = load_json(p.sweep, {}) or {}
    delta = {s: stage_counts[s] - (prev.get("stage_counts") or {}).get(s, 0) for s in stage_counts} if prev else {}
    for o in others:
        groups.setdefault(str(o["ticket"]) if o["ticket"] else "untraced", []).append(
            {"title": None, "url": o["url"], "desc": o["url"], "mime": "text/html", "stage": o["stage"], "ts": None,
             "kind": "source_snapshot"})
    summary = {
        "schema": 1, "generated_at": now.isoformat(timespec="seconds"),
        "wiki_files": len(files), "stage_counts": stage_counts,
        "stage_sum_ok": sum(stage_counts.values()) == len(files),
        "oldest": oldest, "delta": delta,
        "registry_kinds": kind_counts, "registry_object_stages": reg_stage_counts,
        "linker_values": link_values,
        "missing_groups": {g: len(v) for g, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))},
        "snapshots_missing": len(others),
    }
    p.work.mkdir(parents=True, exist_ok=True)
    atomic_write(p.index, json.dumps({"generated_at": summary["generated_at"], "tickets": tickets_by_url}))
    atomic_write(p.work / "missing.json", json.dumps(groups))
    if prev:
        atomic_write(p.work / "sweep-previous.json", json.dumps(prev))
    atomic_write(p.sweep, json.dumps(summary, indent=1))
    render.write_missing(p.view, groups, summary)
    return summary


# --------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=("now", "sweep", "approvals"))
    ap.add_argument("--state", default=str(STATE))
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--work", default=str(WORK))
    ap.add_argument("--view", default=str(VIEW))
    ap.add_argument("--wiki-api", default=WIKI_API)
    a = ap.parse_args(argv)
    p = Paths(a)
    p.work.mkdir(parents=True, exist_ok=True)
    p.view.mkdir(parents=True, exist_ok=True)
    with open(p.work / f"{a.mode}.lock", "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("ARCHIVE-DROID busy", a.mode)
            return 0
        if a.mode == "approvals":
            n = approvals_hook(p)
            data = build_now(p)
            atomic_write(p.now, json.dumps(data))
            render.write_now(p.view, data)
            print("ARCHIVE-DROID approvals", n)
        elif a.mode == "now":
            n = approvals_hook(p, source="collector")
            data = build_now(p)
            atomic_write(p.now, json.dumps(data))
            render.write_now(p.view, data)
            print("ARCHIVE-DROID now queue=%d approvals_new=%d warnings=%d added=%s" % (data["queue_len"], n, len(data["warnings"]), data.get("added_counts")))
        else:
            s = run_sweep(p, a.wiki_api)
            data = build_now(p)
            atomic_write(p.now, json.dumps(data))
            render.write_now(p.view, data)
            print("ARCHIVE-DROID sweep", json.dumps({"files": s["wiki_files"], "stages": s["stage_counts"], "sum_ok": s["stage_sum_ok"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
