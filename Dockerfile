# Base image pinned exactly: Python 3.13.16 on Debian 13 (trixie), frozen to this exact
# state by digest (same on amd64 and arm64).
# For a new base image change tag and digest together and run the tests.
ARG PYTHON_IMAGE=python:3.13.16-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="AssetLotse" \
      org.opencontainers.image.version="1.8.2" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    ASSETLOTSE_DATA_DIR=/data \
    HOME=/tmp \
    ASSETLOTSE_PORT=9343 \
    PUID=1000 \
    PGID=1000

WORKDIR /app
COPY requirements.txt .
# pin pip itself, then install exactly the lock file – without pulling in sub-dependencies
# (--no-deps) – and finally check that everything fits together.
RUN pip install pip==26.2.2 \
 && pip install --no-deps --only-binary=:all: -r requirements.txt \
 && pip check
# Time zone: TZ (e.g. Europe/Berlin) needs /usr/share/zoneinfo (Debian package tzdata, included
# in the python-slim image). If it is missing, the build reports it here and AssetLotse at start-up.
RUN test -e /usr/share/zoneinfo/Europe/Berlin \
 || echo "WARNING: /usr/share/zoneinfo is missing in the base image – TZ has no effect, times in UTC."

COPY assetlotse ./assetlotse
COPY wsgi.py gunicorn.conf.py LICENSE ./
# docs/: manuals (PDF)
COPY docs ./docs
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh && mkdir -p /data
# Installation files: "./update.sh --pull" takes them out of the image, so a server that pulls
# ready-made images keeps its update.sh, docker-compose.yml etc. up to date (last layer: small).
COPY docker-compose.yml Dockerfile docker-entrypoint.sh update.sh requirements.txt requirements.in \
     .env.example .dockerignore README.md README.en.md CHANGELOG.md ./

# The container starts as root, fixes the permissions of /data and
# then switches to PUID:PGID in the entrypoint (no root while running).
VOLUME ["/data"]
# Port: ASSETLOTSE_PORT (default 9343). gunicorn.conf.py, the healthcheck and docker-compose.yml
# all read this one variable – so host and container port are always the same.
EXPOSE 9343

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/login' % (os.environ.get('ASSETLOTSE_PORT') or '9343').strip('\"\' '), timeout=4)" || exit 1
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "wsgi:app"]
