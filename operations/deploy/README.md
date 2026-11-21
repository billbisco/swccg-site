# Production collector deployment runbook (Linux, explicit approval required)

This bundle generates exactly three reviewed units for the five-minute collector. It is not an installer. It never changes existing backup/archive services, existing schedules, an owner's receipt permissions, DNS, nginx, access controls, external provider traffic or credentials. Use the current approved repository revision and exact source paths from the private inventory. No real deployment paths or secrets belong in the public repo.

## Process boundaries

`swccg-operations-export.service` is a small root-owned oneshot because two existing receipt producers use privileged directories. It reads exactly two configured, bounded receipt files, parses their fixed numeric/enum fields, and writes a strict aggregate-only envelope under `/run/swccg-operations-export/receipts.json`. It does not copy raw JSON. `ProtectHome=tmpfs` hides privileged home directories; isolated `BindReadOnlyPaths` supply only the selected receipt files at fixed input paths inside the service namespace. Verify actual binds on the target OS; the service must not require changing the source files or their parent permissions. Runtime directory is root-owned, collector group read/traverse only (0750), export file mode 0640. If a bind is absent, the corresponding source reports unavailable; no fake zero.

`swccg-operations-collector.service` is an unprivileged oneshot under a dedicated no-login Unix account. It reads only the sanitized envelope, bounded explicitly configured host paths and fixed read-only systemd properties. It cannot read root home directories or mutate owner state. Its *own* store/lock and separate dashboard snapshot/view directories are the only writable persistent paths. It uses a nonblocking Linux advisory lock in addition to systemd singleton service behavior. Memory cap 100 MiB, one core maximum, lower CPU/I/O priority, maximum 120 s per invocation, no IP networking. The service reads local AF_UNIX systemd data only; no Archive.org or network analytics requests. The collection continues with explicit unknown receipt status if the exporter is unavailable or its envelope is older than three minutes, rather than substituting stale counts as current.

`swccg-operations-collector.timer` schedules the existing singleton collector every five minutes, and requests one catch-up after reboot. The collector requests the independent exporter first. One file `heartbeat.json` is replaced only after the DB, JSON and HTML results have been committed and flushed. It is collector completion, not evidence that all sources succeeded; their statuses stay separate.

## Preparation and installation

The following is an example exact install sequence for an authorized operator. Substitute the actual approved mount and paths before generating. Do not run the examples unreviewed. Keep the bundle's staged files and source revision in deployment evidence.

1. Verify the intended data filesystem is mounted (`mountpoint -q /mnt/data`, `findmnt --target /mnt/data`) and has reserve space. Choose two nonoverlapping subdirectories on that filesystem and a root-controlled code checkout. Confirm that none aliases a source receipt, and record baseline disk/RAM/CPU. Do not write to `/tmp` for durable state. Use an immutable, reviewed, root-owned code tree; neither collector nor future viewer may write it.
2. Create the dedicated no-login Unix identity **only if absent**, after `getent group swccg-operations` and `getent passwd swccg-operations` checks. Example: `groupadd --system swccg-operations`; `useradd --system --gid swccg-operations --no-create-home --shell /usr/sbin/nologin swccg-operations`. Verify existing identity/group ownership if present; do not repurpose another account. A collector-only rollout requires no web identity or group.
3. Prepare the approved dedicated paths with explicit owner/mode, for example:

   ```sh
   install -d -o swccg-operations -g swccg-operations -m 0700 /mnt/data/operations-private
   install -d -o swccg-operations -g swccg-operations -m 0700 /mnt/data/operations-view
   install -d -o root -g root -m 0700 /mnt/data/operations-unit-review
   ```

   Ensure parent directories permit only the required traversal. Do not recursively change owner or mode on the data volume. The view contains sanitized aggregate JSON, HTML and heartbeat; the private directory contains SQLite state and lock. Both remain private in this rollout. Do not expose either through HTTP or nginx.
4. Run the full offline suite from the reviewed code checkout with `python3 -m unittest discover -s operations/tests -v`. Then generate the exact files in the empty review directory:

   ```sh
   python3 -m operations.deploy.render_units \
     --code-dir /opt/example-operations-code \
     --store-dir /mnt/data/operations-private \
     --view-dir /mnt/data/operations-view \
     --archive-source /private/approved/archive-health.json \
     --backup-source /private/approved/backup-health.json \
     --data-disk /mnt/data \
     --unit archive,example-archive.service,example-archive.timer \
     --unit backup,example-backup.service,example-backup.timer \
     --output /mnt/data/operations-unit-review
   ```

   Supply all approved unit dimensions/names; the generator validates names and distinct paths, forbids input/output overlap, requires persistent data paths beneath the specified mount, and refuses overwriting a staged unit. Inspect generated `ExecStart`, source bindings, directory modes and systemd properties. `systemd-analyze verify /mnt/data/operations-unit-review/swccg-operations-export.service /mnt/data/operations-unit-review/swccg-operations-collector.service /mnt/data/operations-unit-review/swccg-operations-collector.timer` must pass. If the host refuses the isolated receipt bind arrangement, do not broaden permissions or remove isolation without a reviewed change; fix the exact unit and revalidate.
5. Check the three exact `/etc/systemd/system/swccg-operations-*.{service,timer}` destinations for existing unrelated ownership. On first authorized install, copy only these three files with root ownership and mode 0644. Use separate explicit calls:

   ```sh
   install -o root -g root -m 0644 /mnt/data/operations-unit-review/swccg-operations-export.service /etc/systemd/system/swccg-operations-export.service
   install -o root -g root -m 0644 /mnt/data/operations-unit-review/swccg-operations-collector.service /etc/systemd/system/swccg-operations-collector.service
   install -o root -g root -m 0644 /mnt/data/operations-unit-review/swccg-operations-collector.timer /etc/systemd/system/swccg-operations-collector.timer
   systemctl daemon-reload
   systemctl start swccg-operations-collector.service
   ```

   Do **not** enable the timer until the first cycle's actual result is verified. The collector starts its exporter dependency automatically. `systemctl show swccg-operations-export.service swccg-operations-collector.service -p Result -p ExecMainStatus -p ExecMainStartTimestamp -p ExecMainExitTimestamp` must show successful completed invocations. Verify actual current source timestamps and statuses, not only exit 0. If an expected receipt is unavailable, check its explicit bind path and exporter state; do not consider that source verified.
6. Check heartbeat (independently, as an authorized operator):

   ```sh
   python3 -I -B /opt/example-operations-code/operations/deploy/runner.py check-heartbeat --heartbeat /mnt/data/operations-view/heartbeat.json --max-age 900
   ```

   Check artifact ownership/modes with `stat` on exact DB, snapshot, heartbeat and export files, and confirm the collector cannot modify owner receipts. Compare source metrics against owner-provided aggregate status. Never output raw receipt contents or DB cursors. Record actual service wall runtime, CPU and peak RSS; assess peak-host overhead and data usage before continuing.
7. Enable only this new reviewed timer:

   ```sh
   systemctl enable --now swccg-operations-collector.timer
   systemctl list-timers --all swccg-operations-collector.timer
   ```

   Observe at least one naturally scheduled second invocation after the initial manual start, and confirm heartbeat age, fresh DB observations and actual owner/host status. A healthy timer without a recent completed heartbeat is not proof of collection. If a second run is still pending or unavailable, report the exact state; don't claim autonomous operation yet. Keep separate alerting/watchdog work scoped to an approved independent local monitor. The read-only heartbeat checker is provided, but this bundle does not install a second monitoring timer, send alerts or claim whole-host-outage detection.

## Rollback and maintenance

The reversible rollback is specific to these new units:

```sh
systemctl disable --now swccg-operations-collector.timer
systemctl stop swccg-operations-collector.service swccg-operations-export.service
```

This does not touch any preservation/backup worker, restore old source receipts, change raw permissions, delete state or change account credentials. Preserve the exact DB/aggregate outputs privately for diagnosis and backup. Revert only the reviewed code/configuration for this bundle if required, validate exact replacement units and daemon-reload before a deliberate re-enable. Removing the dedicated units, account and stored data is a separate explicit cleanup decision. An old generated HTML file is a static historical snapshot; never present it as live status after disabling the collector.
