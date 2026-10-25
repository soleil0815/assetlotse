#!/bin/sh
# Brings ALL Debian packages of the image (including those of the base image) to the state of one fixed
# snapshot of the Debian archive (https://snapshot.debian.org). The same snapshot always gives the same
# packages – this is what makes the build reproducible. Runs as root inside the image.
#   apt-snapshot.sh <debian timestamp> <debian-security timestamp>      e.g. 20261026T021503Z
# Both are completed imports of snapshot.debian.org (scripts/lock.py takes the newest ones).
# Used by build/os-install.sh (every image build) and scripts/resolve-in-image.sh (scripts/lock.py).
set -eu
ts="${1:?usage: apt-snapshot.sh <debian timestamp> <debian-security timestamp>}"
sec="${2:?usage: apt-snapshot.sh <debian timestamp> <debian-security timestamp>}"
for t in "$ts" "$sec"; do
  case "$t" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) ;;
    *) echo "apt-snapshot.sh: invalid timestamp '$t'" >&2; exit 2 ;;
  esac
done
src=/etc/apt/sources.list.d/debian.sources
[ -f "$src" ] || { echo "apt-snapshot.sh: $src not found (not a Debian 12+ image?)" >&2; exit 1; }
cp "$src" /tmp/debian.sources.orig
# security first (its URI starts with the other one)
sed -i \
  -e "s#^URIs: http://deb.debian.org/debian-security\$#URIs: https://snapshot.debian.org/archive/debian-security/${sec}#" \
  -e "s#^URIs: http://deb.debian.org/debian\$#URIs: https://snapshot.debian.org/archive/debian/${ts}#" \
  "$src"
grep -q "^URIs: https://snapshot.debian.org/archive/debian/${ts}\$" "$src" \
  && grep -q "^URIs: https://snapshot.debian.org/archive/debian-security/${sec}\$" "$src" \
  || { echo "apt-snapshot.sh: unexpected layout of $src:" >&2; cat "$src" >&2; exit 1; }
# a snapshot is old on purpose: its Release files are past their "Valid-Until" – the signatures are
# still checked as usual
printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "5";\nAcquire::http::Timeout "60";\n' \
  > /etc/apt/apt.conf.d/99assetlotse-snapshot
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get -y -q -o Dpkg::Options::=--force-confold full-upgrade
apt-get clean
rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99assetlotse-snapshot
mv /tmp/debian.sources.orig "$src"
