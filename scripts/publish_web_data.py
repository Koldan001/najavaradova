"""Build the outage feed and push it to the live site's data API (the Cloudflare
Worker in cf/worker.js).

    python scripts/publish_web_data.py               # build + upload
    python scripts/publish_web_data.py --today=2026-08-28   # pin "day zero"
    python scripts/publish_web_data.py --dry-run     # build + print, no upload

Reads two values from the environment (put them in .env):

    WEB_DATA_API      base URL of the Worker, e.g.
                      https://najavaradova-api.<subdomain>.workers.dev
    WEB_DATA_TOKEN    the PUBLISH_TOKEN secret set on that Worker

The build itself is scripts/build_web_data.py — this script only adds the upload.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.build_web_data import build  # noqa: E402

TIMEOUT = 20


def main() -> int:
    pinned = None
    dry_run = "--dry-run" in sys.argv
    for arg in sys.argv[1:]:
        if arg.startswith("--today="):
            pinned = datetime.strptime(arg.split("=", 1)[1], "%Y-%m-%d").date()

    data = build(pinned)
    # Compact separators: the feed is mostly coordinate arrays, where ", " and ": " alone were
    # ~10% of the bytes. Nothing reads this file by eye - use --dry-run for that.
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    count = data["meta"]["count"]
    label = f" (as of {pinned})" if pinned else ""
    print(f"Built {count} outages{label}, city={data['meta']['city']}")

    if dry_run:
        for o in data["outages"][:20]:
            print(f"  d={o['d']} {o['kind']:9} {o['time'] or '—':16} {o['street'][:44]}")
        return 0

    api = os.environ.get("WEB_DATA_API", "").rstrip("/")
    token = os.environ.get("WEB_DATA_TOKEN", "")
    if not api or not token:
        print("WEB_DATA_API and WEB_DATA_TOKEN must be set (see .env.example).", file=sys.stderr)
        return 1

    resp = requests.put(
        f"{api}/outages.json",
        data=body.encode("utf-8"),
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        print(f"Upload failed: HTTP {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return 1

    print(f"Uploaded to {api}/outages.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
