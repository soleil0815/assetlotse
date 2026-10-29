#!/bin/sh
# Image build: installs exactly the Debian packages listed in build/debian-packages.lock and stops the
# build if anything differs. To set new versions: python3 scripts/lock.py refresh  (needs Docker).
# One list per CPU architecture: debian-packages.lock (amd64), debian-packages.<arch>.lock (e.g. arm64).
set -eu
dir="$(dirname "$0")"
here="$(dpkg --print-architecture)"
lock="$dir/debian-packages.lock"
[ "$here" = amd64 ] || lock="$dir/debian-packages.$here.lock"
if [ ! -s "$lock" ]; then
  echo "ERROR: no package list for $here ($lock) – set the versions: python3 scripts/lock.py refresh (or Run workflow → relock)" >&2
  exit 1
fi
ts="$(sed -n 's/^# snapshot: //p' "$lock")"
sec="$(sed -n 's/^# snapshot-security: //p' "$lock")"
arch="$(sed -n 's/^# arch: //p' "$lock")"
if [ -z "$ts" ] || [ "$ts" = none ] || [ -z "$sec" ] || [ "$sec" = none ]; then
  echo "ERROR: $lock has no snapshot – set the versions: python3 scripts/lock.py refresh (or Run workflow → relock)" >&2
  exit 1
fi
if ! sh "$dir/apt-snapshot.sh" "$ts" "$sec"; then
  echo "ERROR: the Debian packages could not be loaded from snapshot.debian.org (network, proxy or a temporary" >&2
  echo "       outage). The build needs HTTPS access to snapshot.debian.org – try again later." >&2
  exit 1
fi
if [ "$here" != "$arch" ]; then
  echo "ERROR: $lock is for '$arch', this build is $here." >&2
  exit 1
fi
dpkg-query -W -f '${Package} ${Version}\n' | LC_ALL=C sort > /tmp/installed-packages
grep -v '^#' "$lock" | grep -v '^$' > /tmp/locked-packages || true
if ! diff -u /tmp/locked-packages /tmp/installed-packages; then
  echo "ERROR: the Debian packages differ from build/${lock##*/} (lines above: - locked, + installed)." >&2
  echo "       Set new versions with: python3 scripts/lock.py refresh (or Run workflow → relock)" >&2
  exit 1
fi
rm -f /tmp/installed-packages /tmp/locked-packages
echo "Debian packages: $(grep -vc '^#' "$lock") as locked (snapshot $ts / security $sec, $here)."
