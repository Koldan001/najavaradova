#!/usr/bin/env bash
# Uploads data/outage_notifier.db to Cloudflare R2, timestamped.
# Run by hand to test, or via the outage-backup.timer systemd unit (daily).
#
# Requires deploy/backup.env to exist (copy deploy/backup.env.example and
# fill in real R2 credentials) and the `aws` CLI installed (bootstrap.sh
# already does this).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/deploy/backup.env"
DB_FILE="$REPO_DIR/data/outage_notifier.db"

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE — copy deploy/backup.env.example and fill in R2 credentials." >&2
  exit 1
fi
if [ ! -f "$DB_FILE" ]; then
  echo "Missing $DB_FILE — nothing to back up." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

# Tolerate R2_ACCOUNT_ID being pasted as the full endpoint URL instead of the bare id
# (a real misconfig that silently broke every nightly backup for days: the script built
# "https://https://<id>.r2.cloudflarestorage.com.r2.cloudflarestorage.com"). Strip any
# scheme, the .r2.cloudflarestorage.com suffix, and stray slashes so both forms work.
R2_ACCOUNT_ID="${R2_ACCOUNT_ID#http://}"
R2_ACCOUNT_ID="${R2_ACCOUNT_ID#https://}"
R2_ACCOUNT_ID="${R2_ACCOUNT_ID%%.r2.cloudflarestorage.com*}"
R2_ACCOUNT_ID="${R2_ACCOUNT_ID%%/*}"
if ! printf '%s' "$R2_ACCOUNT_ID" | grep -qE '^[0-9a-f]{32}$'; then
  echo "R2_ACCOUNT_ID doesn't look like a 32-hex Cloudflare account id: '$R2_ACCOUNT_ID'" >&2
  exit 1
fi

export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
# aws-cli v2 (>= 2.23) adds x-amz-checksum / integrity headers by default that Cloudflare R2
# does not accept - it answers "AccessDenied" on CreateMultipartUpload / PutObject even with a
# valid Read&Write token. These two switch that back off. Harmless on older aws-cli.
export AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED
export AWS_RESPONSE_CHECKSUM_VALIDATION=WHEN_REQUIRED
ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
KEY="backups/outage_notifier-${STAMP}.db"

# The app runs in WAL mode and writes continuously - a plain file copy can catch a torn
# write. `sqlite3 .backup` takes a consistent snapshot (folding in the -wal). Prefer the
# host's sqlite3; fall back to the one inside the api container (writing into the
# bind-mounted data/ dir, which is the same directory on the host); last resort, a plain
# copy with a warning.
SNAP="$REPO_DIR/data/_snapshot-${STAMP}.db"
trap 'rm -f "$SNAP" "${SNAP}-wal" "${SNAP}-shm"' EXIT
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB_FILE" ".backup '$SNAP'"
elif command -v docker >/dev/null 2>&1 && \
     docker compose exec -T api sqlite3 /app/data/outage_notifier.db ".backup '/app/data/$(basename "$SNAP")'" 2>/dev/null; then
  echo "(snapshot via api container - host has no sqlite3; 'sudo apt-get install -y sqlite3' to use the faster path)"
else
  echo "⚠️ no sqlite3 on host or in a running container - plain copy, small torn-write risk" >&2
  cp "$DB_FILE" "$SNAP"
fi

aws s3 cp "$SNAP" "s3://${R2_BUCKET}/${KEY}" \
  --endpoint-url "$ENDPOINT" \
  --region auto

# Keep only the 30 most recent snapshots so the bucket doesn't grow without bound.
aws s3 ls "s3://${R2_BUCKET}/backups/" --endpoint-url "$ENDPOINT" --region auto \
  | awk '{print $4}' | grep '^outage_notifier-' | sort | head -n -30 \
  | while read -r old; do
      aws s3 rm "s3://${R2_BUCKET}/backups/${old}" --endpoint-url "$ENDPOINT" --region auto
    done

echo "Backed up $DB_FILE -> s3://${R2_BUCKET}/${KEY}"

# Success marker for workers/watchdog.py - it alerts the operator if this file's mtime falls
# more than ~26h behind, i.e. a nightly run silently failed. Only touched on a clean upload.
touch "$REPO_DIR/data/.last_backup_ok"
