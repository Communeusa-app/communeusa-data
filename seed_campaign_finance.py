"""
Seed campaign_finance table from output/campaign_finance.json.

Resolves official_name → official_id by querying the officials table.
Records whose name cannot be matched are logged and skipped.

Required env vars (loaded from .env):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY

Run:
    python3 seed_campaign_finance.py
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


def load_records() -> list[dict]:
    path = OUTPUT_DIR / "campaign_finance.json"
    if not path.exists():
        sys.exit("ERROR: output/campaign_finance.json not found — run migrate.py first")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_name_lookup(supabase: Client) -> dict[str, Optional[str]]:
    """Return lowercase official_name → official_id for all active officials.
    Names that map to more than one distinct official are set to None.
    """
    res = (
        supabase.table("officials")
        .select("id,official_name,level,office_title")
        .eq("is_active", True)
        .execute()
    )
    candidates: dict[str, list[tuple[str, str, str]]] = {}
    for row in res.data or []:
        key = (row["official_name"] or "").strip().lower()
        if not key:
            continue
        entry = (row["id"], row.get("level", ""), row.get("office_title") or "")
        candidates.setdefault(key, []).append(entry)

    lookup: dict[str, Optional[str]] = {}
    for key, entries in candidates.items():
        if len(entries) == 1:
            lookup[key] = entries[0][0]
        else:
            unique_profiles = {(lvl, title) for _, lvl, title in entries}
            if len(unique_profiles) == 1:
                lookup[key] = entries[0][0]
            else:
                lookup[key] = None  # genuinely ambiguous
    return lookup


def existing_keys(supabase: Client) -> set[tuple]:
    """Return (official_id, donor_name, amount, donation_date) tuples already in the DB."""
    page, size = 0, 1000
    keys: set[tuple] = set()
    while True:
        res = (
            supabase.table("campaign_finance")
            .select("official_id,donor_name,amount,donation_date")
            .range(page * size, (page + 1) * size - 1)
            .execute()
        )
        for r in res.data or []:
            keys.add((r["official_id"], r["donor_name"], r["amount"], r["donation_date"]))
        if len(res.data or []) < size:
            break
        page += 1
    return keys


def seed(supabase: Client) -> None:
    records = load_records()
    name_lookup = build_name_lookup(supabase)
    seen = existing_keys(supabase)

    to_insert: list[dict] = []
    unmatched = 0
    ambiguous = 0
    duplicates = 0

    for rec in records:
        official_name = clean(rec.get("official_name"))
        if not official_name:
            unmatched += 1
            continue

        key = official_name.lower()
        if key not in name_lookup:
            print(f"  WARN  no official found for: {official_name!r} — skipping")
            unmatched += 1
            continue

        official_id = name_lookup[key]
        if official_id is None:
            print(f"  WARN  ambiguous name: {official_name!r} — skipping")
            ambiguous += 1
            continue

        donor_name   = clean(rec.get("donor_name"))
        amount       = rec.get("amount")
        donation_date = clean(rec.get("donation_date"))

        if (official_id, donor_name, amount, donation_date) in seen:
            duplicates += 1
            continue

        to_insert.append({
            "official_id":     official_id,
            "donor_name":      donor_name,
            "donor_type":      clean(rec.get("donor_type")),
            "amount":          amount,
            "election_cycle":  clean(rec.get("election_cycle")),
            "donation_date":   donation_date,
            "industry_sector": clean(rec.get("industry_sector")),
            "source_url":      clean(rec.get("source_url")),
            "filing_source":   clean(rec.get("filing_source")),
        })

    inserted = 0
    chunk_size = 100
    for i in range(0, len(to_insert), chunk_size):
        supabase.table("campaign_finance").insert(to_insert[i : i + chunk_size]).execute()
        inserted += len(to_insert[i : i + chunk_size])

    print(
        f"  campaign_finance — {inserted} inserted, "
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
    seed(supabase)
    print("\nDone.")


if __name__ == "__main__":
    main()
