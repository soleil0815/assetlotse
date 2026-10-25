#!/bin/sh
# Image build: installs exactly the Debian packages listed in build/debian-packages.lock and stops the
# build if anything differs. To set new versions: python3 scripts/lock.py refresh  (needs Docker).
# The list is made for one CPU architecture (# arch:); on another one (e.g. arm64) the same snapshot is
# installed, but the exact list cannot be compared – the build says so and goes on.
set -eu
dir="$(dirname "$0")"
lock="$dir/debian-packages.lock"
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
here="$(dpkg --print-architecture)"
if [ -n "$arch" ] && [ "$here" != "$arch" ]; then
  echo "NOTE: the package list is for $arch, this build is $here – snapshot $ts installed, list not compared."
  exit 0
fi
dpkg-query -W -f '${Package} ${Version}\n' | LC_ALL=C sort > /tmp/installed-packages
grep -v '^#' "$lock" | grep -v '^$' > /tmp/locked-packages || true
if ! diff -u /tmp/locked-packages /tmp/installed-packages; then
  echo "ERROR: the Debian packages differ from build/debian-packages.lock (lines above: - locked, + installed)." >&2
  echo "       Set new versions with: python3 scripts/lock.py refresh (or Run workflow → relock)" >&2
  exit 1
fi
rm -f /tmp/installed-packages /tmp/locked-packages
echo "Debian packages: $(grep -vc '^#' "$lock") as locked (snapshot $ts / security $sec, $here)."
