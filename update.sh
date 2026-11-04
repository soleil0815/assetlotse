#!/bin/sh
# ------------------------------------------------------------------
# AssetLotse – update by hand
#
# Usually not needed: a container manager (Dockhand, Watchtower, Tugtainer …) or
#   docker compose pull && docker compose up -d
# does the same. AssetLotse backs up its database itself before a new version changes it.
#
#   ./update.sh                    newest version (follows "latest" – incl. the weekly security updates)
#   ./update.sh --version 2.0.0    a fixed version (written to .env; also to go back)
#   ./update.sh --offline FILE     without internet: image file from the release (assetlotse-2.0.0-amd64.tar.gz)
#   ./update.sh --rollback         start the image that ran before the last update again
#   ./update.sh --cleanup          list program files of older installations that are no longer needed,
#                                  ask, then delete them (never data, backups, .env or docker-compose.yml)
#
# Options:
#   -y, --yes     no questions (also: start the previous version again by itself if the new one fails)
#   -h, --help    this help
#
# What it does: remember the running image as "previous" → pull the version → docker compose up -d → wait for
# the health check. If the new version does not start, it offers to start the previous one again. A newer
# docker-compose.yml from the image is put in place; the old one is kept as docker-compose.yml.alt.
# Runs with /bin/sh (dash, busybox – e.g. on a Synology NAS). Needs root or the docker group.
# ------------------------------------------------------------------

DEFAULT_IMAGE="ghcr.io/soleil0815/assetlotse"
SERVICE="assetlotse"
HEALTH_TIMEOUT="${ASSETLOTSE_HEALTH_TIMEOUT:-180}"
MOUNTINFO="${ASSETLOTSE_MOUNTINFO:-/proc/self/mountinfo}"   # mount points on this host (cleanup leaves them alone)
IMAGE_TITLE="AssetLotse"

if [ -t 1 ]; then B="$(printf '\033[1m')"; G="$(printf '\033[32m')"; Y="$(printf '\033[33m')"; R="$(printf '\033[31m')"; N="$(printf '\033[0m')"
else B=""; G=""; Y=""; R=""; N=""; fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s✔ %s%s\n' "$G" "$*" "$N"; }
warn() { printf '%s! %s%s\n' "$Y" "$*" "$N"; }
die()  { printf '%s✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }
usage() { sed -n '3,/^# ---/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0; }

YES=0; MODE=update; WANT=""; OFFLINE=""; PULLED_REF=""; PINNED=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        -y|--yes)    YES=1 ;;
        -h|--help)   usage ;;
        --version)   [ "$#" -ge 2 ] || die "--version needs a version, e.g. --version 2.0.0"; WANT="$2"; shift ;;
        --offline)   [ "$#" -ge 2 ] || die "--offline needs the image file from the release"; OFFLINE="$2"; MODE=offline; shift ;;
        --rollback)  MODE=rollback ;;
        --cleanup)   MODE=cleanup ;;
        # one-time handover from the update.sh of installations before 2.0.0 (it pulled the image already)
        --pinned)    PINNED=1 ;;
        --pull|--no-cache|--prune|--no-cleanup|--no-backup) ;;
        --pulled)    [ "$#" -ge 2 ] || die "Internal error during the handover."; PULLED_REF="$2"; shift ;;
        --continue-after-swap) MODE=handover; shift "$(( $# > 2 ? 2 : $# - 1 ))" ;;
        *)           die "Unknown option: $1  (help: ./update.sh --help)" ;;
    esac
    shift
done
case "$WANT" in
    ""|latest) ;;
    v[0-9]*) WANT="${WANT#v}" ;;
esac
case "$WANT" in
    ""|latest) ;;
    *[!0-9.s-]*|.*|*..*) die "Invalid version: $WANT (e.g. 2.0.0 or 2.0.0-s1)" ;;
esac

# a relative file name means: relative to where the script was called from
case "$OFFLINE" in ""|/*) ;; *) OFFLINE="$(pwd)/$OFFLINE" ;; esac
cd "$(dirname "$0")" || die "Cannot change into the folder of this script."
INSTALL_DIR="$(pwd -P)"
[ -f docker-compose.yml ] || die "docker-compose.yml not found – update.sh belongs next to it."

# ---------------------------------------------------------------- Helpers
confirm() {   # confirm "question" → 0 = yes
    [ "$YES" = 1 ] && return 0
    if [ ! -t 0 ]; then
        warn "$1 – no terminal for the answer; use -y."
        return 1
    fi
    printf '%s [y/N] ' "$1"
    read -r answer || return 1
    case "$answer" in y|Y|j|J|yes|ja|Ja) return 0 ;; *) return 1 ;; esac
}
env_value() {   # last value of KEY in .env (without quotes and comments)
    [ -f .env ] || return 0
    sed -n "s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}$1[[:space:]]*=[[:space:]]*//p" .env \
        | tail -n 1 | sed 's/[[:space:]]#.*$//; s/^["'\'']//; s/["'\'']$//; s/[[:space:]]*$//'
}
set_env() {     # KEY VALUE: replace existing lines, otherwise append – same file (owner and rights stay)
    tmp="$(mktemp "${INSTALL_DIR}/.env.XXXXXX")" || die "Cannot write in ${INSTALL_DIR}."
    if [ -f .env ]; then
        grep -v "^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}$1[[:space:]]*=" .env > "$tmp" || true
    fi
    printf '%s=%s\n' "$1" "$2" >> "$tmp"
    if cat "$tmp" > .env; then rm -f "$tmp"; else rm -f "$tmp"; die "Could not update .env."; fi
}
compose_version() {   # version of a docker-compose.yml (files before 2.0.0 have none → 1)
    v="$(sed -n 's/.*ASSETLOTSE_COMPOSE_VERSION:[[:space:]]*["'\'']\{0,1\}\([0-9][0-9]*\).*/\1/p' "$1" 2>/dev/null | head -n 1)"
    echo "${v:-1}"
}
image_label() {       # image_label REF LABEL
    docker image inspect -f "{{index .Config.Labels \"$2\"}}" "$1" 2>/dev/null || true
}
container_id() {      # also a stopped container ("ps -q" alone lists only running ones)
    { $DC ps -aq "$SERVICE" 2>/dev/null || $DC ps -q "$SERVICE" 2>/dev/null; } | head -n 1
}

if docker compose version >/dev/null 2>&1; then DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then DC="docker-compose"
else die "Docker Compose is missing (docker compose or docker-compose)."
fi
docker info >/dev/null 2>&1 || die "Docker is not reachable – run as root (sudo ./update.sh) or as a member of the docker group."
# The running container may belong to a compose project with another name (folder renamed or reached via a
# link) – use its project, otherwise "up" would start a second container next to it.
PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$SERVICE" 2>/dev/null || true)"
[ -z "$PROJECT" ] || DC="$DC -p $PROJECT"

# only one update at a time (by hand and from a timer, say)
LOCK="${INSTALL_DIR}/.update.lock"
TMPD=""; W=""
on_exit() { rm -rf "$TMPD" "$W"; rmdir "$LOCK" 2>/dev/null; }
if ! mkdir "$LOCK" 2>/dev/null; then
    die "Another update is running (or one was interrupted: then delete the folder ${LOCK})."
fi
trap on_exit EXIT
trap 'exit 1' INT TERM

if [ ! -f .env ]; then
    ( umask 077; printf '# AssetLotse – your own settings (all options: .env.example)\n' > .env ) || die "Could not create .env."
    ok ".env created (empty – the defaults apply)."
fi
IMAGE="$(env_value ASSETLOTSE_IMAGE)"
case "$IMAGE" in
    ""|assetlotse)   # empty, or built locally before 2.0.0 – from now on always the ready-made image
        IMAGE="$(env_value ASSETLOTSE_REGISTRY_IMAGE)"; IMAGE="${IMAGE:-$DEFAULT_IMAGE}" ;;
esac

# ---------------------------------------------------------------- Cleanup
# Only entries from this fixed list, only directly in this folder, only real files/folders (no links),
# never anything that is – or contains – data, backups, .env, docker-compose files or a folder that a
# container uses. Old installations had no timers or helper scripts on the host besides these files.
PROGRAM_ENTRIES="assetlotse build docs scripts tests __pycache__ packages pakete Dockerfile docker-entrypoint.sh
wsgi.py gunicorn.conf.py requirements.txt requirements.in .dockerignore README.md README.en.md CHANGELOG.md LICENSE"

real() {   # absolute path with every link resolved; empty if that is not possible
    r="$(realpath -m -- "$1" 2>/dev/null)" || r="$(realpath "$1" 2>/dev/null)" || r="$(readlink -f "$1" 2>/dev/null)" \
        || r="$( { [ -d "$1" ] && cd "$1" 2>/dev/null && pwd -P; } )" || r=""
    printf '%s\n' "$r"
}
inside() {  # inside A B → A is B or below B
    case "$1/" in "$2"/*) return 0 ;; *) return 1 ;; esac
}
protected_paths() {   # one resolved path per line
    for p in data instance .env docker-compose.yml docker-compose.yml.alt docker-compose.override.yml \
             docker-compose.override.yaml .env.example update.sh; do
        if [ -e "$p" ] || [ -L "$p" ]; then real "$p"; fi
    done
    for p in "$(env_value ASSETLOTSE_BACKUP_DIR)" "$(env_value ASSETLOTSE_DATA_DIR)"; do
        case "$p" in /*) real "$p" ;; esac
    done
    # everything that containers use (bind mounts and volumes) – also stopped ones. A mount of this folder itself
    # or of a folder above it (a container manager's stacks folder, a monitoring agent with "/") is left out:
    # it would protect every file here – data in here is protected by the list above and by its own mounts.
    for c in $(docker ps -aq 2>/dev/null); do
        docker inspect -f '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$c" 2>/dev/null
    done | while IFS= read -r m; do
        [ -n "$m" ] || continue
        r="$(real "$m")"
        [ -n "$r" ] && ! inside "$INSTALL_DIR" "$r" && printf '%s\n' "$r"
    done
    # … and what docker-compose.yml and docker-compose.override.yml mount, even when no container exists
    # (e.g. after "docker compose down"); "config" prints the sources as absolute paths
    compose_sources < "$W/config" | while IFS= read -r m; do
        r="$(real "$m")"
        [ -n "$r" ] && ! inside "$INSTALL_DIR" "$r" && printf '%s\n' "$r"
    done
}
compose_sources() {   # bind mount sources from "docker compose config" (long and short syntax)
    sed -n -e 's/^[[:space:]]*source:[[:space:]]*//p' \
           -e 's/^[[:space:]]*-[[:space:]]*\(["'\'']\{0,1\}\/[^:]*\):.*/\1/p' \
        | sed -e 's/^["'\'']//' -e 's/["'\'']$//' | grep '^/' || :
}
mount_points() {      # one mount point per line (\040 & Co. decoded); empty if unknown
    [ -r "$MOUNTINFO" ] || return 0
    awk '{print $5}' "$MOUNTINFO" | sed 's/\\\([0-7][0-7][0-7]\)/\\0\1/g' | while IFS= read -r m; do
        printf '%b\n' "$m"
    done
}
has_mount() {         # has_mount RESOLVED FILE_WITH_MOUNT_POINTS → a mount point is RESOLVED or below it
    while IFS= read -r m; do
        [ -n "$m" ] || continue
        if inside "$m" "$1"; then return 0; fi
    done < "$2"
    # without mount information (not Linux): a different file system below counts as a mount, too
    [ ! -d "$1" ] || [ "$(find "$1" 2>/dev/null | wc -l)" = "$(find "$1" -xdev 2>/dev/null | wc -l)" ] || return 0
    return 1
}
cleanup_candidates() {   # one name per line (names may contain spaces)
    for e in $PROGRAM_ENTRIES; do
        if [ -e "$e" ] || [ -L "$e" ]; then echo "$e"; fi
    done
    for e in assetlotse-[0-9]*.zip assetlotse-[0-9]*.zip.sha256 .assetlotse-update.* .update-tmp.*; do
        if [ -e "$e" ] || [ -L "$e" ]; then echo "$e"; fi
    done
    # unpacked packages (assetlotse-1.9.0/): only if they really are one and hold no database
    for e in assetlotse-[0-9]*; do
        [ -d "$e" ] && [ ! -L "$e" ] && [ -f "$e/assetlotse/__init__.py" ] || continue
        [ -z "$(find "$e" \( -name '*.db' -o -name '*.sqlite*' -o -name secret_key \) -print 2>/dev/null | head -n 1)" ] && echo "$e"
    done
}
is_protected() {   # is_protected RESOLVED FILE_WITH_PROTECTED → prints the protected path
    while IFS= read -r p; do
        [ -n "$p" ] || continue
        if inside "$1" "$p" || inside "$p" "$1"; then echo "$p"; return 0; fi
    done < "$2"
    return 1
}
run_cleanup() {
    step "Program files that are no longer needed (in ${INSTALL_DIR})"
    [ ! -e .git ] || die "This folder is a source checkout (.git) – cleanup is only for installations."
    W="$(mktemp -d)" || die "No temporary folder."
    $DC config > "$W/config" 2>/dev/null \
        || die "Could not read the compose configuration (${DC} config) – nothing deleted, so that nothing a container mounts is lost."
    protected_paths | sort -u > "$W/protected"
    mount_points > "$W/mounts"
    cleanup_candidates > "$W/candidates"
    : > "$W/list"; : > "$W/skipped"
    while IFS= read -r e; do
        if [ -L "$e" ]; then echo "  $e  (link – left alone)" >> "$W/skipped"; continue; fi
        r="$(real "$e")"
        if [ -z "$r" ] || [ "$r" = "$INSTALL_DIR" ] || ! inside "$r" "$INSTALL_DIR"; then
            echo "  $e  (outside this folder – left alone)" >> "$W/skipped"; continue
        fi
        if hit="$(is_protected "$r" "$W/protected")"; then
            echo "  $e  (protected: $hit)" >> "$W/skipped"; continue
        fi
        if has_mount "$r" "$W/mounts"; then
            echo "  $e  (is or contains a mount point – left alone)" >> "$W/skipped"; continue
        fi
        if [ -d "$r" ] && [ -n "$(find "$r" -type l -print 2>/dev/null | head -n 1)" ]; then
            # rm would not follow a link inside, but whoever put it there may want to keep the folder
            echo "  $e  (contains links – left alone)" >> "$W/skipped"; continue
        fi
        echo "$e" >> "$W/list"
    done < "$W/candidates"
    if [ -s "$W/skipped" ]; then warn "Not touched:"; cat "$W/skipped"; fi
    # images built locally before 2.0.0 (assetlotse:X.Y.Z) that no container uses
    docker ps -a --format '{{.Image}}' > "$W/used" 2>/dev/null || :
    docker image ls --format '{{.Repository}}:{{.Tag}}' assetlotse 2>/dev/null | grep -v ':<none>$' \
        | grep -vxF -f "$W/used" > "$W/images" || :
    if [ ! -s "$W/list" ] && [ ! -s "$W/images" ]; then ok "Nothing to clean up."; return 0; fi
    if [ -s "$W/list" ]; then echo "Will be deleted:"; sed 's/^/  /' "$W/list"; fi
    if [ -s "$W/images" ]; then echo "Old locally built images:"; sed 's/^/  /' "$W/images"; fi
    echo "Stays: data, backups, .env, docker-compose.yml (.alt/.override), .env.example, update.sh."
    confirm "Delete these?" || { echo "Nothing deleted."; return 0; }
    : > "$W/failed"
    while IFS= read -r e; do
        if rm -rf -- "./$e"; then echo "  removed: $e"; else echo "$e" >> "$W/failed"; fi
    done < "$W/list"
    while IFS= read -r ref; do
        docker image rm "$ref" >/dev/null 2>&1 && echo "  removed: image $ref"
    done < "$W/images"
    if [ -s "$W/failed" ]; then warn "Not (completely) removed:"; sed 's/^/  /' "$W/failed"; return 1; fi
    ok "Cleaned up."
}
if [ "$MODE" = cleanup ]; then run_cleanup || exit 1; exit 0; fi

# ---------------------------------------------------------------- Start and wait
wait_healthy() {   # → 0 when healthy; prints the status
    printf 'Waiting for the health check '
    i=0; st=""
    while [ "$i" -lt "$HEALTH_TIMEOUT" ]; do
        cid="$(container_id)"
        if [ -n "$cid" ]; then
            st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}} {{.RestartCount}}' "$cid" 2>/dev/null)"
            case "$st" in
                "healthy "*) echo; return 0 ;;
                unhealthy*|exited*|dead*) break ;;
                *" 0"|"") ;;
                *) break ;;                       # restarted: it crashed at least once
            esac
        fi
        printf '.'; sleep 1; i=$((i + 1))
    done
    echo
    warn "Status: ${st:-unknown}. The last log lines:"
    $DC logs --tail 30 "$SERVICE" 2>/dev/null || true
    return 1
}
start() { $DC up -d --remove-orphans "$SERVICE" || return 1; wait_healthy; }

running_image_id() {
    cid="$(container_id)"
    [ -n "$cid" ] && docker inspect -f '{{.Image}}' "$cid" 2>/dev/null
}
remember_previous() {   # the image that ran so far becomes IMAGE:previous – only if a different one comes now
    if [ -n "$PREV_ID" ] && [ "$PREV_ID" != "$1" ]; then
        docker tag "$PREV_ID" "${IMAGE}:previous" 2>/dev/null || PREV_ID=""
    fi
}
previous_compose() {    # the previous image's own docker-compose.yml back, if it differs from today's (2.0.0 and newer)
    t="$(mktemp -d "${INSTALL_DIR}/.update-tmp.XXXXXX")" || return 0
    c="$(docker create "${IMAGE}:previous" 2>/dev/null)" || { rm -rf "$t"; return 0; }
    docker cp "$c:/app/docker-compose.yml" "$t/docker-compose.yml" >/dev/null 2>&1
    docker rm -v "$c" >/dev/null 2>&1 || true
    if [ -s "$t/docker-compose.yml" ] && [ "$(compose_version "$t/docker-compose.yml")" -ge 2 ] \
            && [ "$(compose_version "$t/docker-compose.yml")" != "$(compose_version docker-compose.yml)" ]; then
        cp -p docker-compose.yml docker-compose.yml.alt && cat "$t/docker-compose.yml" > docker-compose.yml \
            && ok "docker-compose.yml of the previous version restored (the newer one is kept as docker-compose.yml.alt)."
    fi
    rm -rf "$t"
}
start_previous() {
    docker image inspect "${IMAGE}:previous" >/dev/null 2>&1 || die "There is no previous image to go back to."
    build="$(image_label "${IMAGE}:previous" io.assetlotse.build)"
    [ -n "$build" ] || build="$(image_label "${IMAGE}:previous" org.opencontainers.image.version)"
    [ -n "$build" ] || build=previous
    docker tag "${IMAGE}:previous" "${IMAGE}:${build}" || die "Could not tag the previous image."
    set_env ASSETLOTSE_IMAGE "$IMAGE"
    set_env ASSETLOTSE_TAG "$build"
    previous_compose
    step "Starting the previous version ${build}"
    if start; then
        ok "Version ${build} is running again – fixed in .env (ASSETLOTSE_TAG=${build}); update tools leave it alone."
        echo "Back to the newest version later with:  ./update.sh"
        return 0
    fi
    warn "The previous version does not start either. If the log above says the database comes from a newer"
    warn "version, restore the backup it names (docker compose run --rm assetlotse backups lists them)."
    return 1
}
if [ "$MODE" = rollback ]; then start_previous || exit 1; exit 0; fi

# ---------------------------------------------------------------- Get the image
PREV_ID="$(running_image_id)"
OLD_VERSION=""
[ -z "${PREV_ID:-}" ] || OLD_VERSION="$(image_label "$PREV_ID" io.assetlotse.build)"
case "$MODE" in
    offline)
        [ -f "$OFFLINE" ] || die "File not found: $OFFLINE"
        if [ -f "${OFFLINE}.sha256" ] && command -v sha256sum >/dev/null 2>&1; then
            want="$(cut -d' ' -f1 < "${OFFLINE}.sha256" | tr 'A-F' 'a-f')"
            have="$(sha256sum < "$OFFLINE" | cut -d' ' -f1)"
            [ "$want" = "$have" ] || die "Checksum does not match ${OFFLINE}.sha256 – file damaged or incomplete. Nothing changed."
            ok "Checksum matches (SHA-256)."
        fi
        step "Loading the image from $OFFLINE"
        out="$(docker load -i "$OFFLINE" 2>&1)" || die "docker load failed: $out"
        REF="$(printf '%s\n' "$out" | sed -n 's/^Loaded image: //p' | grep -v ':latest$' | head -n 1)"
        [ -n "$REF" ] || die "The file contains no tagged AssetLotse image: $out"
        IMAGE="${REF%:*}"; TAG="${REF##*:}"
        ;;
    handover)
        [ -n "$PULLED_REF" ] || die "Handover without an image – please run ./update.sh again."
        IMAGE="${PULLED_REF%:*}"
        if [ "$PINNED" = 1 ]; then TAG="${PULLED_REF##*:}"     # it was called with --version
        else TAG=latest; docker tag "$PULLED_REF" "${IMAGE}:latest" || die "Could not tag the image."; fi
        ;;
    *)
        TAG="${WANT:-latest}"
        step "Pulling ${IMAGE}:${TAG}"
        docker pull "${IMAGE}:${TAG}" || die "Pull failed – nothing changed. Is the version right? For a private image log in once:  docker login ghcr.io"
        ;;
esac
REF="${IMAGE}:${TAG}"
[ "$(image_label "$REF" org.opencontainers.image.title)" = "$IMAGE_TITLE" ] || die "${REF} is not an AssetLotse image – nothing changed."
remember_previous "$(docker image inspect -f '{{.Id}}' "$REF" 2>/dev/null)"
NEW_VERSION="$(image_label "$REF" io.assetlotse.build)"
[ -n "$NEW_VERSION" ] || NEW_VERSION="$(image_label "$REF" org.opencontainers.image.version)"
echo "Running now: ${OLD_VERSION:-–}   new: ${NEW_VERSION}"

# ---------------------------------------------------------------- docker-compose.yml and update.sh from the image
TMPD="$(mktemp -d "${INSTALL_DIR}/.update-tmp.XXXXXX")" || die "Cannot write in ${INSTALL_DIR}."
cid="$(docker create "$REF" 2>/dev/null)" || die "Could not read the image – nothing changed."
docker cp "$cid:/app/docker-compose.yml" "$TMPD/docker-compose.yml" >/dev/null 2>&1
docker cp "$cid:/app/update.sh" "$TMPD/update.sh" >/dev/null 2>&1
docker rm -v "$cid" >/dev/null 2>&1 || true
if [ -s "$TMPD/docker-compose.yml" ]; then
    have="$(compose_version docker-compose.yml)"; new="$(compose_version "$TMPD/docker-compose.yml")"
    if [ "$new" -gt "$have" ]; then
        cp -p docker-compose.yml docker-compose.yml.alt || die "Could not keep the old docker-compose.yml."
        cat "$TMPD/docker-compose.yml" > docker-compose.yml || die "Could not write docker-compose.yml."
        ok "New docker-compose.yml (version ${new}) – the previous one is kept as docker-compose.yml.alt."
    fi
fi

# ---------------------------------------------------------------- Start
set_env ASSETLOTSE_IMAGE "$IMAGE"
set_env ASSETLOTSE_TAG "$TAG"
case "$TAG" in
    latest) ;;
    *) ok "Fixed to version ${TAG} (.env). Back to the newest with:  ./update.sh" ;;
esac
step "Starting AssetLotse ${NEW_VERSION}"
if start; then
    ok "AssetLotse ${NEW_VERSION} is running."
else
    warn "The new version did not start healthy."
    if [ -n "${PREV_ID:-}" ] && [ "$PREV_ID" != "$(docker image inspect -f '{{.Id}}' "$REF" 2>/dev/null)" ]; then
        if confirm "Start the previous version again?"; then
            start_previous || exit 1
            exit 1
        fi
        echo "Later:  ./update.sh --rollback"
    fi
    exit 1
fi

# the new update.sh last – a new file (new inode), so this running script is not disturbed
if [ -s "$TMPD/update.sh" ] && ! cmp -s "$TMPD/update.sh" update.sh; then
    if sh -n "$TMPD/update.sh" 2>/dev/null; then
        cp "$TMPD/update.sh" "$TMPD/update.sh.new" && chmod 755 "$TMPD/update.sh.new" \
            && mv -f "$TMPD/update.sh.new" update.sh && ok "update.sh updated."
    fi
fi
# dangling AssetLotse images (an old "latest" after the pull) – the previous one keeps its tag
docker image prune -f --filter "label=org.opencontainers.image.title=${IMAGE_TITLE}" >/dev/null 2>&1 || true

rm -rf "$TMPD"; TMPD=""
if [ "$MODE" = handover ] || [ -n "$(cleanup_candidates)" ]; then
    echo
    warn "Program files of the old installation are still in this folder – they are no longer needed."
    echo "Show and delete them with:  ./update.sh --cleanup"
fi
PORT="$(env_value ASSETLOTSE_PORT)"
printf '\n%sDone.%s  %s → %s   port %s\n' "$B" "$N" "${OLD_VERSION:-new}" "$NEW_VERSION" "${PORT:-9343}"
