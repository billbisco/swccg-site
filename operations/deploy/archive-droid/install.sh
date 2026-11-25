#!/bin/bash
# Install the Archive Droid staff page on gemp (swccg-guild/work#1360). Run as root from the
# unpacked bundle directory. Read-only toward the wayback state; proves that with before/after hashes.
set -euo pipefail
B=$(cd "$(dirname "$0")" && pwd)          # bundle root (contains operations/)
OPS=/opt/swccg-operations
WORK=/mnt/HC_Volume_107153504/work/archive-droid
VIEW=/mnt/HC_Volume_107153504/archive-droid-view
ST=/root/.local/state/swccg-wayback
EV=$WORK/evidence; TS=$(date -u +%Y%m%dT%H%M%SZ)
SITE=/etc/nginx/sites-available/swccg.com
SNIP=/etc/nginx/snippets/swccg-archive-droid-route.conf
mkdir -p "$EV"; chmod 0750 "$WORK"

state_hash() {
  { sha256sum "$ST/registry/approved-objects.json" /opt/swccg-ops/repos/swccg-wiki/ops/wayback/queue.txt \
      /etc/systemd/system/swccg-wayback.service /etc/systemd/system/swccg-wayback.timer \
      /etc/systemd/system/swccg-wayback.service.d/*.conf
    find "$ST/registry/requests" -type f -printf '%P %s %T@\n' | sort | sha256sum | sed 's/-$/registry-requests-listing/'
  } 2>/dev/null
}
state_hash > "$EV/before-$TS.txt"

echo "== code"
DEPLOYED_SHA=$(sha256sum "$OPS/operations/dashboard.py" | cut -d' ' -f1)
EXPECT_SHA=$(cat "$B/dashboard.py.main-sha256")
install -d -m 0755 "$OPS/operations/archive_droid"
install -m 0644 "$B"/operations/archive_droid/*.py "$OPS/operations/archive_droid/"
if [ "$DEPLOYED_SHA" = "$EXPECT_SHA" ]; then
  cp -p "$OPS/operations/dashboard.py" "$WORK/dashboard.py.bak-$TS"
  install -m 0644 "$B/operations/dashboard.py" "$OPS/operations/dashboard.py"
  echo "overview link installed (deployed dashboard.py matched main)"
  LINK=1
else
  echo "SKIP overview link: deployed dashboard.py differs from main ($DEPLOYED_SHA)"; LINK=0
fi
install -d -m 2750 -o root -g www-data "$VIEW" "$VIEW/missing"

echo "== units"
install -m 0644 "$B"/operations/deploy/archive-droid/archive-droid-*.{service,timer,path} /etc/systemd/system/
systemctl daemon-reload
systemctl start archive-droid-sweep.service
systemctl start archive-droid-now.service
journalctl -u archive-droid-sweep.service -u archive-droid-now.service -n 6 -o cat --no-pager | grep ARCHIVE-DROID || true

echo "== nginx"
(
  flock -w 120 9
  cp -p "$SITE" "$WORK/swccg.com.nginx.bak-$TS"
  install -m 0644 "$B/operations/deploy/archive-droid/nginx-route.conf" "$SNIP"
  if ! grep -q 'swccg-archive-droid-route.conf' "$SITE"; then
    sed -i '/include \/etc\/nginx\/snippets\/swccg-operations-staff-gateway.conf;/a\    include /etc/nginx/snippets/swccg-archive-droid-route.conf;  # #1360 Archive Droid staff page' "$SITE"
  fi
  if nginx -t 2>"$EV/nginx-t-$TS.txt"; then
    systemctl reload nginx; echo "nginx -t ok; reloaded"
  else
    echo "nginx -t FAILED; restoring"; cp -p "$WORK/swccg.com.nginx.bak-$TS" "$SITE"; rm -f "$SNIP"; nginx -t && systemctl reload nginx; exit 3
  fi
) 9>/root/nginx-edit.lock
cat "$EV/nginx-t-$TS.txt"

echo "== timers"
systemctl enable --now archive-droid-now.timer archive-droid-sweep.timer archive-droid-approvals.path
if [ "$LINK" = 1 ]; then systemctl restart swccg-operations-gateway-reader.service swccg-operations-reader.service; fi

sleep 2
state_hash > "$EV/after-$TS.txt"
if diff -q "$EV/before-$TS.txt" "$EV/after-$TS.txt" >/dev/null; then echo "READ-ONLY PROOF: wayback state identical before/after"; else echo "STATE CHANGED during install (worker may have run):"; diff "$EV/before-$TS.txt" "$EV/after-$TS.txt" | cut -c1-200; fi

echo "== checks"
for u in /admin/operations/archive-droid/ /admin/operations/archive-droid /admin/operations/archive-droid/missing/untraced-1.html /admin/operations/archive-droid/missing/groups.json /admin/operations/; do
  printf '%s anon -> %s\n' "$u" "$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "https://swccg.com$u")"
done
printf 'POST anon -> %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -X POST https://swccg.com/admin/operations/archive-droid/)"
curl -s https://swccg.com/admin/operations/archive-droid/ | head -c 300; echo
runuser -u www-data -- head -c 120 "$VIEW/index.html" >/dev/null && echo "www-data can read the page files"
ls -ld /mnt/HC_Volume_107153504 "$VIEW"; ls "$VIEW/missing" | wc -l; du -sh "$VIEW"
grep -c 'archive-droid' "$OPS/operations/dashboard.py" || true
systemctl list-timers --no-pager | grep -E 'archive-droid|swccg-wayback'
systemctl is-active archive-droid-approvals.path swccg-operations-gateway-reader.service swccg-operations-reader.service
python3 - <<'PY'
import json
s=json.load(open('/mnt/HC_Volume_107153504/work/archive-droid/sweep.json'))
n=json.load(open('/mnt/HC_Volume_107153504/work/archive-droid/now.json'))
print('SWEEP', json.dumps({k:s[k] for k in ('generated_at','wiki_files','stage_counts','stage_sum_ok','registry_kinds','linker_values','snapshots_missing')}))
print('GROUPS', json.dumps(dict(list(s['missing_groups'].items())[:25])), 'n_groups', len(s['missing_groups']))
print('NOW', json.dumps({k:n[k] for k in ('generated_at','health','capacity','current','queue_len','approved_ids','ledger','warnings','approvals_log_total')}))
print('QUEUE5', json.dumps(n['queue'][:5]))
print('QCUR', [r for r in n['queue'] if r['current']][:1], 'qtickets', sum(1 for r in n['queue'] if r['ticket']))
print('APPROVALS', json.dumps(sorted(n['approvals_log'], key=lambda x:x['at'] or '')[-12:]))
PY
