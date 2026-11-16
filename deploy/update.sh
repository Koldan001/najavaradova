#!/usr/bin/env bash
# Pull the latest code and restart the stack on the VM.
#
# This is what the GitHub Actions deploy job runs (see .github/workflows/ci.yml),
# and it's also safe to run by hand on the VM if CI is down or you want to deploy
# a fix immediately:
#
#   ssh ubuntu@<vm-ip>
#   cd ~/outage_notifier && bash deploy/update.sh
#
# It does exactly the manual steps deploy/README.md used to describe: fetch,
# fast-forward main, rebuild the images, run the (idempotent) schema migrations,
# restart. Nothing here is destructive to the database - data/ is a bind-mounted
# volume, untouched by rebuilds.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "==> Was at commit: $(git rev-parse --short HEAD) ($(git log -1 --format=%s))"

echo "==> Fetching latest code from origin"
git fetch --prune origin
git checkout main
# Hard-reset to origin/main. The VM is a deploy target, not a workstation - it
# should mirror origin/main exactly. Any local edit made directly on the server
# (e.g. a stray `chmod +x`) is drift, and `git merge --ff-only` would abort the
# whole deploy rather than step over it (learned the hard way). `reset --hard`
# throws that drift away and always lands on the pushed commit. Real changes
# always go through git + a push, never a hand-edit on the box.
git reset --hard origin/main

echo "==> Now at commit: $(git rev-parse --short HEAD) ($(git log -1 --format=%s))"

# Derived data files (rgz.db = RGZ address registry, serbia_streets.db = OSM fallback) are
# gitignored and not in the image. On a fresh VM they'd be missing and the app would run with
# no address coverage at all. Pull them from R2 (uploaded by deploy/backup_assets_to_r2.sh)
# only when absent or empty - a present file is left alone, so a deploy never re-downloads
# 250 MB. Best-effort: if backup.env or the asset isn't there, log and carry on.
if [ -f deploy/backup.env ]; then
  # shellcheck disable=SC1091
  source deploy/backup.env
  _acct="${R2_ACCOUNT_ID#http://}"; _acct="${_acct#https://}"
  _acct="${_acct%%.r2.cloudflarestorage.com*}"; _acct="${_acct%%/*}"
  for f in rgz.db serbia_streets.db; do
    if [ ! -s "data/$f" ]; then
      echo "==> data/$f missing - fetching from R2"
      AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
      AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED AWS_RESPONSE_CHECKSUM_VALIDATION=WHEN_REQUIRED \
        aws s3 cp "s3://${R2_BUCKET}/assets/$f" "data/$f" \
        --endpoint-url "https://${_acct}.r2.cloudflarestorage.com" --region auto \
        || echo "⚠️ could not fetch data/$f from R2 - address coverage will be limited until it's present"
    fi
  done
fi

echo "==> Rebuilding images and restarting containers"
# --build: rebuild any image whose source changed. Compose only restarts the
# containers whose image or config actually changed, so untouched services keep
# running without a blip.
docker compose up -d --build

echo "==> Applying database schema migrations"
# init_db() is fully idempotent: every table is CREATE TABLE IF NOT EXISTS, every
# new column is an additive ALTER wrapped in try/except, every index is
# CREATE INDEX IF NOT EXISTS. Running it on an already-current DB is a no-op.
# Running it here every deploy is what stops the "new column migration never ran,
# container crash-loops" class of bug (see PLAN.md, the unsub_token incident).
docker compose run --rm api python -m core.database

echo "==> Cleaning up dangling image layers from the rebuild"
docker image prune -f

echo "==> Container status:"
docker compose ps

echo "==> Deploy complete: now running $(git rev-parse --short HEAD)"
