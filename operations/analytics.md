# Aggregate launch and engagement metrics contract

This first adapter is deliberately an offline, source-owner projection. It does not create a database user, read raw event rows, add tracking, configure a schedule, or enable new production traffic. The live dashboard continues to report unsupported audience, security and player-activity metrics as unknown. Sources must be enabled only after their owner and actual production coverage are validated.

## Existing GEMP daily rollup

GEMP's existing `metrics_daily(day, metric, count)` schema is the starting point. The source's maintenance path produces daily UTC aggregates and writes `_rolled = 1` transactionally with event counts for completed days. Empty days without this marker are not evidence of zero. `metrics_event` is a separate raw source retained under its existing policy; this dashboard must never export its guest/game IDs, details or rows, and must not change its retention.

The owner must validate that the running database schema and event emitters actually represent the intended events before advertising a capability. A source tree, compiled class, daily rollup marker, or scheduled worker is not proof of production frontend coverage. Validation must cover every possible day in the receipt's 60-day reporting interval, not merely the currently deployed emitter. If an event was introduced partway through the interval, keep that event unvalidated until a reviewed per-event coverage-start contract exists; never interpret the pre-instrumentation period as zero. The adapter requires an explicit deployment configuration of validated event types; its private receipt must declare the same allowlist. Default capability is empty. Unknown and unvalidated counts are `null`, even when a receipt contains a count or `_rolled` marker.

### Producer and private receipt

- One already-authorized source owner produces an atomic, private aggregate export. The consumer accepts **Play only**; never merge Test/staging with production. An independent Test adapter can be added later if needed.
- Read only the existing daily table, scoped to the last 60 completed UTC days. Require a bounded query timeout, a row cap, a read-only transaction and the existing date/metric primary key or a verified equivalent index. Never retrieve raw tables, details, player/account records or credentials. Do not run repeated production queries without an approved execution path.
- If the table, permissions or coverage is not proven, leave this adapter unavailable. Do not create a new credential or claim the fixture is live data. A narrow source-owner query shape (proposed, to validate against the deployed dialect) is:

```sql
SELECT day, metric, count
FROM metrics_daily
WHERE day >= UTC_DATE() - INTERVAL 60 DAY AND day < UTC_DATE()
  AND metric IN ('_rolled','play_click','game_start','turn_reached',
                 'game_end','game_abandoned','game_resume',
                 'signup_after_guest','outbound_click')
ORDER BY day, metric
LIMIT 541;
```

At most 540 valid rows can result (60 distinct days × 9 keys). Reject extra, duplicate, invalid, out-of-range or partial query results rather than silently truncating. Group by the returned date and keep only these literal metric keys. If a query fails or times out, do not publish a new source timestamp or substitute empty counts. Receipt time records the successful aggregate export, not each underlying event's time.

Receipt v1 (synthetic example only):

```json
{
  "schema": 1,
  "source": "gemp_daily",
  "environment": "play",
  "observed_at": 1700000000,
  "validated_events": ["play_click", "game_start"],
  "days": [
    {"day": "2023-11-13", "metrics": {"_rolled": 1, "play_click": 3, "game_start": 2}}
  ]
}
```

The reader caps receipt bytes at 64 KiB and rejects additional envelope/row/metric keys, unknown labels, invalid counts, duplicate days, future times, current partial days and dates more than 60 days before receipt generation. The event capability is a fixed set from separate owner validation, not automatically inferred. The reader outputs only static metric IDs, static window dimensions and numeric/null values. No raw source paths, exceptions or row details are emitted. Historical window counts are gauges, not additive deltas.

### Interpretation and comparison

The `player` source produces six windows: `completed_1d`, `prior_1d`, `completed_7d`, `prior_7d`, `completed_30d`, `prior_30d`. All end at complete UTC-day boundaries; the prior windows have identical durations immediately preceding the corresponding completed window. These are not rolling clock-hour counts. `player.window_end_at` and `player.source_checked_at` keep actual source time visible. Each window includes `player.covered_days` and `player.expected_days`. Any absent or unrolled day makes its window's event totals unavailable; it does not zero-fill. For a fully covered window, only a validated event emitter permits interpreting an absent event key as zero in that source's existing rollup. A fresh receipt with incomplete event/rollup coverage is degraded, and an old receipt explicitly reports `source_old`.

| Existing event | Safe label / metric |
|---|---|
| `play_click` | Instrumented Play clicks (`player.instrumented_play_click`) |
| `game_start` | Instrumented bot game starts (`player.instrumented_bot_game_start`); the current event contract requires a bot dimension, so not all PvP/game starts |
| `turn_reached` | Instrumented turn-reached events (`player.instrumented_turn_reached`), not unique turns or players |
| `game_end` | Instrumented game-end events (`player.instrumented_game_end`), not proven all completed games |
| `game_abandoned` | Instrumented abandonment events (`player.instrumented_game_abandoned`) |
| `game_resume` | Instrumented resume events (`player.instrumented_game_resume`), not distinct returning players |
| `signup_after_guest` | Instrumented guest-conversion events (`player.instrumented_signup_after_guest`), not total newly registered accounts |
| `outbound_click` | Instrumented outbound-click events (`player.instrumented_outbound_click`) |

Do not compute conversion percentages from incompatible counters, assume unique players from clicks/events, or label bot starts as all game starts. A separate authoritative aggregate account/game-history source is needed for registrations, active/returning players, PvP, completion, concurrency and cohort retention. Validate those definitions, de-duplication, indexes, role/authorization and query costs first. No usernames, emails, IP addresses, guest/game IDs or person-level histories in dashboard data. A source-specific export can compute authorized aggregates without exporting underlying identities.

## Next: #108 traffic and #1232 defensive security coverage plan

### Coverage before counters

1. Enumerate the existing nginx/site log sources and exact configured formats, owner, retention, rotation policy, clock and service mapping. The implementation must establish actual host/service attribution rather than infer from untrusted paths or host headers. Disabled logging, unknown formats, unsampled/ambiguous sources and staging contamination are explicit gaps. Use known aggregate sources first. Keep the existing 30-day raw IP policy; do not duplicate log storage or widen it.
2. Establish literal safe groups for public content/service routes, static assets, authenticated service routes, unknown and excluded health traffic. Parse only approved source-local fields, discard raw request target, query/referrer, IP, username, user agent and other identifiers before metrics persistence. Referrer domains, if separately approved, use a short bounded domain-category list, not arbitrary full URLs or query strings. Unknown format/parse errors count only as bounded coverage issues, not a silently dropped denominator. Do not deploy third-party analytics, client trackers, cookies, or browser fingerprints.
3. Separate instrumented page requests from human estimates. Existing source-owned verified crawler/defensive classification may support categories (known crawler, likely automation, likely human, unknown); do not infer a person from an IP or a self-reported UA. No claimant's identity or IP appears in the result. Unique client-group estimates require a separately reviewed privacy-preserving local aggregation method and clear limitations; do not store durable per-client pseudonyms or infer unique people/retention by summing daily counts. Signed-in aggregate player activity comes from the authoritative source owner, not proxy heuristics.

### Rotation, event time and capacity gates

- Start with a single source, a bounded local incremental read and an explicit cursor (device/inode/offset, plus validated generation context where required). Only a coherent committed source range may be processed once. Hold incomplete trailing rows until complete; record precise covered time range and parser rejection coverage. Commit the cursor and event-time aggregates atomically. A crash/replay after either boundary must not double counts. Source identity and retention state are not visitor identifiers.
- Reopen a rotated file only when its trusted exact identity is established in the approved rotation inventory. Copytruncate, inode reuse, lost old file, unknown generation or unexplained cursor regression is a coverage gap requiring explicit reconciliation; never silently reset and count again. Detect shrinking size, missing or unknown files, partial lines and permission failures. No raw files copied to dashboard or off-host. No continuous full scans or decompression replays by default.
- Current `Store` is built for collection-time 15-minute samples. A traffic reader must add a reviewed atomic event-time bucket/cursor contract before backfill or delayed ingestion; storing yesterday's requests in the current collection-time bucket would be incorrect. Explicitly define day alignment, covered intervals, source outages, idempotent per-range writes and partially parsed windows before enabling delta metrics. Current `expected_cursor` guards competing writes but cannot substitute for source rotation transition validation.
- Benchmark a representative approved local sample without exporting raw rows; measure CPU, memory, IO, backlog and data growth. Set a hard per-run read/time limit and catch-up only when headroom permits. Never repeatedly rescan large logs for a dashboard page. The approved lightweight dashboard remains query/cached-snapshot driven, no new external traffic or AI runtime. If a budget cap prevents catching up, show the lag and coverage instead of pretending the current period is complete.

### Defensive security metrics

- Reuse existing firewall/bouncer, security engine, flood/blocked-request and login outcome aggregate interfaces, with each source's actual units and timestamp. Counts of detection decisions, actual blocks, request attempts and login failures are distinct; do not sum overlapping sources into “unique attackers.” No offender identity, address list, event payload, raw target path or credential export. Use source-specific counts and fixed reason categories only as supported; unknown categories aggregate into one `unknown` bucket.
- Preserve the existing enforcement settings. This dashboard does not block/unblock, probe, scan, test live security controls, restart services or infer intrusive targeting details. No active actions accompany monitoring. Read existing local aggregates through authorized read-only owner contracts; handle unavailable source/counter resets/service restarts explicitly, never convert missing to zero.
- Test with synthetic aggregate fixtures including duplicate decision/block records, separate units, unavailable/partial sources, reset counter, normal traffic, classification unknown, rejected parser rows, rotation and truncation, out-of-order timestamps, one crash before/after commit and cap-exceeded lag. Validate source totals independently. Any prospective anomaly alert uses defensible existing aggregate rates and freshness; uncertain or incomplete coverage must not trigger confident malicious attribution.
