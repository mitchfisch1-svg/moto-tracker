"""Match riders to official headshots and store riders.headshot_url.

The SMX rider headshots live in a public GCS bucket named
feld-smx-rider-headshots, keyed by lowercase 'firstname-lastname.png' (some
riders have .jpg instead/as well). We list the bucket once and match each rider
by that slug, preferring .png.

Run from the project root (idempotent, safe to re-run any time):
    python scripts/sync_headshots.py
"""

import pathlib
import re
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db import get_connection  # noqa: E402
from src.names import slug_variants  # noqa: E402

BUCKET_API = "https://storage.googleapis.com/storage/v1/b/feld-smx-rider-headshots/o"
PUBLIC_BASE = "https://storage.googleapis.com/feld-smx-rider-headshots/"
HEADERS = {"User-Agent": "MotoTracker/0.1 (personal project)"}


def list_bucket() -> set[str]:
    names, token = set(), None
    while True:
        params = {"maxResults": 1000}
        if token:
            params["pageToken"] = token
        resp = requests.get(BUCKET_API, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        names.update(item["name"] for item in data.get("items", []))
        token = data.get("nextPageToken")
        if not token:
            return names


def main() -> None:
    objects = list_bucket()
    print(f"Bucket lists {len(objects)} objects.")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, full_name FROM riders")
            riders = cur.fetchall()

            matched = 0
            for rid, full_name in riders:
                url = None
                # Try each spelling of the name, not just ours: results give us
                # "R J Hampshire" while the bucket keys him "rj-hampshire".
                for slug in slug_variants(full_name):
                    for ext in ("png", "jpg"):
                        if f"{slug}.{ext}" in objects:
                            url = f"{PUBLIC_BASE}{slug}.{ext}"
                            break
                    if url:
                        break
                cur.execute(
                    "UPDATE riders SET headshot_url = %s WHERE id = %s", (url, rid)
                )
                if url:
                    matched += 1

    print(f"Matched headshots for {matched} of {len(riders)} riders.")


if __name__ == "__main__":
    main()
