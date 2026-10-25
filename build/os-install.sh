#!/bin/sh
# Image build: installs exactly the Debian packages listed in build/debian-packages.lock and stops the
# build if anything differs. To set new versions: python3 scripts/lock.py refresh  (needs Docker).
set -eu
dir="$(dirname "$0")"
lock="$dir/debian-packages.lock"
ts="$(sed -n 's/^# snapshot: //p' "$lock")"
[ -n "$ts" ] && [ "$ts" != none ] || { echo "ERROR: $lock has no snapshot – run: python3 scripts/lock.py refresh" >&2; exit 1; }
sh "$dir/apt-snapshot.sh" "$ts"
dpkg-query -W -f '${Package} ${Version}\n' | LC_ALL=C sort > /tmp/installed-packages
grep -v '^#' "$lock" | grep -v '^$' > /tmp/locked-packages || true
if ! diff -u /tmp/locked-packages /tmp/installed-packages; then
  echo "ERROR: the Debian packages differ from build/debian-packages.lock (lines above: - locked, + installed)." >&2
  echo "       Set new versions with: python3 scripts/lock.py refresh" >&2
  exit 1
fi
rm -f /tmp/installed-packages /tmp/locked-packages
echo "Debian packages: $(wc -l < "$lock" | tr -d ' ') lines, snapshot $ts – as locked."
