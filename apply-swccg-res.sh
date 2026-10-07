#!/bin/bash
# Install holotable card faces + GEMP chrome on this box and serve them from swccg.com.
# Run as root on the VPS. Idempotent.
set -euo pipefail

HOLO="${HOLO:-/opt/swccg-holotable}"
RES="${RES:-/opt/swccg-res}"
MIRROR="${MIRROR:-https://github.com/billbisco/swccg-holotable.git}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NGINX_SITE="${NGINX_SITE:-/etc/nginx/sites-available/swccg.com}"

echo "== holotable $HOLO"
if [ ! -d "$HOLO/.git" ]; then
  git clone --depth 1 --single-branch --branch master "$MIRROR" "$HOLO"
else
  git -C "$HOLO" fetch --depth 1 origin master
  git -C "$HOLO" reset --hard origin/master
fi
test -d "$HOLO/Images-HT/starwars"

echo "== GEMP chrome $RES"
mkdir -p "$RES/gemp" "$RES/packs" "$RES/rules" "$RES/social" "$RES/parsecs" "$RES/wp" "$RES/cards"
while IFS= read -r url || [ -n "$url" ]; do
  url="${url%%$'\r'}"
  [ -z "$url" ] && continue
  case "$url" in \#*) continue ;; esac
  rel="${url#https://res.starwarsccg.org/}"
  dest="$RES/$rel"
  if [ "$rel" = "swccg_gold64.png" ]; then
    dest="$RES/swccg_gold64.png"
  fi
  mkdir -p "$(dirname "$dest")"
  if [ ! -s "$dest" ]; then
    echo "get $rel"
    curl -fsSL --retry 3 -o "$dest" "$url"
  fi
done < "$HERE/extra-assets.txt"

echo "== nginx include"
if ! grep -q "swccg-res\|/opt/swccg-holotable" "$NGINX_SITE"; then
  python3 - "$NGINX_SITE" "$HERE/nginx-res.inc" <<'PY'
import sys
from pathlib import Path
site, inc = Path(sys.argv[1]), Path(sys.argv[2])
text = site.read_text(encoding="utf-8")
needle = "    location /chrome/ {"
block = inc.read_text(encoding="utf-8").rstrip() + "\n\n"
if needle not in text:
    raise SystemExit(f"cannot find {needle!r} in {site}")
site.write_text(text.replace(needle, block + needle, 1), encoding="utf-8")
print("patched", site)
PY
fi

nginx -t
systemctl reload nginx
echo "DONE apply-swccg-res"
echo "probe: curl -sI https://swccg.com/cards/Premiere-Light/large/lukeskywalker.gif"
