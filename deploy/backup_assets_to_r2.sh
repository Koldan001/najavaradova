#!/usr/bin/env bash
# One-shot upload of the big derived data files to R2. Run by hand whenever they change
# (new RGZ opštine imported, serbia_streets rebuilt) - NOT on a timer, they're near-static.
#
#   bash deploy/backup_assets_to_r2.sh
#
# These are "regenerable" only in theory: rebuilding rgz.db needs every CSV in
# data/rgz_raw/, which otherwise lives on one laptop. This makes R2 the real source of
# truth so a VM rebuild (or a lost laptop) isn't a data-loss event. deploy/update.sh pulls
# rgz.db / serbia_streets.db from here when they're missing on the VM.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/deploy/backup.env"
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

for f in data/rgz.db data/serbia_streets.db; do
  if [ -s "$f" ]; then
    echo "==> uploading $f ($(du -h "$f" | cut -f1))"
    r2 cp "$f" "s3://${R2_BUCKET}/assets/$(basename "$f")"
  else
    echo "==> skip $f (missing or empty)"
  fi
done

if [ -d data/rgz_raw ] && [ -n "$(ls -A data/rgz_raw 2>/dev/null)" ]; then
  TAR="$(mktemp --suffix=.tgz)"
  trap 'rm -f "$TAR"' EXIT
  echo "==> packing data/rgz_raw/ ($(du -sh data/rgz_raw | cut -f1))"
  tar czf "$TAR" -C data rgz_raw
  r2 cp "$TAR" "s3://${R2_BUCKET}/assets/rgz_raw.tgz"
fi

echo "Done. Assets in s3://${R2_BUCKET}/assets/"
