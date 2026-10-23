"""Gunicorn settings for AssetLotse (loaded by gunicorn automatically).

The port comes from ASSETLOTSE_PORT (default 9343) – the same value as in .env,
so host and container port always match.
"""
import os
import sys

DEFAULT_PORT = 9343


def _port() -> int:
    raw = (os.environ.get("ASSETLOTSE_PORT") or "").strip().strip('"').strip("'")
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        sys.exit(f"ERROR: ASSETLOTSE_PORT={raw!r} is not a number – please set a port number "
                 f"such as {DEFAULT_PORT} in .env.")
    if not 1024 <= port <= 65535:
        sys.exit(f"ERROR: ASSETLOTSE_PORT={port} is not allowed – please choose a port between 1024 and 65535 "
                 "(ports below 1024 need root; put a reverse proxy in front for 80/443).")
    return port


bind = f"0.0.0.0:{_port()}"
workers = 2
threads = 4
accesslog = "-"
