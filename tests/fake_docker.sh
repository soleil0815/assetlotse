#!/bin/sh
# A stand-in for docker (and docker compose) for the tests of update.sh – no Docker needed.
# State in $FAKE_STATE:
#   registry/<ref>          image id that "docker pull <ref>" gets (/ in refs as _)
#   images/<id>/labels      key=value lines;  images/<id>/files/…  files under /app;  images/<id>/health
#   tags/<ref>              local tags → image id
#   running                 image id of the running container (missing = no container)
#   mounts                  mount sources of the running container
#   local_images            "repository:tag" lines for "docker image ls assetlotse"
# Every call is appended to $FAKE_STATE/log.
S="$FAKE_STATE"
echo "docker $*" >> "$S/log"
key() { printf '%s' "$1" | tr '/' '_'; }
id_of() {   # ref or id → id
    if [ -f "$S/tags/$(key "$1")" ]; then cat "$S/tags/$(key "$1")"; return 0; fi
    [ -d "$S/images/$1" ] && { echo "$1"; return 0; }
    return 1
}
label() { sed -n "s/^$2=//p" "$S/images/$1/labels" 2>/dev/null; }
env_of() { sed -n "s/^$1=//p" .env 2>/dev/null | tail -n 1; }
mkdir -p "$S/tags"

if [ "$1" = compose ]; then
    shift
    [ -z "${FAKE_NO_PLUGIN:-}" ] || [ "$1" != version ] || exit 1
    while [ "$1" = -p ]; do shift 2; done
    case "$1" in
        version) exit 0 ;;
        ps) [ -f "$S/running" ] && echo cid1; exit 0 ;;
        logs) echo "fake log line"; [ ! -f "$S/logtext" ] || cat "$S/logtext"; exit 0 ;;
        up)
            image="$(env_of ASSETLOTSE_IMAGE)"; tag="$(env_of ASSETLOTSE_TAG)"
            id="$(id_of "${image:-ghcr.io/soleil0815/assetlotse}:${tag:-latest}")" || { echo "no such image" >&2; exit 1; }
            echo "$id" > "$S/running"
            echo "$id" >> "$S/started"
            exit 0 ;;
    esac
    exit 0
fi

case "$1" in
    info) exit 0 ;;
    pull)
        id="$(cat "$S/registry/$(key "$2")" 2>/dev/null)" || { echo "manifest unknown" >&2; exit 1; }
        echo "$id" > "$S/tags/$(key "$2")"; exit 0 ;;
    tag)
        id="$(id_of "$2")" || exit 1
        echo "$id" > "$S/tags/$(key "$3")"; exit 0 ;;
    create) id_of "$2" > "$S/created" || exit 1; echo tmpcid; exit 0 ;;
    cp)
        id="$(cat "$S/created")"; f="${2#tmpcid:/app/}"
        [ -f "$S/images/$id/files/$f" ] || exit 1
        cp "$S/images/$id/files/$f" "$3"; exit 0 ;;
    rm) exit 0 ;;
    load)
        file="$3"; ref="$(head -n 1 "$file" | cut -d' ' -f1)"; id="$(head -n 1 "$file" | cut -d' ' -f2)"
        [ -d "$S/images/$id" ] || { echo "invalid tar" >&2; exit 1; }
        echo "$id" > "$S/tags/$(key "$ref")"; echo "Loaded image: $ref"; exit 0 ;;
    ps)
        case "$*" in
            *--format*) [ ! -f "$S/running" ] || echo "ghcr.io/soleil0815/assetlotse:latest" ;;
            *) [ ! -f "$S/running" ] || echo cid1 ;;
        esac
        exit 0 ;;
    inspect)
        fmt="$3"; target="$4"
        case "$fmt" in
            *com.docker.compose.project*) [ -n "${FAKE_PROJECT:-}" ] || exit 1; echo "$FAKE_PROJECT" ;;
            "{{.Image}}") cat "$S/running" ;;
            *State.Health*)
                id="$(cat "$S/running")"; echo "$(cat "$S/images/$id/health" 2>/dev/null || echo healthy) 0" ;;
            *Mounts*) cat "$S/mounts" 2>/dev/null ;;
        esac
        exit 0 ;;
    image)
        case "$2" in
            inspect)
                if [ "$3" = -f ]; then fmt="$4"; ref="$5"; else fmt=""; ref="$3"; fi
                id="$(id_of "$ref")" || exit 1
                case "$fmt" in
                    "") ;;
                    "{{.Id}}") echo "$id" ;;
                    *Labels*) label "$id" "$(printf '%s' "$fmt" | sed 's/.*"\(.*\)".*/\1/')" ;;
                esac
                exit 0 ;;
            ls) cat "$S/local_images" 2>/dev/null; exit 0 ;;
            rm) echo "$3" >> "$S/removed_images"; exit 0 ;;
            prune) exit 0 ;;
        esac ;;
esac
exit 0
