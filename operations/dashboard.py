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
    "archive.verified_24h": "Verified in last 24 hours (current ledger)",
    "archive.verified_7d": "Verified in last 7 days (current ledger)",
    "archive.verified_undated": "Verified records without usable timestamp",
    "archive.failed_transient": "Failed — eligible for weekly retry",
    "archive.failed_due": "Failed — past weekly retry threshold",
    "archive.failed_other": "Failed — needs investigation",
    "archive.verified_manpc": "Verified by MANPC",
    "archive.manpc_candidates": "MANPC verification candidates",
    "archive.manpc_deferred": "Explicitly deferred to MANPC",
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
    "audience.forum_page_requests":"Recorded forum page navigations (24h)",
    "audience.forum_likely_browser_requests":"Likely browser requests (24h)",
    "audience.forum_known_automation_requests":"Automation-indicating requests (24h)",
    "audience.forum_unknown_requests":"Unclassified page requests (24h)",
    "audience.forum_browser_address_groups":"Distinct likely-browser address groups (24h estimate)",
    "audience.forum_parsed_lines":"Parsed forum log lines (24h)",
    "audience.forum_invalid_lines":"Unparsed lines in scanned files",
    "audience.forum_coverage_seconds":"Recorded log coverage duration",
    "player.games":"Recorded game results",
    "player.partial_ids":"Results with missing participant IDs",
    "player.participants":"Distinct recorded participant IDs",
    "player.local_accounts":"Local Play account rows created",
    "player.source_checked_at":"Play aggregate checked at",
    "security.community_active":"Community blocklist records (active)",
    "security.local_active":"Local security-engine decisions (active)",
    "security.community_dropped_packets":"Packets dropped — community blocklist",
    "security.local_dropped_packets":"Packets dropped — local engine",
    "security.community_dropped_bytes":"Bytes dropped — community blocklist",
    "security.local_dropped_bytes":"Bytes dropped — local engine",
    "security.processed_packets":"Packets processed by bouncer",
    "security.source_checked_at":"Security aggregate checked at",
    "security.interval_seconds":"Most recent bouncer reporting interval",
    "security.daily_community_dropped_packets":"Community-list packets dropped (recorded 24h intervals)",
    "security.daily_local_dropped_packets":"Local-engine packets dropped (recorded 24h intervals)",
    "security.daily_processed_packets":"Packets processed (recorded 24h intervals)",
    "security.daily_coverage_seconds":"Coverage of reported daily intervals",
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
    if source_id == "player":return {"rolling_1d":"24 hours","rolling_7d":"7 days","rolling_30d":"30 days"}.get(dimension)
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


def _preservation(sources):
    def value(name):
        row = _find(sources, "archive", "archive." + name)
        return _number(row.get("value")) if row else None
    counts = [value(k) for k in ("verified", "failed", "pending", "retry", "unverified", "ok", "unknown")]
    if any(v is None for v in counts) or not sum(counts):
        return '<section class="history"><h2>Preservation progress</h2><p>Complete ledger counts unavailable.</p></section>'
    total = sum(counts)
    percentage = 100 * counts[0] / total
    timer = next((r for r in sources.get("service", {}).get("metrics", []) if isinstance(r, Mapping) and r.get("metric_id") == "service.timer_active" and r.get("dimension") in ("wayback", "swccg-wayback")), {})
    scheduler = {0: "Stopped", 1: "Running"}.get(timer.get("value"), "Unknown")
    reason = next((label for key,label in (("provider_unavailable","Provider unavailable"),("provider_throttled","Provider throttled"),("canary_unverified","Recovery capture not yet verified"),("capacity_wait","Account capture capacity unavailable"),("source_wait","Recovery source-specific limit")) if value("reason_"+key)==1),"Reason unavailable")
    cooldown = value("run_paused") == 1 and (value("mode_open") == 1 or value("mode_half_open") == 1 or value("reason_capacity_wait")==1)
    state = f'<p><strong>Automatic cooldown.</strong> {_e(reason)}. Next eligible retry: {_e(_time(value("next_eligible_at")))}.</p>' if cooldown else ''
    def count(name):
        return _value(value(name), "count")
    def window(name):
        n = count(name)
        return "At least " + n if value("verified_undated") and n != "Unavailable" else n
    coverage = "Verification timestamps; undated records: " + count("verified_undated")
    return f'''<section class="history"><h2>Preservation progress</h2>{state}<p>Scheduler: {_e(scheduler)} · Last verified: {_e(_time(value("last_verified_at")))}</p>
    <p><strong>{percentage:.1f}%</strong> · {counts[0]:,} of {total:,} current ledger entries verified</p><progress value="{counts[0]}" max="{total}" style="width:100%;height:1.4rem" aria-label="Verified ledger entries"></progress>
    <div class="tiles">{_card("Verified in 24 hours", window("verified_24h"), coverage)}{_card("Verified in 7 days", window("verified_7d"), coverage)}{_card("Failed — weekly retry", count("failed_transient"), "Subject to queue and cooldown; due: " + count("failed_due"))}{_card("Failed — investigation", count("failed_other"), "Not automatically reset by the transient rule")}{_card("MANPC lane", count("verified_manpc"), "Verified receipts recorded; candidates: " + count("manpc_candidates") + "; explicitly deferred: " + count("manpc_deferred"))}</div>
    <p>Counts are current ledger entries, not unique file versions or an immutable activity history. MANPC verifies existing submissions; it does not submit saves.</p></section>'''


def _manpc_panel(sources, now):
    def value(name):
        row = _find(sources, "archive", "archive.manpc_" + name)
        return _number(row.get("value")) if row else None
    observed = value("checked_at")
    now = _number(now)
    state = next((label for key, label in (("running", "Verifying"), ("completed", "Online · waiting for next run"), ("waiting", "Waiting · VPS provider hold"), ("provider_backoff", "Waiting · provider cooldown"), ("error", "Error")) if value("state_"+key)==1), "Unknown")
    if observed is None or now is None:
        state = "No heartbeat yet"
    elif now-observed>2400:
        state = "Offline or task stopped · heartbeat stale"
    elif value("state_running")==1 and now-observed>600:
        state = "Run overdue · check Task Scheduler"
    return f'''<section class="history" id="manpc"><h2>MANPC automatic verification</h2><p><strong>{_e(state)}</strong> · Last heartbeat: {_e(_time(observed))}</p>
    <div class="tiles">{_card("Last successful batch", _time(value("last_success_at")), "Batch completion, not necessarily new verification")}{_card("Next expected run", _time(value("next_expected_at")), "Approximate; Windows Task Scheduler is authoritative")}{_card("Verified in 24 hours", _value(value("verified_24h"), "count"), "Receipts accepted into the owner ledger")}{_card("Last batch", _value(value("last_verified_count"), "count"), "Receipts sent; attempted: " + _value(value("attempted"), "count") + "; errors: " + _value(value("errors"), "count"))}</div>
    <p>Task: SWCCG MANPC Verification · 30 minutes, low priority, maximum 10 minutes, no duplicate runs. Stop or disable from Windows Task Scheduler. Last possible provider cooldown: {_e(_time(value("cooldown_until")))}.</p></section>'''


def _recovery_panel(sources):
    def v(key):
        row=_find(sources,'archive','archive.recovery_'+key)
        return _number(row.get('value')) if row else None
    total, verified=v('total'),v('verified')
    if total is None or verified is None:
        return '<section class="history" id="archive-recovery"><h2>Historical failed capture recovery</h2><p>Awaiting the owner’s fixed-cohort receipt; unavailable is not zero.</p></section>'
    def count(name): return _value(v(name),'count')
    progress=f'<progress value="{verified}" max="{total}" style="width:100%;height:1.4rem" aria-label="Recovered captures"></progress>' if total else ''
    return f'''<section class="history" id="archive-recovery"><h2>Historical failed capture recovery</h2><p><strong>{count('verified')} of {count('total')} verified</strong> · Fixed historical cohort</p>{progress}<p>Original failures: {count('auth')} authentication · {count('no_job')} no job ID · {count('other')} other.</p>
    <div class="tiles">{_card('Verified',count('verified'),'MANPC accepted: '+count('verified_manpc'))}{_card('In progress',count('in_progress'),'Existing capture or ordinary owner retry')}{_card('Waiting',count('waiting'),'Awaiting bounded reconciliation')}{_card('Needs review',count('review'),'No matching capture, excluded, or outside active queue')}</div><p>Existing captured content is checked first. Only active queued requests can return to the VPS submission loop after a definitive no-capture result. Provider holds are respected. Original failure evidence is retained.</p></section>'''


def _reliability_panel(sources):
    def v(key):
        row=_find(sources,'archive','archive.'+key)
        return _number(row.get('value')) if row else None
    def count(key):return _value(v(key),'count')
    events={'account_sessions':'Account capacity exhausted','account_daily_limit':'Account daily capture limit','url_daily_limit':'Individual URL daily limit','target_host_limit':'Target host rate limit','target_redirects':'Target redirect limit','unknown_limit':'Unclassified provider limit','http_throttled':'Provider HTTP throttle','http_unavailable':'Provider HTTP unavailable','http_auth':'Authentication rejected','capacity_unknown':'Account capacity check unavailable','capacity_available':'Capacity checked'}
    event=next((label for key,label in events.items() if v('event_'+key)==1),'Exact event not yet recorded')
    return f'''<section class="history" id="archive-reliability"><h2>Archive provider reliability</h2><p>Latest classified event: <strong>{_e(event)}</strong> · Capacity sampled: {_e(_time(v('capacity_checked_at')))}. Values are the last observed account status, not a live guarantee.</p>
    <div class="tiles">{_card('Available capture sessions',count('capacity_available'),'Processing: '+count('capacity_processing'))}{_card('Daily captures',count('capacity_daily_captures'),'Limit: '+count('capacity_daily_captures_limit'))}{_card('Daily status requests',count('capacity_daily_status'),'Limit: '+count('capacity_daily_status_limit'))}{_card('Provider failures',count('provider_failure_count'),'Consecutive global cooldown events')}{_card('Scoped holds',count('source_limits_active'),'URL/host-specific deferrals')}</div>
    <h2>Stale acknowledged jobs</h2><p>Previously submitted jobs retained for reconciliation; they are not blindly resubmitted.</p><div class="tiles">{_card('Tracked stale jobs',count('stale_total'),'Waiting: '+count('stale_waiting'))}{_card('Verified',count('stale_verified'),'MANPC accepted: '+count('stale_manpc'))}{_card('In progress',count('stale_in_progress'),'Previously acknowledged capture or queued retry')}{_card('Needs review',count('stale_review'),'Unresolved or conflicting receipt')}</div></section>'''


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
    notes={"audience":"Pilot: production forum log only. Address groups are a rough estimate, not people; shared mixed production/test logs are excluded. No raw addresses or user agents are retained.","player":"Play-only recorded game results and distinct IDs in recorded outcomes; missing IDs limit coverage. Local player rows are not deduplicated sign-in registrations. Live sessions and instrumented funnel events are not yet measured.","security":"Source-owned latest bouncer interval and non-overlapping reported daily intervals. Packets, bytes and active decisions are distinct units, not unique attackers or HTTP requests. No enforcement settings changed."}
    if source_id in notes:data=f'<p class="service-note">{_e(notes[source_id])}</p>'+data
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
            rendered, style = "Automatic cooldown" if (open_mode and open_mode.get("value") == 1) or (recovery and recovery.get("value") == 1) else "Paused", "warn"
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
{_preservation(sources)}{_reliability_panel(sources)}{_recovery_panel(sources)}{_manpc_panel(sources, snapshot.get("generated_at"))}<section class="history"><h2>Historical comparisons</h2><p>Other 24-hour, 7-day and 30-day trends require comparable historical buckets and source coverage. Missing samples are never zero.</p></section>
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
