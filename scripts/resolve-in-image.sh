#!/bin/sh
# Runs INSIDE a candidate base image (started by scripts/lock.py): updates all Debian packages to the
# snapshot, lists them, and resolves the newest Python packages within the version limits.
#   resolve-in-image.sh <snapshot> <security snapshot> <pip spec>   (requirements.in, constraints.txt in /work)
# Output: blocks between "@@<name>-begin" / "@@<name>-end" lines on stdout; everything else goes to stderr.
set -eu
snap="$1"; sec="$2"; pip_spec="$3"
. /etc/os-release
sh /lock/apt-snapshot.sh "$snap" "$sec" >&2
echo "@@debian ${VERSION_ID} ${VERSION_CODENAME}"
echo "@@arch $(dpkg --print-architecture)"
echo "@@packages-begin"; dpkg-query -W -f '${Package} ${Version}\n' | LC_ALL=C sort; echo "@@packages-end"
python -c 'import sys; print("@@python", sys.version.split()[0])'
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
python -m pip install -q --upgrade "$pip_spec" >&2
echo "@@pip $(python -m pip --version | cut -d' ' -f2)"
python -m pip install -q --dry-run --ignore-installed --only-binary=:all: --report /tmp/report.json \
  -r /work/requirements.in -c /work/constraints.txt >&2
echo "@@report-begin"; cat /tmp/report.json; echo; echo "@@report-end"
