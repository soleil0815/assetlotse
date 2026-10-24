#!/bin/sh
# Runs INSIDE an AssetLotse image: lists Python version, Debian packages and Python packages.
# The weekly rebuild compares this list with the current "latest" – only if it differs is anything published.
set -eu
python -c 'import sys; print("python", sys.version.split()[0])'
if command -v dpkg-query >/dev/null 2>&1; then dpkg-query -W -f '${Package} ${Version}\n' | sort; fi
python - <<'PY'
import importlib.metadata as m
for name, version in sorted({(d.metadata["Name"].lower(), d.version) for d in m.distributions()}):
    print("py", name, version)
PY
