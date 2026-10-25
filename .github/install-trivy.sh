#!/usr/bin/env bash
# Installs Trivy in a FIXED version and checks the download against a FIXED SHA-256 before it is run.
# Deliberately no GitHub Action: trivy-action and setup-trivy were compromised in March 2026
# (CVE-2026-33634). To update: take the new version's line for Linux-64bit.tar.gz from
# trivy_<version>_checksums.txt of the official release and change BOTH values here together.
#   install-trivy.sh <target directory>      -> <target directory>/trivy
set -euo pipefail
TRIVY_VERSION="0.75.0"
TRIVY_SHA256="c6e65abddb348e25f10549df887045629cf28cc72453cd1c63acb717316b3f3f"   # trivy_0.75.0_Linux-64bit.tar.gz
dest="${1:?usage: install-trivy.sh <target directory>}"
url="https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz"
mkdir -p "$dest"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL --retry 3 -o "$tmp/trivy.tar.gz" "$url"
if ! echo "${TRIVY_SHA256}  $tmp/trivy.tar.gz" | sha256sum -c --quiet -; then
  echo "::error title=Trivy::download does not match the fixed SHA-256 – not run"
  exit 1
fi
tar -xzf "$tmp/trivy.tar.gz" -C "$dest" trivy
"$dest/trivy" --version | head -n 1
