#!/usr/bin/env bash
# Scans an image (saved with "docker save") with Trivy: vulnerabilities HIGH/CRITICAL that already have a
# fix, plus secrets. Exceptions: .trivyignore (each with reason and expiry date – checked first) and, with
# GRACE_DIR set, the automatic, time-limited one for findings the published image has as well.
# Shows the findings in the run summary and as annotations; exit code 1 if there are any.
#   trivy-scan.sh <image.tar> <heading>        (trivy must be on PATH: .github/install-trivy.sh)
set -euo pipefail
tar_file="${1:?usage: trivy-scan.sh <image.tar> <heading>}"
heading="${2:-Trivy}"
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/.." && pwd)"
python3 "$root/scripts/ci.py" check-trivyignore "$root/.trivyignore"
rc=0
trivy image --input "$tar_file" --scanners vuln,secret --severity HIGH,CRITICAL --ignore-unfixed \
  --ignorefile "$root/.trivyignore" --format json --output trivy.json --exit-code 0 --quiet || rc=$?
if [ "$rc" != 0 ] || [ ! -s trivy.json ]; then
  echo "::error title=Trivy::the scan itself failed (exit code ${rc}) – often a temporary download problem of the vulnerability database: run the workflow again"
  exit 1
fi
# automatic, time-limited exception (pull requests, releases, security updates – not the daily scan): what the
# published image has in exactly the same version does not stop the run (scripts/ci.py trivy-grace)
if [ -n "${GRACE_DIR:-}" ] && [ -f "${GRACE_DIR}/published" ]; then
  python3 "$root/scripts/ci.py" trivy-grace trivy.json --lock "${GRACE_DIR}/debian-packages.lock" \
    --requirements "${GRACE_DIR}/requirements.txt" --published "$(cat "${GRACE_DIR}/published")" \
    --published-date "$(cat "${GRACE_DIR}/published-date")"
fi
HEADING="$heading" "$here/trivy-report.sh" trivy.json
