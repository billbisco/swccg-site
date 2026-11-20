# SWCCG operations dashboard foundation

This is a small first-party, aggregate-only Python/SQLite application. It can collect configured local health sources and produce a self-contained dashboard without installing another metrics server, transmitting data to third parties, or using an AI. Python 3.10+ and its standard library are sufficient; `systemctl` is needed only for the optional Linux service adapter. No external Archive.org calls are made.

The first implementation is intentionally a **one-shot offline/local collector and renderer**. It does not configure systemd, nginx, DNS, authentication, alert delivery or public hosting. Those production integrations require the project's approved rollout, least-privilege access paths and independent freshness monitoring. Future audience, player and security adapters are distinct stories; absence of those sources is not zero traffic or zero players.

## Run with explicit local paths

From this repository's root, with a writable **private** data directory and approved read-only source access:

```text
python -m operations.cli run --db operations/state/metrics.sqlite --meminfo /proc/meminfo --root-disk / --data-disk /path/to/data --archive-health /private/receipts/archive-health.json --backup-health /private/receipts/backup-health.json --unit archive,example-archive.service,example-archive.timer --snapshot-out operations/state/snapshot.json --output operations/state/dashboard.html
```

Replace all example paths and units with those from the approved deployment. Create the private `operations/state` directory first. Every source flag is optional, but at least one configured source is required. Receipt paths have no default, so this command cannot silently start reading another application's private directory. `--unit` may be repeated with `dimension,service[,timer]`. Only fixed read-only systemd properties are requested. The collector caps unit count/read time and does not start or stop anything. The host adapter reads a bounded memory-information file and only the explicitly named root/data filesystems.

The command initializes an empty DB if necessary, finalizes expired retention buckets (at most once per UTC boundary), collects each configured source separately, persists the observations, and writes the sanitized aggregate snapshot and optional HTML via temporary-file replacement. The output path must differ from all receipts and the DB. Stdout also contains the aggregate JSON. A failed source is recorded with a bounded status/detail code and omitted metrics become unknown; other sources continue. Storage/configuration errors exit nonzero and are not labeled as successful collection. Check each source status separately from the exit code. Opening generated HTML locally does not expose a network service.

### Read history or render without recollecting

```text
python -m operations.cli snapshot --db operations/state/metrics.sqlite
python -m operations.cli history --db operations/state/metrics.sqlite --source host --metric host.memory_available --start 1700006400 --end 1700092800 --resolution daily
python -m operations.cli maintain --db operations/state/metrics.sqlite
python -m operations.dashboard operations/examples/dashboard-mock.json operations/state/example.html --preview
```

Timestamps use UTC Unix seconds. History ranges are half-open and MUST align to 900-second bucket or 86,400-second UTC-day boundaries. The snapshot computes freshness at read time; static HTML is only a snapshot and will not refresh itself. A production private route should render a fresh read-only export (or independently detect and warn about stale static artifacts). Every future dashboard request/JSON route requires server-side administrator authorization; hiding a URL is insufficient. Do not expose filesystem paths, source receipts, DB files or the raw CLI interface through web routing.

## Data contract

`operations.contracts.Metric` accepts a bounded nonnegative finite numeric value or `None`, which means unknown. Units and metric kinds are allowlisted; booleans are explicitly numeric 0 or 1. `AdapterResult` contains a static source label, UTC collection timestamp, bounded tuple of unique metric-series samples and optional status/detail machine labels. **Labels are not a sanitization algorithm.** An adapter must use explicit allowlisted compile-time labels, units and dimensions, not payload-derived slugs. Do not use usernames, raw or hashed IPs, email, guest ID, game ID, token, URL, user agent or free-form exception text as labels or values. The operational adapters use fixed fields only, discard arbitrary source values, and never write source files.

Storage caps 32 sources, 128 metric series per source and 128 samples per observation. Metric IDs, source labels, dimensions and detail codes have strict bounded ASCII syntax. Kind/unit of a series is immutable; a semantic change requires a new versioned metric name. The SQLite database is capped at 250 MiB of pages; temporary rollback journal filesystem headroom must still be budgeted. New DB/artifact files are created privately. Use a protected directory and independent backup of aggregate state; the package does not manage filesystem principals or server backup jobs. Read paths are read-only connections and do not create a database.

`Store.record()` commits a complete source observation, all its samples, source status/success timestamp and optional checkpoint in one SQLite transaction. Strictly increasing collection timestamps prevent older observations from overwriting newer data. An exact equal-timestamp replay is a no-op; a differing result at the same timestamp is a conflict. A cursor update requires the caller's expected prior checkpoint. Deltas require a numeric cursor and may not repeatedly add data without advancing it. Checkpoint syntax is one to five colon-separated integer components; checkpoint interpretation is an adapter responsibility. Future log adapters must prove monotonic within-inode offsets, correct rotation and truncation transitions, and no duplicate replay. No log parser or autonomous identity tracking is shipped here.

The low-level `record --expected-cursor ...` stdin command is an internal, trusted/offline testing interface only, not a general external ingestion API. JSON must be a bounded `AdapterResult` object, with sample objects and no duplicate keys. Use `--expected-cursor ""` for the first NULL checkpoint. Future production adapters must be explicitly registered in the CLI and enforce their own fixed field/dimension allowlists before sending observations. Never send dashboard data from an untrusted direct JSON writer.

### Time, unknowns and retention

- Observations are grouped into 15-minute UTC buckets; gauges and cumulative counters retain most recent observation, deltas sum only committed increments. A `null` delta poisons its bucket; overflow is unknown rather than an imprecise total. A gauge snapshot is not a daily sum.
- Daily aggregate history retains 400 days. Complete days older than the 30-day bucket window are finalized transactionally and the expired buckets removed. A persisted retention boundary refuses late entries that would reopen expired history. DB page reclamation is left to SQLite; no routine `VACUUM` or data volume cleanup is performed.
- History returns point counts, expected/observed/known buckets, coverage flags and last observation; missing buckets are gaps, not zeros. A daily delta is unknown unless all 96 buckets have known values. Daily gauge/cumulative values are last-observed snapshots with explicit coverage, not a claim of a complete day. Today is incomplete until all relevant buckets have arrived. Do not compute percent changes from a missing or zero denominator; distinct group counts are not additive across days.
- `last_success_at` describes a readable committed source observation, not the successful execution of a service or provider job. A readable receipt can be degraded while still reporting actual progress. Source-specific thresholds distinguish archive/backup age from short host/service sampling; a stale receipt keeps its own original checked/verified timestamps as separate gauges. Unknown/error observations do not advance the last-success timestamp or checkpoint.
- Snapshots never expose private checkpoint cursors or raw source receipts. If the latest collection omitted a series, its current value is `null` instead of silently presenting the earlier value as fresh. Historical values older than the current bucket retention are not resurrected as current metrics. The renderer separately allowlists recognized sources, metrics and dimensions. All JSON must remain protected, as only the trusted registered runtime adapters satisfy the production metric-label policy.

## Offline verification and resource checks

```text
python -m unittest discover -s operations/tests -v
```

Tests exercise no external service: fixture receipts and memory/disk data, mock systemd, unknown-vs-zero, source failure isolation, cursor rollback/retry, immutable definitions, stale/fresh distinction, bounded input, rotation checkpoint API, partial/full-day rollups, retention boundary and read-only snapshots, and safe HTML rendering. They do not establish production throughput, actual alert delivery, or dashboard authorization.

Before rollout, measure CPU/RSS, DB size, lock wait and collector age on realistic source and cadence. Reuse existing timers/health and independent watchdog facilities where suitable. Keep the new collector non-overlapping; never change existing backup or preservation schedules to make dashboard collection succeed. The independent checker must detect stale or missing collector success even when the collector itself cannot send an alert. Whole-host loss requires a monitor outside that host; report unavailable coverage if none is approved. Prototype targets are ≤100 MiB collector RSS, bounded source time and low CPU; compare against actual peak-host operation before ratifying. No new permanent service, web route or alert destination is installed by this package.
