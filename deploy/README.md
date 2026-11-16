# Deploy — Oracle Cloud Free Tier VM

Goal: one always-on VM, outside your PC, running the scraper/bot/worker code
independently, plus a nightly off-VM backup of the database that actually
matters (`data/outage_notifier.db`).

This doc is the manual, browser-side part (VM creation) plus the scripts in
this folder for everything after you have SSH access.

## 1. Create the Oracle account + VM (manual, ~15-20 min)

1. Go to https://signup.oraclecloud.com and sign up for the **Always Free**
   tier. Requires a credit card for identity verification — you will not be
   charged as long as you stay on Always Free resources (the ones flagged
   "Always Free" in the console).
2. If your chosen region is out of free-tier capacity during signup, either
   pick a different region or retry in a day or two — a known, temporary
   Oracle quirk, not something wrong on your end.
3. Once in the console: **Compute → Instances → Create Instance**.
   - Image: **Ubuntu 24.04** (or latest LTS available under Always Free).
   - Shape: pick an **Always Free** shape — either the Ampere (ARM) shape
     with 1-4 OCPU / 6-24 GB RAM, or the AMD Micro shape (1 OCPU / 1 GB RAM).
     The ARM shape is more headroom for the same $0; take it if offered.
   - Add your SSH public key (generate one locally first if you don't have
     one: `ssh-keygen -t ed25519`) — this is how you'll log in, no password.
   - Leave networking on the default VCN; it creates one for you.
4. Once running, note the **public IP** shown on the instance page.
5. Open the firewall for it: **Networking → Virtual Cloud Networks → (your
   VCN) → Security Lists → default → Ingress Rules → Add**. You'll want at
   least:
   - `22/tcp` (SSH — likely already open by default)
   - `443/tcp` and `80/tcp` (for the registration API / bot webhook, once
     those exist)
6. SSH in: `ssh ubuntu@<public-ip>` (Ubuntu images use the `ubuntu` user).

## 2. Bootstrap the VM

Copy `deploy/bootstrap.sh` to the VM and run it, or just run it straight from
a fresh clone:

```bash
ssh ubuntu@<public-ip>
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/Koldan001/outage_notifier.git
cd outage_notifier
bash deploy/bootstrap.sh
```

This installs system packages, creates a venv, installs `requirements.txt`,
and reminds you to fill in `.env` (copy `.env.example`, fill in the real
`GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, etc. — the VM needs its own `.env`,
it does not inherit anything from your PC).

## 3. Backup — nightly, off-VM

The VM itself is not a backup. If it dies, gets misconfigured, or Oracle has
an incident, you still need a copy of `data/outage_notifier.db` somewhere
else. This repo backs up to **Cloudflare R2** (same account you're already
using for the Worker/Pages site, 10 GB free, S3-compatible).

### One-time setup

1. Cloudflare dashboard → **R2 → Create bucket** → name it e.g.
   `outage-notifier-backups`.
2. **R2 → Manage API Tokens → Create API Token** — permissions: Object
   Read & Write, scoped to that bucket. Note the **Access Key ID**, **Secret
   Access Key**, and your **Account ID** (shown on the R2 overview page).
3. On the VM:
   ```bash
   sudo apt-get install -y awscli
   cp deploy/backup.env.example deploy/backup.env
   nano deploy/backup.env   # fill in the four values
   chmod +x deploy/backup_to_r2.sh
   ```
   **`R2_ACCOUNT_ID` is the bare 32-hex id, not the endpoint URL.** Pasting
   `https://<id>.r2.cloudflarestorage.com` there produces a doubled hostname
   and every upload fails silently in the nightly timer's logs. The script now
   strips a scheme/suffix defensively and rejects anything that isn't 32 hex.
4. Test it once by hand: `bash deploy/backup_to_r2.sh` — should print an
   upload confirmation. Check the bucket in the Cloudflare dashboard to
   confirm the file landed. Also check `journalctl -u outage-backup.service -n 20`
   after the first scheduled run — a timer that "runs" isn't proof it succeeded.

### Automate it — systemd timer (daily)

```bash
sudo cp deploy/systemd/outage-backup.service /etc/systemd/system/
sudo cp deploy/systemd/outage-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now outage-backup.timer
systemctl list-timers outage-backup.timer   # confirm it's scheduled
```

Runs once a day. `backup_to_r2.sh` takes a `sqlite3 .backup` snapshot (safe
against the app's live WAL writes) and keeps the 30 most recent in the bucket.

### Restore

```bash
bash deploy/restore_from_r2.sh --list          # see available snapshots
bash deploy/restore_from_r2.sh                 # restore the most recent
bash deploy/restore_from_r2.sh outage_notifier-20260903-030414.db
```

Stops the containers, moves the current DB to
`data/outage_notifier.db.pre-restore-<stamp>` (your undo), drops the verified
snapshot in, restarts. **Do one restore drill now, not during an incident.**

### Derived data files (`rgz.db`, `serbia_streets.db`)

Regenerable *in theory* — but rebuilding `rgz.db` needs every CSV in
`data/rgz_raw/`, which otherwise lives on one laptop, and `serbia_streets.db`
needs the OSM `.pbf`. Treat R2 as the real source of truth:

```bash
bash deploy/backup_assets_to_r2.sh   # uploads rgz.db, serbia_streets.db, rgz_raw.tgz
```

Run it by hand whenever those change (new RGZ opštine, OSM rebuild) — not on a
timer. `deploy/update.sh` pulls `rgz.db` / `serbia_streets.db` from
`s3://<bucket>/assets/` automatically when they're missing on the VM, so a
fresh VM no longer needs a manual `scp`.

**Rebuilding `serbia_streets.db` from scratch** (only if R2 ever loses it — the
intermediates were deleted in the 2026-09-06 dead-code sweep since they're
~739 MB of regenerable input):

1. Serbia extract from Geofabrik —
   <https://download.geofabrik.de/europe/serbia-latest.osm.pbf>. The copy this
   DB was built from was dated 2026-08-11 (`data/serbia-260811.osm.pbf`).
2. Filter to ways tagged `highway` and export line geometries as GeoJSON
   Sequence to `data/serbia_roads.geojsonseq` — the osmium step
   `scripts/build_serbia_index.py` refers to as "Step 2". The exact original
   invocation wasn't recorded; the standard chain is
   `osmium tags-filter <in>.osm.pbf w/highway -o roads.osm.pbf` followed by
   `osmium export roads.osm.pbf -f geojsonseq -o data/serbia_roads.geojsonseq`.
3. `python scripts/build_serbia_index.py` — indexes that into
   `data/serbia_streets.db`.

## 4. HTTPS to the origin (Cloudflare Origin Certificate)

Cloudflare's "Flexible" SSL/TLS mode gives visitors real HTTPS to Cloudflare's
edge, but the Cloudflare→VM hop behind it stays plain HTTP — addresses and
emails from the registration form cross that hop unencrypted. This closes
that gap using a **Cloudflare Origin Certificate**: not publicly trusted (a
browser hitting the VM's IP directly would reject it), but Cloudflare trusts
it specifically, which is what makes "Full (strict)" mode possible instead of
just "Full" (accepts *any* cert, including self-signed, without verifying it).

1. Cloudflare dashboard → **SSL/TLS → Origin Server → Create Certificate**.
   Defaults are fine (RSA, 15-year expiry, covers the zone + subdomains).
   Save the **Origin Certificate** and **Private Key** it shows you — that
   private key is shown *once*, there's no re-download later.
2. On the VM, **not through chat** (a private key sitting in a chat log is a
   real key-management mistake, however low the practical risk here):
   ```bash
   mkdir -p ~/outage_notifier/certs
   nano ~/outage_notifier/certs/origin_cert.pem   # paste the Origin Certificate, save
   nano ~/outage_notifier/certs/origin_key.pem    # paste the Private Key, save
   chmod 600 ~/outage_notifier/certs/*.pem
   ```
3. Oracle Security List: confirm `443/tcp` ingress is open (see step 1.5 —
   `80/tcp` was opened for the earlier Flexible setup; this needs `443/tcp`
   too, same place).
4. `git pull && docker compose up -d caddy` — brings up the new `caddy`
   service (see `Caddyfile`), terminating real TLS on port 443 and proxying
   to `api` internally.
5. Verify before touching Cloudflare's mode: `curl -vk https://<public-ip>/api/cities`
   from your own machine — `-k` skips cert trust-checking since you're
   hitting the IP directly, not the hostname the cert is issued for. A 200
   with real JSON confirms Caddy + the cert are working before Cloudflare's
   involved at all.
6. Cloudflare dashboard → **SSL/TLS → Overview** → switch mode from
   *Flexible* to **Full (strict)**. This is zone-wide — spot-check the main
   Pages site still loads afterward, same as when Flexible was first set.
7. **Close port 80 once Full (strict) is confirmed working.** `docker-compose.yml`'s
   `api` service no longer publishes it (removed - `api` is reachable only
   from `caddy`, over the internal docker network, never the internet).
   Remove the now-dead `80/tcp` ingress rule from the Oracle Security List
   too (step 1.5) - a rule with nothing listening behind it is still an open
   door for scanner traffic to knock on, confirmed for real: that exact
   exposure is what wedged `api`'s server before the threading fix (see
   `scripts/local_web_server.py`'s `ThreadingHTTPServer` comment). Cloudflare
   only ever needs 443 on the origin once Full (strict) is set.

## What's still missing

See `PLAN.md` at the repo root — the living, up-to-date launch checklist.
This file only covers VM provisioning, backups, and HTTPS; it deliberately
doesn't duplicate the rest.
