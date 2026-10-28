#!/usr/bin/env bash
# ------------------------------------------------------------------
# AssetLotse – rebuild and update the container
#
#   ./update.sh                       rebuild the installed version locally and restart
#   ./update.sh assetlotse-X.Y.Z.zip  install a new package, then build locally and start
#   ./update.sh --pull                install the newest ready-made image from GitHub (ghcr.io)
#
# Ready-made images (built by GitHub Actions, see .github/workflows/docker.yml):
#   --pull           pull the image instead of building it (also updates update.sh & co.)
#   --image ADDRESS  other image address (default ghcr.io/soleil0815/assetlotse, saved in .env)
#   --version X.Y.Z  pin a specific version instead of following the newest (also to go back);
#                    the next plain --pull follows the newest version again
#
# With --pull the container follows the image tag "latest", so other update tools can install
# new versions as well. AssetLotse backs up the database itself whenever a
# new version starts for the first time.
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
HEALTH_TIMEOUT=180   # seconds to wait for "healthy" (the first start of a new version also makes a backup)

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

usage() { awk 'NR>2 && /^# ---/{exit} NR>2{sub(/^# ?/,""); print}' "$0"; exit 0; }

# ---------------------------------------------------------------- Arguments
BACKUP=1; NOCACHE=""; PRUNE=0; YES=0; ZIP=""; CLEANUP=1; CLEANUP_ONLY=0
AFTER_SWAP=0; HANDED_OLD_VERSION=""; HANDED_ZIP=""
PULL=0; WANT_IMAGE=""; WANT_VERSION=""; PULLED_REF=""; PINNED=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-backup)  BACKUP=0 ;;
        --no-cache)   NOCACHE="--no-cache" ;;
        --prune)      PRUNE=1 ;;
        --no-cleanup) CLEANUP=0 ;;
        --cleanup)    CLEANUP_ONLY=1 ;;
        -y|--yes)     YES=1 ;;
        --pull)       PULL=1 ;;
        --image)      [ "$#" -ge 2 ] || die "--image needs an address, e.g. --image ghcr.io/owner/repo"
                      WANT_IMAGE="$2"; shift ;;
        --version)    [ "$#" -ge 2 ] || die "--version needs a version number, e.g. --version 1.7.0"
                      WANT_VERSION="$2"; shift ;;
        --pinned)     PINNED=1 ;;      # internal: handover, the version was given with --version
        # internal: the image the old script pulled (handover after the swap)
        --pulled)     [ "$#" -ge 2 ] || die "Internal error during handover."
                      PULL=1; PULLED_REF="$2"; shift ;;
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
[ "$CLEANUP_ONLY" = 0 ] || { [ -z "$ZIP" ] && [ "$PULL" = 0 ]; } || die "--cleanup only cleans up – please call it without a package or --pull."
[ "$PULL" = 0 ] || [ -z "$ZIP" ] || die "Either a package or --pull, not both."
[ "$PULL" = 1 ] || [ -z "$WANT_IMAGE$WANT_VERSION" ] || die "--image and --version only work together with --pull."
if [ -n "$WANT_IMAGE" ]; then
    WANT_IMAGE="${WANT_IMAGE%/}"
    case "${WANT_IMAGE##*/}" in
        *:*) die "Please give the image address without a version and the version with --version (e.g. --image ghcr.io/owner/repo --version 1.7.0)." ;;
    esac
    # registry (optionally with port) / path, lower case – e.g. ghcr.io/owner/repo or localhost:5000/team/app
    [[ "$WANT_IMAGE" =~ ^[a-z0-9]([a-z0-9._-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)+$ ]] \
        || die "Invalid image address: $WANT_IMAGE  (lower case, e.g. ghcr.io/owner/repo)"
fi
# a program version (1.7.0) or one of its security updates (1.7.0-s2: same program, newer packages)
[ -z "$WANT_VERSION" ] || [[ "$WANT_VERSION" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+(-s[0-9]+)?$ ]] || die "Invalid version: $WANT_VERSION  (e.g. 1.7.0 or 1.7.0-s2)"
WANT_VERSION="${WANT_VERSION#v}"

# Resolve the package path BEFORE changing into the program folder (also relative paths like ../x.zip)
if [ -n "$ZIP" ]; then
    [ -f "$ZIP" ] || die "File not found: $ZIP"
    ZIP="$(readlink -f -- "$ZIP")"
fi
INSTALL_DIR="$(dirname "$(readlink -f -- "$0")")"
cd "$INSTALL_DIR" || die "Program folder not accessible: $INSTALL_DIR"
# Only one update at a time (cron and a manual run must not race). The new script after the handover
# inherits the lock (file descriptor 9) and does not take it again.
if [ "$AFTER_SWAP" = 0 ] && command -v flock >/dev/null 2>&1; then
    exec 9>>"$INSTALL_DIR/.assetlotse.lock"
    flock -n 9 || die "Another update is running right now. Please wait until it has finished."
fi

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
    sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=[[:space:]]*//p" .env 2>/dev/null | tail -n1 \
        | sed 's/[[:space:]]*#.*$//; s/^["'\'']//; s/["'\'']$//; s/[[:space:]]*$//' || true
}
set_env() {      # set KEY=VALUE in .env (replace existing lines, otherwise append)
    local key="$1" value="$2"
    [ -f .env ] || ( umask 077; printf '# AssetLotse – your own settings (all options: .env.example)\n' > .env )
    if grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" .env; then
        # --follow-symlinks: a symlinked .env stays a symlink
        sed -i --follow-symlinks -E "s|^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=.*|${key}=${value}|" .env
    else
        [ -z "$(tail -c1 .env)" ] || echo >> .env
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
}
unset_env() {    # remove KEY=… lines from .env (no-op if absent)
    [ -f .env ] && grep -Eq "^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=" .env || return 0
    sed -i --follow-symlinks -E "/^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=/d" .env
}
# Which image is running now: built locally (assetlotse) or ready-made (ghcr.io/…)
OLD_IMAGE="$(env_value ASSETLOTSE_IMAGE)"; OLD_IMAGE="${OLD_IMAGE:-assetlotse}"
# Where the ready-made images live (GitHub Actions of the AssetLotse repository); --image overrides it
DEFAULT_REGISTRY_IMAGE="ghcr.io/soleil0815/assetlotse"
REGISTRY_IMAGE="$(env_value ASSETLOTSE_REGISTRY_IMAGE)"
REGISTRY_IMAGE="${REGISTRY_IMAGE:-$DEFAULT_REGISTRY_IMAGE}"
KNOWN_REGISTRY_IMAGE="$REGISTRY_IMAGE"

OLD_VERSION="$(version_of .)"
if [ "$AFTER_SWAP" = 1 ]; then
    OLD_VERSION="$HANDED_OLD_VERSION"        # the files have already been swapped
fi
if [ -z "$OLD_VERSION" ] && [ "$AFTER_SWAP" = 0 ] && [ "$CLEANUP_ONLY" = 0 ]; then
    if [ -z "$ZIP" ] && [ "$PULL" = 0 ]; then
        die "No AssetLotse installation found in $INSTALL_DIR (assetlotse/__init__.py is missing). Install a package with:  ./update.sh assetlotse-X.Y.Z.zip"
    fi
    warn "No complete installation found (assetlotse/__init__.py is missing) – the package will be installed in full."
fi
if [ -z "$ZIP" ] && [ "$PULL" = 0 ]; then
    [ -f docker-compose.yml ] || die "docker-compose.yml not found – this script belongs in the AssetLotse folder."
fi
# A server that runs ready-made images is never switched to a local build by accident (the local files
# may be older than the running image, which an update tool may already have moved on).
if [ -z "$ZIP" ] && [ "$PULL" = 0 ] && [ "$AFTER_SWAP" = 0 ] && [ "$CLEANUP_ONLY" = 0 ] && [ "$OLD_IMAGE" != assetlotse ]; then
    die "This server runs ready-made images ($OLD_IMAGE). Update with:  ./update.sh --pull   – or build locally from a package:  ./update.sh assetlotse-X.Y.Z.zip"
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
TMP_CID=""; RESTORE_LATEST=""
cleanup() {
    local rc=$?
    if [ -n "$TMP_CID" ]; then docker rm -fv "$TMP_CID" >/dev/null 2>&1 || true; fi
    if [ "$SWAP_STATE" = running ]; then
        rollback || true
    fi
    if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then
        if [ "$SWAP_STATE" = failed ]; then
            # never deleted by a later cleanup (that only removes .assetlotse-update.*)
            local keep="$INSTALL_DIR/assetlotse-restore-$(date +%Y%m%d-%H%M%S)"
            mv -- "$STAGE" "$keep" 2>/dev/null || keep="$STAGE"
            warn "The previous files are kept in: $keep/old – copy them back by hand."
        else
            rm -rf -- "$STAGE"
        fi
    fi
    # A pull that was refused or failed must not leave a moved "latest" behind
    if [ -n "$RESTORE_LATEST" ] && [ "$rc" != 0 ]; then
        if [ "$RESTORE_LATEST" = none ]; then
            docker image rm "${REGISTRY_IMAGE}:latest" >/dev/null 2>&1 || true
        else
            docker tag "$RESTORE_LATEST" "${REGISTRY_IMAGE}:latest" >/dev/null 2>&1 || true
        fi
    fi
    return "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

die_swap() {   # after rollback: say what really happened
    if [ "$SWAP_STATE" = failed ]; then
        die "Installing failed ($1) and the previous files could not all be restored – see above."
    fi
    die "Installing failed ($1) – the previous version is unchanged."
}

rollback() {   # swap back entries that were already replaced (in reverse order)
    # Only entries that were demonstrably placed (PLACED) or moved away (MOVED_OLD) are touched –
    # if moving an entry away failed, the original stays untouched in its place.
    # Entries are recorded right BEFORE each mv, so an interrupt between mv and bookkeeping loses nothing:
    # a placed entry is only removed if its old version is safe in old/ (or never existed), and an old
    # entry is only moved back if it really is in old/.
    local i name ok=1
    warn "Error while installing – restoring the previous files …"
    for (( i=${#PLACED[@]}-1; i>=0; i-- )); do
        name="${PLACED[$i]}"
        [ -e "$SRC/$name" ] && continue                 # mv never happened – ./name is still the original
        rm -rf -- "./$name" || ok=0
    done
    for (( i=${#MOVED_OLD[@]}-1; i>=0; i-- )); do
        name="${MOVED_OLD[$i]}"
        [ -e "$STAGE/old/$name" ] || [ -L "$STAGE/old/$name" ] || continue
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
    local current="$1" n=0 f v keep_prev="" ref active repo
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
    # 4) Docker: old AssetLotse images – locally built (assetlotse) and ready-made (ghcr.io/…), only tags
    #    that are full version numbers. Kept: the running one and the previous one as a fallback (--prune: only
    #    the running one).
    local active_repo keep_ref="" refs="" r best=""
    active_repo="$(env_value ASSETLOTSE_IMAGE)"; active_repo="${active_repo:-assetlotse}"
    active="${active_repo}:${current}"
    for repo in assetlotse "$REGISTRY_IMAGE"; do
        r="$(docker image ls --format '{{.Repository}}:{{.Tag}}' "$repo" 2>/dev/null || true)"
        refs+="$(grep -E ':[0-9]+\.[0-9]+\.[0-9]+(-s[0-9]+)?$' <<<"$r" || true)"$'\n'
    done
    if [ -n "${OLD_VERSION:-}" ] && [ "$OLD_VERSION" != "$current" ] \
            && grep -qxF "${OLD_IMAGE}:${OLD_VERSION}" <<<"$refs"; then
        keep_ref="${OLD_IMAGE}:${OLD_VERSION}"        # the image that ran before this update
    else
        # unknown (e.g. --cleanup only): the newest older image, preferably from the same source
        for repo in "$active_repo" any; do
            while read -r ref; do
                [ -n "$ref" ] || continue
                [ "$repo" = any ] || [ "${ref%:*}" = "$repo" ] || continue
                v="${ref##*:}"
                version_lt "$v" "$current" || continue
                if [ -z "$best" ] || version_lt "${best##*:}" "$v"; then best="$ref"; fi
            done <<<"$refs"
            [ -z "$best" ] || break
        done
        keep_ref="$best"
    fi
    while read -r ref; do
        [ -n "$ref" ] || continue
        [ "$ref" = "$active" ] && continue
        if [ "$PRUNE" = 0 ] && [ "$ref" = "$keep_ref" ]; then continue; fi
        docker image rm "$ref" >/dev/null 2>&1 && echo "  removed: image $ref" && n=$((n+1)) || true
    done <<<"$refs"
    # "latest" is only a pointer for pulling – unless the container follows it
    if [ "$(env_value ASSETLOTSE_TAG)" != latest ]; then
        docker image rm "${REGISTRY_IMAGE}:latest" >/dev/null 2>&1 || true
    fi
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

# ---------------------------------------------------------------- 1. Check the new package / image
if [ "$PULL" = 1 ] && [ "$AFTER_SWAP" = 0 ]; then
    REGISTRY_IMAGE="${WANT_IMAGE:-$REGISTRY_IMAGE}"
    PULL_TAG="${WANT_VERSION:-latest}"
    if [ "$PULL_TAG" = latest ]; then
        # remember the local "latest" – a refused or failed pull puts it back (see cleanup)
        RESTORE_LATEST="$(docker image inspect -f '{{.Id}}' "${REGISTRY_IMAGE}:latest" 2>/dev/null || true)"
        RESTORE_LATEST="${RESTORE_LATEST:-none}"
    fi
    # which build runs now (e.g. 1.7.0-s2) – for the way back after the update (survives the handover)
    if [ "$OLD_IMAGE" != assetlotse ]; then
        OLD_BUILD="$(docker image inspect -f '{{index .Config.Labels "io.assetlotse.build"}}' "${OLD_IMAGE}:$(env_value ASSETLOTSE_TAG)" 2>/dev/null || true)"
        [[ "$OLD_BUILD" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-s[0-9]+)?$ ]] && export ASSETLOTSE_OLD_BUILD="$OLD_BUILD"
    fi
    step "Pulling ready-made image: ${REGISTRY_IMAGE}:${PULL_TAG}"
    docker pull "${REGISTRY_IMAGE}:${PULL_TAG}" \
        || die "Pull failed – nothing changed. Is the address/version right? For a private repository log in once:  docker login ghcr.io -u <github-user>  (password: a token with read:packages)."
    NEW_VERSION="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' "${REGISTRY_IMAGE}:${PULL_TAG}" 2>/dev/null || true)"
    [[ "$NEW_VERSION" =~ ^[0-9]+(\.[0-9]+)*$ ]] || die "The image has no valid version label – nothing changed."
    PULLED_REF="${REGISTRY_IMAGE}:${NEW_VERSION}"
    if [[ "$PULL_TAG" == *-s* ]]; then        # a pinned security update keeps its own tag
        [ "${PULL_TAG%-s*}" = "$NEW_VERSION" ] || die "Image ${PULL_TAG} has the version label ${NEW_VERSION} – nothing changed."
        PULLED_REF="${REGISTRY_IMAGE}:${PULL_TAG}"
    fi
    # Trust: the image's update.sh will run here as root. Check that it was built from the expected
    # repository and show what was pulled; a new image address always needs an explicit "yes".
    SOURCE="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.source"}}' "${REGISTRY_IMAGE}:${PULL_TAG}" 2>/dev/null || true)"
    DIGEST="$(docker image inspect -f '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "${REGISTRY_IMAGE}:${PULL_TAG}" 2>/dev/null || true)"
    if [[ "$REGISTRY_IMAGE" == ghcr.io/* ]]; then
        EXPECTED_SOURCE="https://github.com/${REGISTRY_IMAGE#ghcr.io/}"
        [ "${SOURCE,,}" = "${EXPECTED_SOURCE,,}" ] \
            || die "The image was not built from ${EXPECTED_SOURCE} (source label: ${SOURCE:-none}) – nothing changed."
    fi
    echo "Image:   ${REGISTRY_IMAGE}:${PULL_TAG}  →  version ${NEW_VERSION}"
    [ -z "$SOURCE" ] || echo "Source:  ${SOURCE}"
    [ -z "$DIGEST" ] || echo "Digest:  ${DIGEST##*@}"
    if [ "$REGISTRY_IMAGE" != "$KNOWN_REGISTRY_IMAGE" ]; then
        warn "New image address: ${REGISTRY_IMAGE} (so far: ${KNOWN_REGISTRY_IMAGE}). Its update.sh will run on this server with your rights."
        YES_SAVED="$YES"; YES=0
        confirm "Trust this image?" || die "Aborted – nothing changed."
        YES="$YES_SAVED"
    fi
    if [ "${REGISTRY_IMAGE}:${PULL_TAG}" != "$PULLED_REF" ]; then
        docker tag "${REGISTRY_IMAGE}:${PULL_TAG}" "$PULLED_REF" || die "Could not tag the image – nothing changed."
    fi
    # The image carries update.sh, docker-compose.yml & co. – take them out and install them like a package
    STAGE="$(mktemp -d "$INSTALL_DIR/.assetlotse-update.XXXXXX")" \
        || die "No write access to $INSTALL_DIR – nothing changed."
    mkdir "$STAGE/new" "$STAGE/old"
    TMP_CID="$(docker create "$PULLED_REF")" || { TMP_CID=""; die "Could not read the image – nothing changed."; }
    docker cp "$TMP_CID:/app/." "$STAGE/new/" >/dev/null 2>&1 \
        || die "Could not copy the installation files out of the image – nothing changed."
    docker rm -fv "$TMP_CID" >/dev/null 2>&1 || true     # -v: also the anonymous volume for /data
    TMP_CID=""
fi
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
fi
if { [ -n "$ZIP" ] || [ "$PULL" = 1 ]; } && [ "$AFTER_SWAP" = 0 ]; then
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
    # everything else the package's own Dockerfile copies – checked BEFORE the files are swapped
    for f in requirements.in .env.example .dockerignore README.md README.en.md CHANGELOG.md gunicorn.conf.py LICENSE \
             build/apt-snapshot.sh build/os-install.sh build/debian-packages.lock; do
        grep -qF "$f" "$SRC/Dockerfile" || continue
        [ -f "$SRC/$f" ] || die "The package is incomplete ($f is missing) – nothing changed."
    done
    bash -n "$SRC/update.sh" 2>/dev/null || die "The package is damaged (update.sh) – nothing changed."
    NEW_VERSION="$(version_of "$SRC")"
    [ -n "$NEW_VERSION" ] || die "Cannot read the version number in the package – nothing changed."
    PULLED_TAG="${PULLED_REF##*:}"
    if [ "$PULL" = 1 ] && [ "$NEW_VERSION" != "${PULLED_TAG%-s*}" ]; then
        die "The files in the image ($NEW_VERSION) do not match its version label (${PULLED_TAG%-s*}) – nothing changed."
    fi
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
if { [ -n "$ZIP" ] || [ "$PULL" = 1 ]; } && [ "$AFTER_SWAP" = 0 ]; then
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
            MOVED_OLD+=("$name")
            mv -- "./$name" "$STAGE/old/$name" || { rollback; die_swap "$name"; }
        fi
        if [ -e "$SRC/$name" ]; then
            PLACED+=("$name")
            mv -- "$SRC/$name" "./$name" || { rollback; die_swap "$name"; }
        fi
    done
    [ "$(version_of .)" = "$NEW_VERSION" ] || { rollback; die_swap "check"; }
    SWAP_STATE=done
    chmod +x ./update.sh ./docker-entrypoint.sh 2>/dev/null || true
    rm -rf -- "$STAGE"; STAGE=""
    ok "Files of version ${NEW_VERSION} installed."
    # From here the NEW update.sh takes over (build, start, clean up) – so improvements to the
    # script already apply to this update. Older packages without this handover just carry on here.
    if grep -q -- '--continue-after-swap' ./update.sh 2>/dev/null; then
        # Only options the new script knows – going back to an older version must not fail on a new option
        knows() { grep -q -- "$1)" ./update.sh 2>/dev/null; }
        PASS=()
        [ -n "$NOCACHE" ] && PASS+=(--no-cache)
        [ "$PRUNE" = 1 ] && PASS+=(--prune)
        [ "$CLEANUP" = 0 ] && knows --no-cleanup && PASS+=(--no-cleanup)
        [ "$YES" = 1 ] && PASS+=(-y)
        [ "$PULL" = 1 ] && knows --pulled && PASS+=(--pulled "$PULLED_REF")
        [ -n "$WANT_VERSION" ] && knows --pinned && PASS+=(--pinned)
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

# Image source in .env: ready-made from GitHub, or built locally
if [ "$PULL" = 1 ]; then
    REGISTRY_IMAGE="${PULLED_REF%:*}"
    PULLED_TAG="${PULLED_REF##*:}"
    [ "$VERSION" = "${PULLED_TAG%-s*}" ] || die "Version mismatch: files $VERSION, image $PULLED_REF."
    set_env ASSETLOTSE_REGISTRY_IMAGE "$REGISTRY_IMAGE"
    set_env ASSETLOTSE_IMAGE "$REGISTRY_IMAGE"
    if [ "$PINNED" = 1 ] || [ -n "$WANT_VERSION" ]; then
        # pinned: update tools leave it alone until the next plain ./update.sh --pull
        set_env ASSETLOTSE_TAG "$PULLED_TAG"
        ok "Pinned to version ${PULLED_TAG}. Back to the newest version with:  ./update.sh --pull"
    else
        # follows "latest": ./update.sh --pull or another update tool installs new versions
        docker tag "$PULLED_REF" "${REGISTRY_IMAGE}:latest" >/dev/null 2>&1 || true
        set_env ASSETLOTSE_TAG latest
    fi
    unset_env ASSETLOTSE_DOCKHAND_UPDATE        # setting of 1.8.0, no longer used
elif [ -n "$(env_value ASSETLOTSE_IMAGE)$(env_value ASSETLOTSE_TAG)" ]; then
    # Built locally: never under the GitHub name – an update tool would replace it with the GitHub image
    if [ "$OLD_IMAGE" != assetlotse ]; then
        warn "Switching from the ready-made image ($OLD_IMAGE) to a locally built one. Back to GitHub images with:  ./update.sh --pull"
    fi
    set_env ASSETLOTSE_IMAGE assetlotse
    set_env ASSETLOTSE_TAG "$VERSION"
    unset_env ASSETLOTSE_DOCKHAND_UPDATE
    [ "$OLD_IMAGE" = assetlotse ] || warn "Update tools cannot update a locally built image – they only work after  ./update.sh --pull"
fi
if [ -n "$OLD_VERSION" ] && [ -f "packages/assetlotse-${OLD_VERSION}.zip" ]; then
    GO_BACK="./update.sh packages/assetlotse-${OLD_VERSION}.zip"
elif [ "$OLD_IMAGE" != assetlotse ] && [ -n "$OLD_VERSION" ]; then
    # exactly the build that ran before (e.g. 1.7.0-s2), not the unpatched original release
    GO_BACK="./update.sh --pull --version ${ASSETLOTSE_OLD_BUILD:-$OLD_VERSION}"
else
    GO_BACK="./update.sh packages/assetlotse-OLD.zip"
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
OLD_IMAGE_ID=""
[ -z "$OLD_VERSION" ] || OLD_IMAGE_ID="$(docker image inspect -f '{{.Id}}' "${OLD_IMAGE}:${OLD_VERSION}" 2>/dev/null || true)"
UP_ARGS=(-d --remove-orphans)
if [ "$PULL" = 1 ]; then
    step "Using ready-made image ${PULLED_REF}"
    ok "Nothing to build."
    UP_ARGS+=(--no-build)
else
    step "Building image assetlotse:${VERSION}"
    # No --pull needed: the base image is pinned by digest in the Dockerfile.
    "${DC[@]}" build $NOCACHE "$SERVICE" \
        || die "Build failed – the running container is unchanged (but the files are new). To go back:  ${GO_BACK}"
    ok "Image built."
fi

# ---------------------------------------------------------------- 5. Start
step "Restarting the container"
"${DC[@]}" up "${UP_ARGS[@]}" "$SERVICE" \
    || die "Could not start the container (see the message above). To go back:  ${GO_BACK}"

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
    die "The container did not start healthy. To go back:  ${GO_BACK}"
fi

# ---------------------------------------------------------------- 6. Clean up
if [ -n "$ZIP" ]; then
    archive_package "$ZIP" "$VERSION"
fi
if [ "$CLEANUP" = 1 ]; then
    cleanup_install "$VERSION"
    if [ "$PRUNE" = 0 ] && [ -n "$OLD_IMAGE_ID" ] && [ "$OLD_VERSION" != "$VERSION" ]; then
        echo "The previous version is kept as a fallback (go back with:  ${GO_BACK})."
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
