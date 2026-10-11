#!/usr/bin/env bash
# Weekly ContextShrink Postgres backup: dump, check it restores, upload to S3
# as Glacier Flexible Retrieval (storage class GLACIER), keep a few locally.
#
# Runs on the Pi 5 from cron (see deploy/backup/README.md). Needs:
#   ~/.config/contextshrink-backup/backup.env  (mode 600)
#       AWS_ACCESS_KEY_ID=...        an upload-only IAM key (README has the policy)
#       AWS_SECRET_ACCESS_KEY=...
#       AWS_DEFAULT_REGION=...       the bucket's region
#       # optional: BACKUP_BUCKET, BACKUP_PREFIX, KEEP_LOCAL
#   ~/horizon/.env for POSTGRES_USER / POSTGRES_DB (no password is needed:
#   pg_dump runs inside the Postgres container over its local socket).
#
# Usage: pg-weekly-backup.sh [--dry-run]   (--dry-run: dump and check, no upload)
set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

CONFIG="${BACKUP_CONFIG:-$HOME/.config/contextshrink-backup/backup.env}"
STACK_ENV="${STACK_ENV:-$HOME/horizon/.env}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/contextshrink}"
PG_CONTAINER="${PG_CONTAINER:-contextshrink-postgres}"
AWS_CLI_IMAGE="amazon/aws-cli:2.17.20"

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }
fail() { log "FAILED: $*"; exit 1; }

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
exec 9>"$BACKUP_DIR/.lock"
flock -n 9 || fail "another backup is already running"

stack_value() { grep -E "^$1=" "$STACK_ENV" 2>/dev/null | tail -1 | cut -d= -f2-; }
PG_USER="$(stack_value POSTGRES_USER)"; PG_USER="${PG_USER:-contextshrink}"
PG_DB="$(stack_value POSTGRES_DB)"; PG_DB="${PG_DB:-contextshrink}"

if [ "$DRY_RUN" -eq 0 ]; then
    [ -f "$CONFIG" ] || fail "missing $CONFIG (see deploy/backup/README.md)"
    [ "$(stat -c %a "$CONFIG")" = "600" ] || fail "$CONFIG must be mode 600 (chmod 600 it)"
    # shellcheck disable=SC1090
    set -a; . "$CONFIG"; set +a
    : "${AWS_ACCESS_KEY_ID:?set in $CONFIG}" "${AWS_SECRET_ACCESS_KEY:?set in $CONFIG}" \
      "${AWS_DEFAULT_REGION:?set in $CONFIG}"
fi
BUCKET="${BACKUP_BUCKET:-contextshrink-weekly-backup}"
PREFIX="${BACKUP_PREFIX:-postgres}"
KEEP_LOCAL="${KEEP_LOCAL:-4}"

STAMP="$(date -u '+%Y-%m-%dT%H%M%SZ')"
NAME="contextshrink-$STAMP.dump"
FILE="$BACKUP_DIR/$NAME"

log "dumping $PG_DB from $PG_CONTAINER"
umask 077
docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" --format=custom --no-owner \
    > "$FILE.partial" || { rm -f "$FILE.partial"; fail "pg_dump"; }
mv "$FILE.partial" "$FILE"

# A dump that pg_restore cannot list is useless; so is one without the
# account tables. Check both before it replaces anything.
TOC="$(docker exec -i "$PG_CONTAINER" pg_restore --list < "$FILE")" || fail "pg_restore cannot read $NAME"
for table in users api_keys subscriptions; do
    grep -q "TABLE DATA core $table " <<<"$TOC" || fail "$NAME has no data for core.$table"
done
SIZE="$(stat -c %s "$FILE")"
SHA="$(sha256sum "$FILE" | cut -d' ' -f1)"
log "dump ok: $NAME ($SIZE bytes, sha256 $SHA)"

if [ "$DRY_RUN" -eq 1 ]; then
    log "dry run: not uploading"
else
    KEY="$PREFIX/$(date -u +%Y)/$NAME"
    log "uploading to s3://$BUCKET/$KEY (GLACIER)"
    docker run --rm \
        -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION \
        -v "$BACKUP_DIR:/backup:ro" \
        "$AWS_CLI_IMAGE" s3 cp "/backup/$NAME" "s3://$BUCKET/$KEY" \
        --storage-class GLACIER --sse AES256 --only-show-errors \
        --metadata "sha256=$SHA" \
        || fail "upload to s3://$BUCKET/$KEY"
    date -u '+%Y-%m-%dT%H:%M:%SZ' > "$BACKUP_DIR/last_success"
    log "uploaded s3://$BUCKET/$KEY"
fi

# Keep the newest KEEP_LOCAL dumps here for quick restores; S3 keeps the rest.
mapfile -t old < <(ls -1t "$BACKUP_DIR"/contextshrink-*.dump 2>/dev/null | tail -n +"$((KEEP_LOCAL + 1))")
if [ "${#old[@]}" -gt 0 ]; then
    rm -f -- "${old[@]}"
    log "removed ${#old[@]} older local dump(s)"
fi
log "done"
