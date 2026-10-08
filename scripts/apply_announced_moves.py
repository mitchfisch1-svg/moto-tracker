"""Apply announced rider moves (data/announced_moves_*.json) to rider_seasons.

Each move becomes the rider's row for that season with source 'news', the
least trusted source: the series' entry lists and results replace it as soon
as they exist (see src/rider_seasons.py). Every rider must already exist; a
name that does not match is an error, not a new rider, because a typo here
would otherwise invent one.

    python scripts/apply_announced_moves.py data/announced_moves_2027.json [--dry-run]
"""

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

import psycopg  # noqa: E402
from src.config import get_database_url  # noqa: E402
from src import rider_seasons  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    data = json.loads(pathlib.Path(args.path).read_text(encoding="utf-8"))
    season = data["season"]

    with psycopg.connect(get_database_url()) as conn:
        rows, missing = [], []
        for m in data["moves"]:
            hit = conn.execute("SELECT id FROM riders WHERE lower(full_name) = lower(%s)",
                               (m["rider"],)).fetchall()
            if len(hit) != 1:
                missing.append(f"{m['rider']} ({len(hit)} matches)")
                continue
            rows.append((hit[0][0], m))
        if missing:
            sys.exit("no single rider for: " + ", ".join(missing))
        written = 0
        for rider_id, m in rows:
            note = f"{m['announced']} {m['source']} — {m['note']}"
            written += rider_seasons.record(
                conn, season,
                [(rider_id, None, m["team"], m["manufacturer"], m["class"])],
                "news", note=note)
            print(f"  {m['rider']}: {season} {m['class']} {m['team']}")
        if args.dry_run:
            conn.rollback()
            print(f"dry run: {written} row(s) would change")
        else:
            conn.commit()
            print(f"{written} row(s) written or changed for {season}")


if __name__ == "__main__":
    main()
