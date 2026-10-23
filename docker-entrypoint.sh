#!/bin/sh
# Starts as root, fixes the permissions of the data directory
# (e.g. when Docker created ./data as root) and then starts the app
# without root rights as user PUID:PGID (default 1000:1000).
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DATA="${ASSETLOTSE_DATA_DIR:-/data}"

# New files (database, photos, keys) are readable by the app user only
umask 077

for v in "$PUID" "$PGID"; do
    case "$v" in
        ""|*[!0-9]*) echo "ERROR: PUID/PGID must be numbers (got: $PUID/$PGID)." >&2; exit 1 ;;
    esac
done
if [ "$PUID" -eq 0 ] || [ "$PGID" -eq 0 ]; then
    echo "ERROR: PUID/PGID 0 would run AssetLotse as root – please use a normal user." >&2
    exit 1
fi

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA" "$DATA/images"
    # Lock the data folder (database with personal data, keys) against other users of the server
    chmod 700 "$DATA" 2>/dev/null || echo "WARNING: cannot change the permissions of $DATA." >&2
    # Only fix when needed (spares large directories)
    if [ "$(stat -c %u "$DATA")" != "$PUID" ] || [ -n "$(find "$DATA" \! -user "$PUID" -print -quit)" ]; then
        echo "AssetLotse: setting owner of $DATA to $PUID:$PGID"
        chown -R "$PUID:$PGID" "$DATA" || echo "WARNING: cannot change all owners in $DATA (e.g. a NAS share inside the data folder) – give $PUID:$PGID write access there." >&2
    fi
    # Separate backup folder (ASSETLOTSE_BACKUP_DIR, e.g. a NAS drive) – also only when needed
    BACKUP="${ASSETLOTSE_BACKUP_DIR:-}"
    case "$BACKUP" in
        ""|"$DATA"|"$DATA"/*) ;;    # inside the data folder – already handled above
        *)
            # Without a mounted drive the backups would live inside the container and be lost on the next update
            if command -v mountpoint >/dev/null 2>&1 && ! mountpoint -q "$BACKUP" 2>/dev/null; then
                echo "WARNING: $BACKUP (ASSETLOTSE_BACKUP_DIR) is not a mounted drive – backups there will be lost on the next update. Check the volume in docker-compose.override.yml." >&2
            fi
            if ! mkdir -p "$BACKUP" 2>/dev/null; then
                echo "WARNING: cannot create the backup folder $BACKUP." >&2
            elif [ "$(stat -c %u "$BACKUP")" != "$PUID" ] || [ -n "$(find "$BACKUP" \! -user "$PUID" -print -quit)" ]; then
                echo "AssetLotse: setting owner of $BACKUP to $PUID:$PGID"
                chown -R "$PUID:$PGID" "$BACKUP" || echo "WARNING: cannot change the owner of $BACKUP (e.g. a NAS share) – give $PUID:$PGID write access there." >&2
            fi
            ;;
    esac
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --no-new-privs "$@"
fi

# Already running without root (e.g. with "user:" in the Compose file)
if [ ! -w "$DATA" ]; then
    echo "ERROR: $DATA is not writable for user $(id -u)." >&2
    echo "Run on the host:  sudo chown -R $(id -u):$(id -g) ./data" >&2
    exit 1
fi
exec "$@"
