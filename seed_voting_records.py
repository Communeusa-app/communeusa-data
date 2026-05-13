"""
Seed voting_records table from output/voting_records.json.

Resolves official_name → official_id by querying the officials table.
Records whose name cannot be matched are logged and skipped.

Required env vars (loaded from .env):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY

Run:
    python3 seed_voting_records.py
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv(Path(__file__).parent / ".env")

OUTPUT_DIR = Path(__file__).parent / "output"


def clean(val) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        val = val.strip()
        return val if val else None
    return val


def load_voting_records() -> list[dict]:
    path = OUTPUT_DIR / "voting_records.json"
    if not path.exists():
        sys.exit("ERROR: output/voting_records.json not found — run migrate.py first")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_name_lookup(supabase: Client) -> dict[str, Optional[str]]:
    """Fetch all officials and return lowercase name → official_id.
    Names that appear more than once are mapped to None to flag ambiguity.
    """
    res = supabase.table("officials").select("id,official_name").execute()
    lookup: dict[str, Optional[str]] = {}
    for row in res.data:
        key = (row["official_name"] or "").strip().lower()
        if not key:
            continue
        if key in lookup:
            lookup[key] = None  # mark ambiguous
        else:
            lookup[key] = row["id"]
    return lookup


def existing_keys(supabase: Client) -> set[tuple]:
    """Return (official_id, bill_name, vote_date) tuples already in the DB."""
    res = (
        supabase.table("voting_records")
        .select("official_id,bill_name,vote_date")
        .execute()
    )
    return {(r["official_id"], r["bill_name"], r["vote_date"]) for r in res.data}


def seed_voting_records(supabase: Client) -> None:
    records = load_voting_records()
    name_lookup = build_name_lookup(supabase)
    seen = existing_keys(supabase)

    to_insert: list[dict] = []
    unmatched = 0
    ambiguous = 0
    duplicates = 0

    for rec in records:
        official_name = clean(rec.get("official_name"))
        if not official_name:
            print(f"  WARN  missing official_name on record: {rec.get('bill_name')!r} — skipping")
            unmatched += 1
            continue

        key = official_name.lower()

        if key not in name_lookup:
            print(f"  WARN  no official found for: {official_name!r} — skipping")
            unmatched += 1
            continue

        official_id = name_lookup[key]
        if official_id is None:
            print(f"  WARN  ambiguous name (multiple officials match): {official_name!r} — skipping")
            ambiguous += 1
            continue

        bill_name = clean(rec.get("bill_name"))
        vote_date = clean(rec.get("vote_date"))

        if (official_id, bill_name, vote_date) in seen:
            duplicates += 1
            continue

        to_insert.append({
            "official_id":        official_id,
            "bill_name":          bill_name,
            "bill_description":   clean(rec.get("bill_description")),
            "topic_category":     clean(rec.get("topic_category")),
            "vote_date":          vote_date,
            "vote_cast":          clean(rec.get("vote_cast")),
            "result":             clean(rec.get("result")),
            "constituent_impact": clean(rec.get("constituent_impact")),
            "source_url":         clean(rec.get("source_url")),
        })

    inserted = 0
    chunk_size = 100
    for i in range(0, len(to_insert), chunk_size):
        supabase.table("voting_records").insert(to_insert[i : i + chunk_size]).execute()
        inserted += len(to_insert[i : i + chunk_size])

    print(
        f"  voting_records — {inserted} inserted, "
        f"{duplicates} duplicate, "
        f"{unmatched} unmatched, "
        f"{ambiguous} ambiguous"
    )


def main() -> None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")

    supabase: Client = create_client(url, key)
    print("Connected to Supabase.\n")
    seed_voting_records(supabase)
    print("\nDone.")


if __name__ == "__main__":
    main()
