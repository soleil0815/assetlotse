"""WSGI entry point: gunicorn wsgi:app (port from ASSETLOTSE_PORT, see gunicorn.conf.py)"""
import os
import sys

from assetlotse import create_app

try:
    app = create_app()
except SystemExit as exc:
    # AssetLotse refuses to start (e.g. the key does not match the database). Exit code 4 tells gunicorn
    # that restarting the workers does not help – otherwise they would be restarted endlessly.
    if exc.code not in (None, 0):
        print(exc.code if isinstance(exc.code, str) else f"AssetLotse: start-up aborted ({exc.code})",
              file=sys.stderr)
        sys.exit(4)
    raise

if __name__ == "__main__":
    # Development/testing only. Production: gunicorn (Linux) or waitress (Windows)
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT") or os.environ.get("ASSETLOTSE_PORT") or "9343"),
            debug=os.environ.get("FLASK_DEBUG") == "1")
