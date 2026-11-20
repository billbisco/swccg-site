"""Render a private, pre-authorized aggregate snapshot as self-contained HTML.

The caller must enforce authorization and serve this only on an admin route.
This module has no HTTP server, auth logic, credentials, or network access.
It deliberately displays only recognized metric and source identifiers; raw
adapter details and arbitrary dimensions are never included in the page.
"""

from __future__ import annotations

import argparse
import html
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


CARACAS = timezone(timedelta(hours=-4))
SOURCES = {
    "archive": ("Archive.org preservation", "Verified captures, provider state and queue progress"),
    "host": ("Infrastructure capacity", "Host memory, swap, disk and CPU capacity"),
    "service": ("Service and scheduler health", "Latest scheduled runs and service states"),
    "backup": ("Backups and recovery", "Verified receipts and incomplete backup work"),
    "audience": ("Audience", "Page activity and privacy-reviewed visitor estimates"),
    "player": ("Player activity", "Registrations, active players and completed games"),
    "security": ("Defensive traffic", "Aggregate security and suspicious-traffic events"),
}
METRIC_LABELS = {
    "archive.verified": "Verified captures",
    "archive.failed": "Failed",
    "archive.pending": "Pending",
    "archive.retry": "Retry",
    "archive.unverified": "Unverified",
    "archive.ok": "Captured, not verified",
    "archive.unknown": "Other or unknown ledger status",
    "archive.mode_healthy": "Provider healthy",
    "archive.mode_open": "Provider circuit open",
    "archive.mode_half_open": "Provider recovery probe",
    "archive.mode_auth_blocked": "Authentication blocked",
    "archive.mode_probe_blocked": "Recovery probe blocked",
    "archive.run_completed": "Run completed",
    "archive.run_paused": "Run paused",
    "archive.run_failed": "Run failed",
    "archive.last_verified_at": "Last verified progress",
    "archive.last_submission_at": "Last submission attempt",
    "archive.next_eligible_at": "Next eligible attempt",
    "archive.oldest_pending_at": "Oldest pending activity",
    "archive.awaiting_canary": "Awaiting recovery verification",
    "archive.source_checked_at": "Owner receipt checked at",
    "archive.run_success": "Latest run succeeded",
    "host.memory_available": "Available RAM",
    "host.memory_total": "Total RAM",
    "host.swap_total": "Total swap",
    "host.swap_free": "Free swap",
    "host.disk_available": "Available storage",
    "host.disk_total": "Total storage",
    "host.cpu_cores": "CPU capacity",
    "service.active": "Service active",
    "service.timer_active": "Timer active",
    "service.run_success": "Latest run succeeded",
    "service.last_start_at": "Last start",
    "service.last_end_at": "Last end",
    "service.last_trigger_at": "Last scheduled trigger",
    "service.next_run_at": "Next scheduled run",
    "backup.last_verified_at": "Last independently verified backup",
    "backup.completed_tasks": "Completed backup tasks",
    "backup.failed_pending_tasks": "Failed or pending tasks",
    "backup.pending_tasks": "Pending tasks",
    "backup.discovery_failures": "Backup discovery failures",
    "backup.coverage_partial": "Task totals are partial",
    "backup.source_checked_at": "Backup receipt checked at",
}
STATUS_LABELS = {
    "ok": ("Observed", "good"),
    "error": ("Failed check", "danger"),
    "failed": ("Failed", "danger"),
    "degraded": ("Degraded", "warn"),
    "backoff": ("Backoff", "warn"),
    "paused": ("Paused", "warn"),
    "unavailable": ("Unavailable", "neutral"),
    "unknown": ("Unknown", "neutral"),
    "stale": ("Stale", "warn"),
}
DIMENSIONS = {"root": "Root", "data": "Data", "": "", "all": "All"}
# Explicit labels, never arbitrary configured unit names or internal paths.
SERVICE_DIMENSIONS = {
    "wayback": "Archive.org worker", "swccg-wayback": "Archive.org worker",
    "git_backup": "Git backup", "git-backup": "Git backup", "swccg-backup": "Git backup",
    "db_backup": "Database backup", "db-backup": "Database backup", "swccg-db-backup": "Database backup",
    "login": "Sign-in", "swccg-login-downcheck": "Sign-in checks",
    "disk": "Disk checks", "swccg-disk-check": "Disk checks", "swccg-disk-summary": "Disk summary",
    "cert": "Certificate checks", "swccg-cert-expiry": "Certificate checks", "certbot": "Certificate renewal",
    "flood-alert": "Flood alerts", "swccg-flooddrop-add": "Flood protection", "swccg-ip30": "IP retention",
    "sysstat-collect": "System statistics", "crowdsec": "CrowdSec", "nginx": "Web server",
    "collector": "Dashboard collector", "test": "Test service", "play": "Play service",
}


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if 0 <= value <= 2**53 - 1 and math.isfinite(value) else None


def _time(value: Any) -> str:
    value = _number(value)
    if value is None or value <= 0:
        return "Unavailable"
    try:
        local = datetime.fromtimestamp(value, timezone.utc).astimezone(CARACAS)
    except (OverflowError, OSError, ValueError):
        return "Unavailable"
    return local.strftime("%d %b %Y, %H:%M:%S") + " Caracas"


def _value(value: Any, unit: Any) -> str:
    value = _number(value)
    if value is None:
        return "Unavailable"
    if unit == "epoch_seconds":
        return _time(value)
    if unit in ("boolean", "bool"):
        return {0: "No", 1: "Yes"}.get(value, "Unknown")
    if unit == "bytes":
        if value < 0:
            return "Unavailable"
        for divisor, label in ((1024**3, "GiB"), (1024**2, "MiB"), (1024, "KiB"), (1, "B")):
            if value >= divisor:
                return f"{value / divisor:,.1f} {label}"
        return "0 B"
    if unit == "cores":
        return f"{value:g} cores"
    if unit == "percent":
        return f"{value:.1f}%" if 0 <= value <= 100 else "Unavailable"
    if unit == "seconds":
        return f"{value:,.1f} s"
    if unit == "milliseconds":
        return f"{value:,.1f} ms"
    if unit == "ratio":
        return f"{value * 100:.1f}%" if value <= 1 else "Unavailable"
    if unit != "count" or value < 0 or value != int(value):
        return "Unavailable"
    return f"{int(value):,}"


def _state(source: Mapping[str, Any]) -> tuple[str, str]:
    raw = source.get("status")
    label, style = STATUS_LABELS.get(raw, ("Unknown", "neutral")) if isinstance(raw, str) else ("Unknown", "neutral")
    if (source.get("stale") is True or source.get("detail") == "source_old") and raw != "stale":
        return (f"Stale · last check {label.lower()}", "warn")
    return label, style


def _valid_dimension(source_id: str, dimension: Any) -> str | None:
    if not isinstance(dimension, str):
        return None
    if dimension in DIMENSIONS:
        return DIMENSIONS[dimension]
    if source_id == "service":
        return SERVICE_DIMENSIONS.get(dimension)
    return None


def _metrics(source_id: str, source: Mapping[str, Any]) -> list[tuple[str, str, str, str]]:
    result = []
    rows = source.get("metrics", [])
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        metric_id = row.get("metric_id")
        if not isinstance(metric_id, str) or metric_id not in METRIC_LABELS:
            continue
        if not metric_id.startswith(source_id + "."):
            continue
        dimension = _valid_dimension(source_id, row.get("dimension", ""))
        if dimension is None:
            continue
        result.append((METRIC_LABELS[metric_id], dimension, _value(row.get("value"), row.get("unit")), _time(row.get("observed_at"))))
    return result


def _find(sources: dict[str, Mapping[str, Any]], source_id: str, metric_id: str) -> Mapping[str, Any] | None:
    for metric in sources.get(source_id, {}).get("metrics", []):
        if isinstance(metric, Mapping) and metric.get("metric_id") == metric_id and metric.get("dimension", "") in ("", "all"):
            return metric
    return None


def _card(title: str, value: str, hint: str, style: str = "neutral") -> str:
    return f'<article class="tile {style}"><h2>{_e(title)}</h2><strong>{_e(value)}</strong><p>{_e(hint)}</p></article>'


def _service_table(rows: list[tuple[str, str, str, str]]) -> str:
    services: dict[str, dict[str, str]] = {}
    for metric, dimension, value, _ in rows:
        if dimension:
            services.setdefault(dimension, {})[metric] = value
    if not services:
        return ''
    columns = ("Service active", "Timer active", "Latest run succeeded", "Last start", "Last end", "Last scheduled trigger", "Next scheduled run")
    header = ''.join(f'<th scope="col">{_e(name)}</th>' for name in columns)
    lines = []
    for service, values in services.items():
        cells = []
        for column in columns:
            value = values.get(column, "Unavailable")
            css = ' class="bad-value"' if column == "Latest run succeeded" and value == "No" else ''
            cells.append(f'<td{css}>{_e(value)}</td>')
        lines.append(f'<tr><th scope="row">{_e(service)}</th>{"".join(cells)}</tr>')
    return ('<p class="service-note">Scheduled jobs are normally inactive between runs. A failed latest run is shown separately from timer and observation status.</p>'
            '<div class="table-scroll"><table><thead><tr><th scope="col">Service</th>' + header +
            '</tr></thead><tbody>' + ''.join(lines) + '</tbody></table></div>')


def _source_section(source_id: str, source: Mapping[str, Any] | None) -> str:
    name, description = SOURCES[source_id]
    if source is None:
        return f'<section id="{source_id}" class="panel"><header><div><h2>{_e(name)}</h2><p>{_e(description)}</p></div><span class="badge neutral">Unavailable</span></header><p class="empty">No aggregate has been collected for this source yet. Missing data is not zero.</p></section>'
    label, style = _state(source)
    rows = _metrics(source_id, source)
    table = ''.join(f'<tr><th scope="row">{_e(metric)}</th><td>{_e(dimension or "—")}</td><td class="metric-value">{_e(value)}</td><td class="sample-time">{_e(when)}</td></tr>' for metric, dimension, value, when in rows)
    data = _service_table(rows) if source_id == "service" else ''
    if not data:
        data = ('<div class="table-scroll"><table><thead><tr><th scope="col">Metric</th><th scope="col">Scope</th><th scope="col">Value</th><th scope="col">Observed at</th></tr></thead><tbody>' + table + '</tbody></table></div>') if table else '<p class="empty">No supported aggregate values in this sample. Coverage is missing.</p>'
    return f'''<section id="{source_id}" class="panel"><header><div><h2>{_e(name)}</h2><p>{_e(description)}</p></div><span class="badge {style}">{_e(label)}</span></header>
      <dl class="freshness"><div><dt>Source observed</dt><dd>{_e(_time(source.get('observed_at')))}</dd></div><div><dt>Last successful source read</dt><dd>{_e(_time(source.get('last_success_at')))}</dd></div></dl>
      {data}</section>'''


def render_dashboard(snapshot: Mapping[str, Any], *, preview: bool = False) -> str:
    """Render the allowlisted aggregate schema v1. No I/O or remote assets."""
    if not isinstance(snapshot, Mapping) or type(snapshot.get("schema_version")) is not int or snapshot.get("schema_version") != 1 or not isinstance(snapshot.get("sources"), list):
        raise ValueError("unsupported aggregate snapshot schema")
    sources: dict[str, Mapping[str, Any]] = {}
    for source in snapshot["sources"]:
        if isinstance(source, Mapping) and isinstance(source.get("source"), str) and source["source"] in SOURCES:
            if source["source"] in sources:
                raise ValueError("duplicate source identifier")
            if not isinstance(source.get("metrics", []), list):
                raise ValueError("invalid aggregate metrics")
            sources[source["source"]] = source
    archive = _find(sources, "archive", "archive.verified")
    run = _find(sources, "archive", "archive.run_success")
    provider = _find(sources, "archive", "archive.mode_healthy")
    backup = _find(sources, "backup", "backup.last_verified_at")
    ok_count = sum(_state(src)[1] == "good" for src in sources.values())
    issue_count = sum(_state(src)[1] in {"danger", "warn"} for src in sources.values())
    unknown_count = len(sources) - ok_count - issue_count
    source_hint = f"{issue_count} degraded/failed/stale, {unknown_count} unknown, {len(SOURCES) - len(sources)} not collected"
    cards = [_card("Fresh source checks", str(ok_count) if sources else "Unavailable", source_hint, "warn" if issue_count else "neutral")]
    def tile(metric: Mapping[str, Any] | None, source_id: str, title: str, hint: str) -> str:
        source = sources.get(source_id)
        if metric is None or source is None:
            return _card(title, "Unavailable", hint)
        label, style = _state(source)
        rendered = _value(metric.get("value"), metric.get("unit"))
        mid = metric.get("metric_id")
        # A normal protective pause or backoff is not a run crash. Its mode
        # and next eligible attempt are separate from the completion outcome.
        paused = _find(sources, "archive", "archive.run_paused")
        open_mode = _find(sources, "archive", "archive.mode_open")
        recovery = _find(sources, "archive", "archive.mode_half_open")
        if mid == "archive.run_success" and paused and paused.get("value") == 1:
            rendered, style = "Paused", "warn"
        elif mid == "archive.mode_healthy" and open_mode and open_mode.get("value") == 1:
            rendered, style = "Backoff", "warn"
        elif mid == "archive.mode_healthy" and recovery and recovery.get("value") == 1:
            rendered, style = "Recovering", "warn"
        elif rendered == "No" and mid in ("archive.run_success", "archive.mode_healthy"):
            style = "warn" if label.startswith("Stale") else "danger"
        return _card(title, rendered, f"{label} · {hint}", style)
    cards += [tile(archive, "archive", "Verified archive captures", "Ledger status; not a distinct-version count"), tile(run, "archive", "Archive latest run succeeded", "Separate from provider health and capture progress"), tile(provider, "archive", "Archive provider healthy", "Separate from worker exit status"), tile(backup, "backup", "Last verified backup", "Restoration evidence is separate")]
    nav = ''.join(f'<a href="#{source_id}">{_e(SOURCES[source_id][0])}</a>' for source_id in SOURCES)
    sections = ''.join(_source_section(source_id, sources.get(source_id)) for source_id in SOURCES)
    sample = _time(snapshot.get("generated_at"))
    preview_badge = '<aside class="preview">MOCK PREVIEW · Synthetic demonstration data. No live site or administrative access supplied.</aside>' if preview else ''
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>SWCCG Operations</title><style>{CSS}</style></head>
<body><main><div class="masthead"><div><p class="eyebrow">SWCCG.COM &nbsp; / &nbsp; PRIVATE OPERATIONS</p><h1>Operations overview</h1><p class="subtitle">One view of service checks, preservation progress, backups and launch measurement.</p></div><div class="stamp"><span>SNAPSHOT GENERATED</span><strong>{_e(sample)}</strong><small>Historical data shown as collected</small></div></div>{preview_badge}
<nav aria-label="Dashboard sections">{nav}</nav><div class="tiles">{''.join(cards)}</div>
<section class="history"><h2>Historical comparisons</h2><p>24-hour, 7-day and 30-day trends are unavailable in this current-snapshot contract. They will be shown when comparable historical buckets and source coverage are supplied. Missing samples are never zero.</p></section>
{sections}<footer>Private aggregate dashboard · All timestamps in Caracas (UTC−04:00). Source checks do not prove an operational outcome. This view is read-only; it does not submit archive jobs, restart services or run AI.</footer></main></body></html>'''


CSS = '''
:root{color-scheme:dark;--bg:#0b1424;--panel:#121f32;--border:#283b52;--text:#eff4fa;--muted:#a7b8ce;--accent:#67d5d2;--good:#7ee0ae;--warn:#f5c979;--danger:#f9a0a0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#101f35 0,#0b1424 550px);color:var(--text);font-size:15px;line-height:1.55}main{max-width:1380px;margin:auto;padding:48px 34px}.masthead{display:flex;justify-content:space-between;gap:24px;align-items:center;margin-bottom:26px}.eyebrow{color:var(--accent);font-size:.77rem;letter-spacing:.12em;font-weight:700;margin:0}h1{font-size:clamp(2rem,4vw,3.1rem);line-height:1.12;letter-spacing:-.04em;margin:14px 0 10px}.subtitle,.panel header p,.history p{color:var(--muted);margin:0}.stamp{min-width:230px;border-left:2px solid var(--accent);padding-left:18px}.stamp span,.stamp small{display:block;color:var(--muted);font-size:.78rem}.stamp strong{display:block;margin:6px 0;font-size:1rem}.preview{padding:12px 15px;background:#382a0f;border:1px solid #846529;color:#ffe4b0;border-radius:8px;margin:0 0 19px}nav{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 22px}nav a{background:#172b41;color:#d8e9ff;border:1px solid #2c4560;padding:7px 12px;text-decoration:none;border-radius:7px;font-size:.83rem}nav a:focus,nav a:hover{border-color:var(--accent);color:var(--accent)}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:13px;margin-bottom:20px}.tile{border:1px solid var(--border);background:var(--panel);padding:18px;border-radius:11px}.tile h2{font-size:.85rem;font-weight:600;color:var(--muted);margin:0 0 12px}.tile strong{display:block;font-size:1.55rem;line-height:1.25;overflow-wrap:anywhere}.tile p{font-size:.75rem;color:var(--muted);margin:10px 0 0}.tile.good strong{color:var(--good)}.tile.warn strong{color:var(--warn)}.tile.danger strong{color:var(--danger)}.history{padding:17px 20px;border:1px dashed var(--border);border-radius:10px;margin:0 0 22px}.history h2{font-size:1rem;margin:0 0 5px}.history p{font-size:.87rem}.panel{margin-bottom:18px;border:1px solid var(--border);background:var(--panel);border-radius:11px;overflow:hidden}.panel header{display:flex;justify-content:space-between;align-items:center;gap:18px;padding:22px 22px 16px}.panel header h2{font-size:1.3rem;margin:0}.panel header p{font-size:.86rem}.badge{display:inline-block;font-size:.78rem;font-weight:700;border:1px solid var(--border);background:#182c43;padding:5px 10px;border-radius:999px;white-space:nowrap}.badge.good{color:var(--good)}.badge.warn{color:var(--warn)}.badge.danger{color:var(--danger)}.badge.neutral{color:var(--muted)}.freshness{margin:0;padding:0 22px 15px;display:flex;gap:27px;flex-wrap:wrap;font-size:.82rem}.freshness dt{color:var(--muted)}.freshness dd{margin:2px 0 0}.table-scroll{overflow-x:auto;border-top:1px solid var(--border)}table{width:100%;border-collapse:collapse;min-width:570px}th,td{padding:11px 18px;text-align:left;border-bottom:1px solid #223449}thead th{color:var(--muted);font-size:.76rem;text-transform:uppercase;letter-spacing:.04em;background:#142238}tbody th{font-weight:500;font-size:.88rem}tbody tr:last-child td,tbody tr:last-child th{border-bottom:0}tbody tr:nth-child(even){background:#142339}.metric-value{font-variant-numeric:tabular-nums;font-weight:650}.sample-time{font-size:.78rem;color:var(--muted);white-space:nowrap}.empty{color:var(--muted);margin:0;padding:6px 22px 22px}.panel:target{border-color:var(--accent)}footer{color:var(--muted);font-size:.79rem;padding-top:12px}.tile,.panel{box-shadow:0 5px 15px #0002}@media(max-width:650px){main{padding:25px 14px}.masthead{flex-direction:column;align-items:flex-start;gap:12px}.stamp{padding-left:12px}.panel header{flex-direction:column;align-items:flex-start;padding:17px}.freshness{padding:0 17px 15px;gap:14px}.tiles{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.tile{padding:13px}.tile strong{font-size:1.32rem}.tile h2{margin-bottom:8px}.tile p{font-size:.72rem}.history{padding:14px}.empty{padding:0 17px 17px}}@media(max-width:330px){.tiles{grid-template-columns:1fr}}
'''
CSS += '.service-note{color:var(--muted);font-size:.8rem;padding:0 22px 10px;margin:0}.bad-value{color:var(--danger);font-weight:700}.panel#service table{min-width:1000px}'


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a private sanitized dashboard snapshot; no web server")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--preview", action="store_true", help="watermark synthetic mock data")
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    args.output.write_text(render_dashboard(snapshot, preview=args.preview), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
