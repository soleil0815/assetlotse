#!/usr/bin/env bash
# ------------------------------------------------------------------
# AssetLotse – rebuild and update the container
#
#   ./update.sh                       rebuild the current version and restart
#   ./update.sh assetlotse-X.Y.Z.zip  install a new package, then build and start
#
# Options:
#   --no-backup   do not make a backup first
#   --no-cache    build the image without the Docker cache
#   --prune       delete all old AssetLotse images (otherwise the previous one is kept as a fallback)
#   --no-cleanup  do not clean up after the update
#   --cleanup     clean up only (no build, no restart)
#   -y, --yes     no questions
#   -h, --help    this help
#
# After a successful update the script cleans up: the installed zip goes into the
# archive ./packages (current and previous package are kept for going back); loose
# packages in the program folder, leftovers of aborted updates and older AssetLotse
# images are removed. Never touched: ./data (database, photos, keys, backups),
# .env and docker-compose.override.yml.
# ------------------------------------------------------------------

# Can also be called as "sh update.sh": then restart with Bash automatically.
if [ -z "${BASH_VERSION:-}" ]; then
    if command -v bash >/dev/null 2>&1; then
        exec bash "$0" "$@"
    fi
    echo "This script needs Bash. Please run:  bash update.sh" >&2
    exit 1
fi

set -euo pipefail

# The whole script lives in main(): Bash reads it completely before running it –
# important, because a new package replaces this very file.
main() {
SERVICE=assetlotse
HEALTH_TIMEOUT=120   # seconds to wait for "healthy"

# ---------------------------------------------------------------- Output
if [ -t 1 ]; then
    B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'
else
    B=""; G=""; Y=""; R=""; N=""
fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s✔ %s%s\n' "$G" "$*" "$N"; }
warn() { printf '%s! %s%s\n' "$Y" "$*" "$N"; }
die()  { printf '%s✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# ---------------------------------------------------------------- Arguments
BACKUP=1; NOCACHE=""; PRUNE=0; YES=0; ZIP=""; CLEANUP=1; CLEANUP_ONLY=0
AFTER_SWAP=0; HANDED_OLD_VERSION=""; HANDED_ZIP=""
ARGS=("$@")
while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-backup)  BACKUP=0 ;;
        --no-cache)   NOCACHE="--no-cache" ;;
        --prune)      PRUNE=1 ;;
        --no-cleanup) CLEANUP=0 ;;
        --cleanup)    CLEANUP_ONLY=1 ;;
        -y|--yes)     YES=1 ;;
        -h|--help)    usage ;;
        # internal: handover from the old to the new script after the files were swapped
        --continue-after-swap)
            [ "$#" -ge 3 ] || die "Internal error during handover."
            AFTER_SWAP=1; HANDED_OLD_VERSION="$2"; HANDED_ZIP="$3"; shift 2 ;;
        -*)           die "Unknown option: $1  (help: ./update.sh --help)" ;;
        *)            [ -z "$ZIP" ] || die "Please give only one package."; ZIP="$1" ;;
    esac
    shift
done
[ "$CLEANUP_ONLY" = 0 ] || [ -z "$ZIP" ] || die "--cleanup only cleans up – please call it without a package."

# Resolve the package path BEFORE changing into the program folder (also relative paths like ../x.zip)
if [ -n "$ZIP" ]; then
    [ -f "$ZIP" ] || die "File not found: $ZIP"
    ZIP="$(readlink -f -- "$ZIP")"
fi
INSTALL_DIR="$(dirname "$(readlink -f -- "$0")")"
cd "$INSTALL_DIR" || die "Program folder not accessible: $INSTALL_DIR"

confirm() {
    [ "$YES" = 1 ] && return 0
    # Without a terminal (cron, ssh without -t) and without -y: abort rather than assume "yes"
    [ -t 0 ] || { echo "$1 – no input possible, aborted (run with -y to skip questions)." >&2; return 1; }
    read -r -p "$1 [Y/n] " a
    case "${a:-y}" in [yYjJ]*) return 0 ;; *) return 1 ;; esac
}

# ---------------------------------------------------------------- Requirements
command -v docker >/dev/null 2>&1 || die "Docker is not installed."
# Compose: the "docker compose" plugin or the older standalone "docker-compose" – any version.
# A specific program can be forced with ASSETLOTSE_COMPOSE (e.g. ASSETLOTSE_COMPOSE=docker-compose).
if [ -n "${ASSETLOTSE_COMPOSE:-}" ]; then
    read -r -a DC <<< "$ASSETLOTSE_COMPOSE"
    "${DC[@]}" version >/dev/null 2>&1 || die "ASSETLOTSE_COMPOSE=${ASSETLOTSE_COMPOSE} does not work."
elif docker compose version >/dev/null 2>&1; then
    DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
else
    die "Docker Compose is missing – install either \"docker compose\" (plugin) or \"docker-compose\"."
fi
DC_BASE=("${DC[@]}")
# Reuse the project name of the running container – otherwise e.g. a symlinked folder
# (/opt/assetlotse → /srv/…) would give a different Compose project and "up" would fail on the container name.
PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' assetlotse 2>/dev/null || true)"
[ -n "$PROJECT" ] && DC=("${DC_BASE[@]}" -p "$PROJECT")
docker info >/dev/null 2>&1 || die "No access to Docker. Run as root or as a member of the \"docker\" group."

version_of() {   # program version of an AssetLotse folder (empty if there is none)
    local f="$1/assetlotse/__init__.py"
    [ -f "$f" ] || return 0
    sed -n 's/^__version__ = "\(.*\)"/\1/p' "$f" 2>/dev/null | head -n1 || true
}
version_lt() {   # $1 < $2 (version numbers)?
    [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$1" ]
}
env_value() {    # last value of a variable in .env (without quotes/spaces)
    [ -f .env ] || return 0
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" .env 2>/dev/null | tail -n1 \
        | sed 's/[[:space:]]*#.*$//; s/^["'\'']//; s/["'\'']$//; s/[[:space:]]*$//' || true
}
OLD_VERSION="$(version_of .)"
if [ "$AFTER_SWAP" = 1 ]; then
    OLD_VERSION="$HANDED_OLD_VERSION"        # the files have already been swapped
fi
if [ -z "$OLD_VERSION" ] && [ "$AFTER_SWAP" = 0 ] && [ "$CLEANUP_ONLY" = 0 ]; then
    if [ -z "$ZIP" ]; then
        die "No AssetLotse installation found in $INSTALL_DIR (assetlotse/__init__.py is missing). Install a package with:  ./update.sh assetlotse-X.Y.Z.zip"
    fi
    warn "No complete installation found (assetlotse/__init__.py is missing) – the package will be installed in full."
fi
if [ -z "$ZIP" ]; then
    [ -f docker-compose.yml ] || die "docker-compose.yml not found – this script belongs in the AssetLotse folder."
fi

container_running() {
    # the old docker-compose does not know "ps --status" – so ask Docker directly
    local cid
    cid="$("${DC[@]}" ps -q "$SERVICE" 2>/dev/null | head -n1)"
    [ -n "$cid" ] && [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" = "true" ]
}

# Never touched when installing a package (pakete = archive folder of 1.6.2, renamed to packages)
KEEP_RE='^(data|instance|packages|pakete|\.env|docker-compose\.override\.yml|\.assetlotse-update\..*)$'

STAGE=""; SWAP_STATE=""; MOVED_OLD=(); PLACED=()
cleanup() {
    local rc=$?
    if [ "$SWAP_STATE" = running ]; then
        rollback || true
    fi
    if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then
        if [ "$SWAP_STATE" = failed ]; then
            warn "The previous files are still kept in: $STAGE/old"
        else
            rm -rf -- "$STAGE"
        fi
    fi
    return "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

rollback() {   # swap back entries that were already replaced (in reverse order)
    # Only entries that were demonstrably placed (PLACED) or moved away (MOVED_OLD) are touched –
    # if moving an entry away failed, the original stays untouched in its place.
    local i name ok=1
    warn "Error while installing – restoring the previous files …"
    for (( i=${#PLACED[@]}-1; i>=0; i-- )); do
        rm -rf -- "./${PLACED[$i]}" || ok=0
    done
    for (( i=${#MOVED_OLD[@]}-1; i>=0; i-- )); do
        name="${MOVED_OLD[$i]}"
        mv -- "$STAGE/old/$name" "./$name" || ok=0
    done
    if [ "$ok" = 1 ]; then
        SWAP_STATE=restored
        warn "Previous version restored – nothing changed."
    else
        SWAP_STATE=failed
        printf '%s✘ Restore incomplete! Previous files: %s/old – please copy them back by hand.%s\n' "$R" "$STAGE" "$N" >&2
    fi
}

# ---------------------------------------------------------------- Cleanup (functions)
PKG_DIR="$INSTALL_DIR/packages"
OLD_PKG_DIR="$INSTALL_DIR/pakete"   # name of the archive folder in 1.6.2
KEEP_PACKAGES=2      # current + previous package (for going back)

pkg_version() {      # version of an AssetLotse package (without unpacking it)
    unzip -p "$1" '*assetlotse/__init__.py' 2>/dev/null | sed -n 's/^__version__ = "\(.*\)"/\1/p' | head -n1 || true
}

migrate_pkg_dir() {  # 1.6.2 called the archive "pakete" – rename it once
    if [ -d "$OLD_PKG_DIR" ] && [ ! -e "$PKG_DIR" ]; then
        mv -- "$OLD_PKG_DIR" "$PKG_DIR" && echo "  renamed: pakete/ → packages/"
    fi
    return 0
}

archive_package() {  # put the installed package into the archive ./packages
    local zip="$1" ver="$2" target
    [ -n "$zip" ] && [ -f "$zip" ] || return 0
    migrate_pkg_dir
    mkdir -p "$PKG_DIR" || { warn "Cannot create archive folder $PKG_DIR – the package stays where it is."; return 0; }
    target="$PKG_DIR/assetlotse-${ver}.zip"
    [ "$(readlink -f -- "$zip")" = "$(readlink -f -- "$target" 2>/dev/null || true)" ] && return 0
    if [ "$(dirname "$(readlink -f -- "$zip")")" = "$INSTALL_DIR" ]; then
        mv -f -- "$zip" "$target" && echo "  package archived: packages/${target##*/}"
        [ -f "$zip.sha256" ] && mv -f -- "$zip.sha256" "$target.sha256"
    else
        cp -f -- "$zip" "$target" && echo "  package archived: packages/${target##*/} (original stays in ${zip%/*})"
        [ -f "$zip.sha256" ] && cp -f -- "$zip.sha256" "$target.sha256"
    fi
    return 0
}

cleanup_install() {  # after a successful update, or with --cleanup
    local current="$1" n=0 f v keep_prev="" ref
    step "Cleaning up"
    migrate_pkg_dir
    # 1) leftovers of aborted updates (work folders)
    for f in "$INSTALL_DIR"/.assetlotse-update.*; do
        [ -d "$f" ] || continue
        rm -rf -- "$f" && echo "  removed: ${f##*/} (leftover of an aborted update)" && n=$((n+1))
    done
    # 2) loose packages in the program folder: into the archive (or delete if already archived)
    for f in "$INSTALL_DIR"/assetlotse-*.zip; do
        [ -f "$f" ] || continue
        v="$(pkg_version "$f")"
        if [ -z "$v" ]; then
            warn "  ${f##*/}: not recognisable as an AssetLotse package – left in place."
            continue
        fi
        if [ -f "$PKG_DIR/assetlotse-${v}.zip" ]; then
            rm -f -- "$f" "$f.sha256" && echo "  removed: ${f##*/} (already in the archive)" && n=$((n+1))
        else
            mkdir -p "$PKG_DIR" && mv -f -- "$f" "$PKG_DIR/assetlotse-${v}.zip" \
                && echo "  archived: ${f##*/} → packages/" && n=$((n+1))
            [ -f "$f.sha256" ] && mv -f -- "$f.sha256" "$PKG_DIR/assetlotse-${v}.zip.sha256"
        fi
    done
    # checksum files without their package
    for f in "$INSTALL_DIR"/assetlotse-*.zip.sha256 "$PKG_DIR"/assetlotse-*.zip.sha256; do
        [ -f "$f" ] && [ ! -f "${f%.sha256}" ] || continue
        rm -f -- "$f" && echo "  removed: ${f##*/} (package no longer there)" && n=$((n+1))
    done
    # 3) archive: keep only the current and the next older package
    if [ -d "$PKG_DIR" ]; then
        local versions=() keep=()
        while IFS= read -r v; do versions+=("$v"); done < <(
            for f in "$PKG_DIR"/assetlotse-*.zip; do [ -f "$f" ] || continue; v="${f##*/assetlotse-}"; echo "${v%.zip}"; done | sort -V)
        # keep: current and previous version (the last installed one, otherwise the highest older one in the archive)
        keep=("$current")
        if [ -n "${OLD_VERSION:-}" ] && [ "$OLD_VERSION" != "$current" ]; then
            keep_prev="$OLD_VERSION"
        else
            for v in "${versions[@]}"; do
                if version_lt "$v" "$current"; then keep_prev="$v"; fi
            done
        fi
        [ -n "$keep_prev" ] && keep+=("$keep_prev")
        for v in "${versions[@]}"; do
            if [[ " ${keep[*]} " != *" $v "* ]] && ! version_lt "$current" "$v"; then
                rm -f -- "$PKG_DIR/assetlotse-${v}.zip" "$PKG_DIR/assetlotse-${v}.zip.sha256" && echo "  removed: packages/assetlotse-${v}.zip (older)" && n=$((n+1))
            fi
        done
    fi
    # 4) Docker: old AssetLotse images (the previous one is kept as a fallback, all of them with --prune)
    if [ -z "$keep_prev" ] && [ -n "${OLD_VERSION:-}" ] && [ "$OLD_VERSION" != "$current" ]; then
        keep_prev="$OLD_VERSION"
    fi
    local refs
    refs="$(docker image ls --format '{{.Repository}}:{{.Tag}}' assetlotse 2>/dev/null | grep -v ':<none>$' || true)"
    if [ -z "$keep_prev" ]; then
        # no archive and no known previous version (e.g. --cleanup only): keep the newest older image
        while read -r ref; do
            v="${ref#assetlotse:}"
            [ -n "$ref" ] && version_lt "$v" "$current" || continue
            if [ -z "$keep_prev" ] || version_lt "$keep_prev" "$v"; then keep_prev="$v"; fi
        done <<<"$refs"
    fi
    while read -r ref; do
        [ -n "$ref" ] || continue
        v="${ref#assetlotse:}"
        [ "$v" = "$current" ] && continue
        if [ "$PRUNE" = 0 ] && [ -n "$keep_prev" ] && [ "$v" = "$keep_prev" ]; then continue; fi
        docker image rm "$ref" >/dev/null 2>&1 && echo "  removed: image $ref" && n=$((n+1)) || true
    done <<<"$refs"
    # dangling (untagged) AssetLotse images, e.g. from aborted builds – only ours, nothing else on the server
    docker image prune -f --filter "label=org.opencontainers.image.title=AssetLotse" >/dev/null 2>&1 || true
    if [ "$n" = 0 ]; then ok "Nothing to clean up."; else ok "Cleaned up ($n)."; fi
}

if [ "$CLEANUP_ONLY" = 1 ]; then
    CUR="$(version_of .)"
    [ -n "$CUR" ] || die "No AssetLotse installation found in $INSTALL_DIR."
    cleanup_install "$CUR"
    exit 0
fi

# ---------------------------------------------------------------- 1. Check the new package
if [ -n "$ZIP" ] && [ "$AFTER_SWAP" = 0 ]; then
    step "Checking new package: $ZIP"
    command -v unzip >/dev/null 2>&1 || die "unzip is missing (e.g. sudo apt install unzip)."
    # work folder inside the program folder: same file system, so swapping is just renaming
    STAGE="$(mktemp -d "$INSTALL_DIR/.assetlotse-update.XXXXXX")" \
        || die "No write access to $INSTALL_DIR – nothing changed."
    mkdir "$STAGE/new" "$STAGE/old"
    # From here on work only with our own copy: checking and unpacking apply to the same file,
    # even if someone replaces the original in between.
    PKG="$STAGE/package.zip"
    cp -- "$ZIP" "$PKG" || die "Could not read the package – nothing changed."
    SUM="$(sha256sum -- "$PKG" | cut -d' ' -f1)"
    if [ -f "$ZIP.sha256" ]; then
        EXPECTED="$(tr -s ' \t' ' ' < "$ZIP.sha256" | cut -d' ' -f1 | tr 'A-F' 'a-f' | head -n1)"
        [ "$SUM" = "$EXPECTED" ] || die "Checksum does not match $(basename "$ZIP").sha256 – package modified or incomplete. Nothing changed."
        ok "Checksum matches (SHA-256)."
    else
        echo "SHA-256: $SUM  (compare with the value published with the download)"
    fi
    unzip -tq "$PKG" >/dev/null 2>&1 || die "The package is damaged (unzip -t) – nothing changed."
    # No absolute paths, no "..", no symbolic links in the package.
    # Read the listing completely first (grep -q in a pipe can wrongly report "not found" under pipefail)
    LISTING="$(unzip -Z "$PKG" 2>/dev/null || true)"
    NAMES="$(unzip -Z1 "$PKG" 2>/dev/null || true)"
    if grep -E '^/|(^|/)\.\.(/|$)' <<<"$NAMES" >/dev/null; then
        die "The package contains invalid paths – nothing changed."
    fi
    if grep '^l' <<<"$LISTING" >/dev/null; then
        die "The package contains symbolic links – nothing changed."
    fi
    unzip -q "$PKG" -d "$STAGE/new" || die "Could not unpack the package (disk space?) – nothing changed."
    rm -f -- "$PKG"
    [ -z "$(find "$STAGE/new" ! -type f ! -type d -print -quit)" ] \
        || die "The package contains special files – nothing changed."
    chmod -R go-w "$STAGE/new"         # never take over permissions from the zip: only the owner may write
    SRC=""
    if [ -f "$STAGE/new/assetlotse/__init__.py" ]; then
        SRC="$STAGE/new"                     # package without a top-level folder
    else
        for d in "$STAGE/new"/*/; do
            if [ -f "${d}assetlotse/__init__.py" ]; then
                [ -z "$SRC" ] || die "Unexpected package layout (several program folders) – nothing changed."
                SRC="${d%/}"
            fi
        done
    fi
    [ -n "$SRC" ] || die "The package does not contain AssetLotse – nothing changed."
    for f in docker-compose.yml Dockerfile docker-entrypoint.sh wsgi.py requirements.txt update.sh; do
        [ -f "$SRC/$f" ] || die "The package is incomplete ($f is missing) – nothing changed."
    done
    bash -n "$SRC/update.sh" 2>/dev/null || die "The package is damaged (update.sh) – nothing changed."
    NEW_VERSION="$(version_of "$SRC")"
    [ -n "$NEW_VERSION" ] || die "Cannot read the version number in the package – nothing changed."
    echo "Installed: ${OLD_VERSION:-none}   Package: ${NEW_VERSION}"
    if [ -z "$OLD_VERSION" ]; then
        :
    elif [ "$NEW_VERSION" = "$OLD_VERSION" ]; then
        warn "Same version as installed."
    elif [ "$(printf '%s\n%s\n' "$OLD_VERSION" "$NEW_VERSION" | sort -V | tail -n1)" = "$OLD_VERSION" ]; then
        warn "The package is OLDER than the installed version. An older version may have problems with a newer database."
    fi
    confirm "Replace the files now?" || die "Aborted – nothing changed."
fi

# ---------------------------------------------------------------- 2. Backup (before replacing anything)
if [ "$BACKUP" = 1 ] && [ "$AFTER_SWAP" = 0 ]; then
    step "Backup before the update"
    if container_running; then
        # via the entrypoint: it drops root, so the backup belongs to PUID:PGID
        if "${DC[@]}" exec -T "$SERVICE" docker-entrypoint.sh flask --app wsgi backup; then
            ok "Backup created (Administration → Backup)."
        else
            warn "Backup failed (normal with MySQL/PostgreSQL – back up with mysqldump/pg_dump there)."
            confirm "Continue anyway?" || die "Aborted – nothing changed."
        fi
    else
        warn "Container is not running – no backup possible (first installation?)."
    fi
fi

# ---------------------------------------------------------------- 3. Swap the files
if [ -n "$ZIP" ] && [ "$AFTER_SWAP" = 0 ]; then
    step "Installing the files of version ${NEW_VERSION}"
    # Replace the program folder completely (deleted files disappear too);
    # everything personal stays: data, .env, docker-compose.override.yml, instance.
    # Only entries inside the program folder are renamed – the folder itself (permissions, owner) stays.
    ENTRIES=()
    while IFS= read -r -d '' path; do
        name="${path##*/}"
        [[ "$name" =~ $KEEP_RE ]] || ENTRIES+=("$name")
    done < <(find "$SRC" -mindepth 1 -maxdepth 1 -print0)
    for name in assetlotse tests; do         # also remove old program folders the package no longer has
        if [ -e "./$name" ] && [ ! -e "$SRC/$name" ]; then ENTRIES+=("$name"); fi
    done
    SWAP_STATE=running
    for name in "${ENTRIES[@]}"; do
        if [ -e "./$name" ] || [ -L "./$name" ]; then
            mv -- "./$name" "$STAGE/old/$name" || { rollback; die "Installing failed ($name) – the previous version is unchanged."; }
            MOVED_OLD+=("$name")
        fi
        if [ -e "$SRC/$name" ]; then
            mv -- "$SRC/$name" "./$name" || { rollback; die "Installing failed ($name) – the previous version is unchanged."; }
            PLACED+=("$name")
        fi
    done
    [ "$(version_of .)" = "$NEW_VERSION" ] || { rollback; die "Installing failed (check) – the previous version is unchanged."; }
    SWAP_STATE=done
    chmod +x ./update.sh ./docker-entrypoint.sh 2>/dev/null || true
    rm -rf -- "$STAGE"; STAGE=""
    ok "Files of version ${NEW_VERSION} installed."
    # From here the NEW update.sh takes over (build, start, clean up) – so improvements to the
    # script already apply to this update. Older packages without this handover just carry on here.
    if grep -q -- '--continue-after-swap' ./update.sh 2>/dev/null; then
        PASS=()
        [ -n "$NOCACHE" ] && PASS+=(--no-cache)
        [ "$PRUNE" = 1 ] && PASS+=(--prune)
        [ "$CLEANUP" = 0 ] && PASS+=(--no-cleanup)
        [ "$YES" = 1 ] && PASS+=(-y)
        trap - EXIT
        exec bash ./update.sh "${PASS[@]}" --continue-after-swap "${OLD_VERSION:-}" "$ZIP"
    fi
fi
[ "$AFTER_SWAP" = 1 ] && ZIP="$HANDED_ZIP"
VERSION="$(version_of .)"
[ -f docker-compose.yml ] || die "docker-compose.yml not found – this script belongs in the AssetLotse folder."
# docker-compose.yml reads .env – if it is missing, create an empty one (template: .env.example)
if [ ! -f .env ]; then
    ( umask 077; printf '# AssetLotse – your own settings (all options: .env.example)\n' > .env ) \
        && ok ".env created (empty – the defaults apply)." \
        || die "Could not create .env."
fi

# Check the port in .env before building (otherwise the container would not start)
PORT_CHECK="$(env_value ASSETLOTSE_PORT)"
if [ -n "$PORT_CHECK" ]; then
    case "$PORT_CHECK" in
        *[!0-9]*) die "ASSETLOTSE_PORT=${PORT_CHECK} in .env is not a number – please fix it (e.g. 9343)." ;;
    esac
    { [ "$PORT_CHECK" -ge 1024 ] && [ "$PORT_CHECK" -le 65535 ]; } \
        || die "ASSETLOTSE_PORT=${PORT_CHECK} in .env is not allowed – please choose 1024–65535 (put a reverse proxy in front for 80/443)."
fi

# ---------------------------------------------------------------- 4. Build
step "Building image assetlotse:${VERSION}"
OLD_IMAGE_ID=""
[ -z "$OLD_VERSION" ] || OLD_IMAGE_ID="$(docker image inspect -f '{{.Id}}' "assetlotse:${OLD_VERSION}" 2>/dev/null || true)"
# No --pull needed: the base image is pinned by digest in the Dockerfile.
"${DC[@]}" build $NOCACHE "$SERVICE" || die "Build failed – the running container is unchanged."
ok "Image built."

# ---------------------------------------------------------------- 5. Start
step "Restarting the container"
"${DC[@]}" up -d --remove-orphans "$SERVICE" \
    || die "Could not start the container (see the message above). To go back: install the previous package with ./update.sh packages/assetlotse-OLD.zip"

printf 'Waiting for start-up '
STATUS=""
for _ in $(seq 1 "$HEALTH_TIMEOUT"); do
    CID="$("${DC[@]}" ps -q "$SERVICE" 2>/dev/null || true)"
    if [ -n "$CID" ]; then
        STATUS="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CID" 2>/dev/null || true)"
        case "$STATUS" in
            healthy) break ;;
            unhealthy|exited|dead) break ;;
        esac
    fi
    printf '.'
    sleep 1
done
echo

if [ "$STATUS" = healthy ]; then
    ok "AssetLotse ${VERSION} is running."
else
    warn "Status: ${STATUS:-unknown}. The last log lines:"
    "${DC[@]}" logs --tail 40 "$SERVICE" || true
    die "The container did not start healthy. To go back: install the previous package with ./update.sh packages/assetlotse-OLD.zip"
fi

# ---------------------------------------------------------------- 6. Clean up
if [ -n "$ZIP" ]; then
    archive_package "$ZIP" "$VERSION"
fi
if [ "$CLEANUP" = 1 ]; then
    cleanup_install "$VERSION"
    if [ "$PRUNE" = 0 ] && [ -n "$OLD_IMAGE_ID" ] && [ "$OLD_VERSION" != "$VERSION" ]; then
        echo "The previous image and package are kept as a fallback (go back with: ./update.sh packages/assetlotse-${OLD_VERSION}.zip)."
    fi
fi

PORT="$(env_value ASSETLOTSE_PORT)"
BIND="$(env_value ASSETLOTSE_BIND)"
HOST="$(hostname -f 2>/dev/null || hostname)"
case "$BIND" in
    127.*|localhost|::1) HOST="$BIND"
        warn "ASSETLOTSE_BIND=${BIND} in .env: AssetLotse is only reachable on this server (for a reverse proxy here). Comment the line out for access from the network." ;;
    ""|0.0.0.0) ;;
    *) HOST="$BIND" ;;
esac
if [ -z "$PORT" ] && [ -n "$OLD_VERSION" ] && version_lt "$OLD_VERSION" "1.2.1"; then
    warn "New default port 9343 (previously 9300). Update your bookmarks – or set ASSETLOTSE_PORT=9300 in .env and run ./update.sh again."
fi
printf '\n%sDone.%s  %s → %s   http://%s:%s\n' "$B" "$N" "${OLD_VERSION:-new}" "$VERSION" "$HOST" "${PORT:-9343}"
}

main "$@"
exit
