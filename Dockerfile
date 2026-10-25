# Base image: Python 3.13 on Debian 13 (trixie) – exact patch release AND digest, so every build starts
# from exactly the same image. scripts/lock.py sets new values, but only ever within Python 3.13 and
# Debian trixie; another Python line or Debian release is a deliberate change (see README, "Base").
ARG PYTHON_IMAGE=python:3.13.17-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="AssetLotse" \
      org.opencontainers.image.version="1.10.1" \
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

# Debian packages: ALL packages of the image (also those of the base image) exactly as listed in
# build/debian-packages.lock – one fixed snapshot of the Debian archive. The build stops if anything differs.
COPY build/apt-snapshot.sh build/os-install.sh build/debian-packages.lock /tmp/build/
RUN sh /tmp/build/os-install.sh && rm -rf /tmp/build

WORKDIR /app
COPY requirements.txt requirements.in ./
# Python packages: pip in a fixed version installs exactly the lock file – without pulling in anything
# else (--no-deps) – and checks that everything fits together. Then pip is removed: AssetLotse does not
# need a package manager at runtime (and pip carries its own, often older copies of urllib3 & co.).
ARG PIP_VERSION=26.2.2
RUN PIP_ROOT_USER_ACTION=ignore pip install "pip==${PIP_VERSION}" \
 && PIP_ROOT_USER_ACTION=ignore pip install --no-deps --only-binary=:all: -r requirements.txt \
 && pip check \
 && PIP_ROOT_USER_ACTION=ignore pip uninstall -y pip \
 && rm -rf /usr/local/lib/python3.*/ensurepip \
 && ! python -c "import pip" 2>/dev/null \
 && ! python -c "import ensurepip" 2>/dev/null
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
COPY build ./build

# The container starts as root, fixes the permissions of /data and
# then switches to PUID:PGID in the entrypoint (no root while running).
VOLUME ["/data"]
# Port: ASSETLOTSE_PORT (default 9343). gunicorn.conf.py, the healthcheck and docker-compose.yml
# all read this one variable – so host and container port are always the same.
EXPOSE 9343

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/login' % (os.environ.get('ASSETLOTSE_PORT') or '9343').strip('\"\' '), timeout=4)" || exit 1
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "wsgi:app"]
