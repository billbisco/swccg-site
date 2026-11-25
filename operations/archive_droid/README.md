# Archive Droid page (swccg-guild/work#1360)

Unlisted, read-only page at `https://swccg.com/admin/operations/archive-droid/`. Anyone with the URL may view it (Bill, 2026-11-25). It is not linked from any public page; the staff-gated Operations overview may keep its link. Pages send `X-Robots-Tag: noindex, nofollow` and a matching meta robots tag. Do not add the path to `robots.txt` (a Disallow would advertise it). POST and other write methods are denied. URLs on github.com/billbisco (or raw.githubusercontent.com/billbisco) are shown as “private source” with no href. Only this page lists items; the overview stays aggregate-only.

| Unit | Schedule | What it does |
|---|---|---|
| `archive-droid-now.timer` | every 5 min (`*:02/5`) | Now + Queue + approval-log fallback, re-renders `index.html` |
| `archive-droid-sweep.timer` | nightly 07:40 UTC (03:40 UTC-4) | wiki `list=allimages` x registry x done.json x linker.json; one stage per file; Missing pages |
| `archive-droid-approvals.path` | on change of `approved-objects.json` | appends dated entries to `approvals.jsonl` |

Everything is **read-only toward the wayback state**. The units run with `ProtectSystem=strict`, `ProtectHome=read-only` and write only to `/mnt/HC_Volume_107153504/work/archive-droid` (JSON snapshots, append-only `approvals.jsonl`) and `/mnt/HC_Volume_107153504/archive-droid-view` (static HTML, group `www-data`). They never approve, submit, clear stamps, call archive.org or change worker config.

Stages, first match wins: linked, verified-no-link, problem, queued, approved-not-queued, ready-unapproved, held, not-ready, unregistered. "Linked" means the linker recorded a Preservation link for the File title.

Approval log: the first run derives past entries from dated `approved-objects.json.bak*` copies (file mtime = state time) and scope `reviewed_at`. After that, every change is diffed against the last snapshot and appended. The hook only reads `approved-objects.json`.

Install: `operations/deploy/archive-droid/install.sh` (as root on gemp, from a bundle). It edits nginx only under `flock /root/nginx-edit.lock`, runs `nginx -t`, then reloads.
