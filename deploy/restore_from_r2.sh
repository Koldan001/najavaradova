#!/usr/bin/env bash
# Restore data/outage_notifier.db from an R2 backup snapshot.
#
#   bash deploy/restore_from_r2.sh              # restore the most recent snapshot
#   bash deploy/restore_from_r2.sh outage_notifier-20260903-030414.db   # a specific one
#   bash deploy/restore_from_r2.sh --list       # just list what's in the bucket
#
# Stops the containers first (so nothing is mid-write), moves the current DB aside to
# data/outage_notifier.db.pre-restore-<stamp>, drops the snapshot in, and brings the
# containers back. The set-aside copy is your undo.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/deploy/backup.env"
DB_FILE="$REPO_DIR/data/outage_notifier.db"
[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

R2_ACCOUNT_ID="${R2_ACCOUNT_ID#http://}"; R2_ACCOUNT_ID="${R2_ACCOUNT_ID#https://}"
R2_ACCOUNT_ID="${R2_ACCOUNT_ID%%.r2.cloudflarestorage.com*}"; R2_ACCOUNT_ID="${R2_ACCOUNT_ID%%/*}"
export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
# See backup_to_r2.sh - aws-cli v2 checksum headers R2 rejects.
export AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED
export AWS_RESPONSE_CHECKSUM_VALIDATION=WHEN_REQUIRED
ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
r2() { aws s3 "$@" --endpoint-url "$ENDPOINT" --region auto; }

cd "$REPO_DIR"

if [ "${1:-}" = "--list" ]; then
  r2 ls "s3://${R2_BUCKET}/backups/" | awk '{print $4"  ("$3" bytes)"}' | grep outage_notifier- | sort
  exit 0
fi

NAME="${1:-}"
if [ -z "$NAME" ]; then
  NAME="$(r2 ls "s3://${R2_BUCKET}/backups/" | awk '{print $4}' | grep '^outage_notifier-' | sort | tail -1)"
  [ -n "$NAME" ] || { echo "No snapshots found in s3://${R2_BUCKET}/backups/" >&2; exit 1; }
fi
echo "Restoring: $NAME"

TMP="$(mktemp --suffix=.db)"
trap 'rm -f "$TMP"' EXIT
r2 cp "s3://${R2_BUCKET}/backups/${NAME}" "$TMP"
# Fail before touching anything live if the download isn't a valid SQLite file. Use the
# host's sqlite3 if present, else the api container's; if neither, warn and proceed.
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$TMP" "PRAGMA integrity_check;" | grep -q '^ok$' || { echo "Downloaded file failed integrity_check" >&2; exit 1; }
elif command -v docker >/dev/null 2>&1 && docker compose ps -q api 2>/dev/null | grep -q .; then
  cp "$TMP" "$REPO_DIR/data/_restore-check.db"; trap 'rm -f "$TMP" "$REPO_DIR/data/_restore-check.db"' EXIT
  docker compose exec -T api sqlite3 /app/data/_restore-check.db "PRAGMA integrity_check;" | grep -q '^ok$' \
    || { echo "Downloaded file failed integrity_check" >&2; exit 1; }
else
  echo "⚠️ no sqlite3 available to verify the snapshot - proceeding unchecked" >&2
fi

if command -v docker >/dev/null && docker compose ps -q 2>/dev/null | grep -q .; then
  echo "==> stopping containers"
  docker compose down
  RESTART=1
fi

if [ -f "$DB_FILE" ]; then
  ASIDE="${DB_FILE}.pre-restore-$(date -u +%Y%m%d-%H%M%S)"
  mv "$DB_FILE" "$ASIDE"
  rm -f "${DB_FILE}-wal" "${DB_FILE}-shm"
  echo "==> current DB moved to $ASIDE"
fi
cp "$TMP" "$DB_FILE"
echo "==> restored $NAME -> $DB_FILE"

if [ "${RESTART:-0}" = 1 ]; then
  echo "==> starting containers"
  docker compose up -d
fi
echo "Done."
