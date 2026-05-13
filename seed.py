"""
Seed CommuneUSA Supabase tables from the migrated JSON output files.

Required env vars:
    SUPABASE_URL      — e.g. https://xyzxyz.supabase.co
    SUPABASE_ANON_KEY — service-role key recommended for seeding

Run:
    pip install supabase
    SUPABASE_URL=... SUPABASE_ANON_KEY=... python seed.py
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

# Office categories that represent real officials in county_officials.json.
# Rows with other values are reference/note rows and should be skipped.
VALID_COUNTY_CATEGORIES = {"Council", "Executive", "Finance", "Judicial", "Law"}


# ── Helpers ──────────────────────────────────────────────────────────────────

def load(filename: str) -> list[dict]:
    path = OUTPUT_DIR / filename
    if not path.exists():
        sys.exit(f"ERROR: {path} not found — run migrate.py first")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def clean(val) -> Optional[str]:
    """Normalise a value: strip strings, coerce empty/whitespace to None."""
    if val is None:
        return None
    if isinstance(val, str):
        val = val.strip()
        return val if val else None
    return val


def existing_officials(supabase: Client, level: str) -> set[tuple]:
    """Return a set of (official_name, level, office_title) already in the DB."""
    res = (
        supabase.table("officials")
        .select("official_name,level,office_title")
        .eq("level", level)
        .execute()
    )
    return {(r["official_name"], r["level"], r["office_title"]) for r in res.data}


def batch_insert(supabase: Client, table: str, rows: list[dict], label: str) -> int:
    """Insert rows in chunks and return inserted count."""
    if not rows:
        return 0
    chunk_size = 100
    inserted = 0
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        supabase.table(table).insert(chunk).execute()
        inserted += len(chunk)
    return inserted


# ── Seed steps ────────────────────────────────────────────────────────────────

def seed_state(supabase: Client) -> str:
    """Upsert Washington state; return its UUID."""
    res = (
        supabase.table("states")
        .upsert({"name": "Washington", "abbreviation": "WA"}, on_conflict="abbreviation")
        .execute()
    )
    wa_id: str = res.data[0]["id"]
    print(f"  states       — Washington ({wa_id})")
    return wa_id


ALL_WA_COUNTIES = [
    "Adams", "Asotin", "Benton", "Chelan", "Clallam", "Clark", "Columbia",
    "Cowlitz", "Douglas", "Ferry", "Franklin", "Garfield", "Grant",
    "Grays Harbor", "Island", "Jefferson", "King", "Kitsap", "Kittitas",
    "Klickitat", "Lewis", "Lincoln", "Mason", "Okanogan", "Pacific",
    "Pend Oreille", "Pierce", "San Juan", "Skagit", "Skamania", "Snohomish",
    "Spokane", "Stevens", "Thurston", "Wahkiakum", "Walla Walla", "Whatcom",
    "Whitman", "Yakima",
]


def seed_counties(supabase: Client, wa_id: str) -> dict[str, str]:
    """
    Upsert all 39 WA counties. Existing rows are skipped via on_conflict.
    Returns a dict mapping county name → UUID.
    """
    rows = [{"state_id": wa_id, "name": name} for name in ALL_WA_COUNTIES]
    res = (
        supabase.table("counties")
        .upsert(rows, on_conflict="state_id,name")
        .execute()
    )
    county_map = {r["name"]: r["id"] for r in res.data}
    print(f"  counties     — {len(county_map)} upserted ({', '.join(sorted(county_map))})")
    return county_map


def seed_county_officials(
    supabase: Client, wa_id: str, county_map: dict[str, str]
) -> None:
    co_data = load("county_officials.json")
    seen = existing_officials(supabase, "county")
    to_insert: list[dict] = []
    skipped = 0

    for r in co_data:
        category = clean(r.get("office_category"))
        if category not in VALID_COUNTY_CATEGORIES:
            skipped += 1
            continue

        county_name = clean(r.get("county"))
        county_id = county_map.get(county_name or "")
        if not county_id:
            skipped += 1
            continue

        name = clean(r.get("official_name"))
        title = clean(r.get("office_title"))
        if not name or not title:
            skipped += 1
            continue

        if (name, "county", title) in seen:
            skipped += 1
            continue

        to_insert.append({
            "state_id": wa_id,
            "county_id": county_id,
            "level": "county",
            "office_title": title,
            "office_category": category,
            "official_name": name,
            "party": clean(r.get("party")),
            "term_start": clean(r.get("term_start")),
            "term_end": clean(r.get("term_end")),
            "phone": clean(r.get("phone_email")),
            "official_website": clean(r.get("official_website")),
            "appointed_or_elected": clean(r.get("appt_or_elected")),
            "notes": clean(r.get("notes")),
        })

    inserted = batch_insert(supabase, "officials", to_insert, "county officials")
    print(f"  officials    — county:   {inserted} inserted, {skipped} skipped")


def seed_state_legislators(supabase: Client, wa_id: str) -> None:
    sl_data = load("state_legislature.json")
    seen = existing_officials(supabase, "state")
    to_insert: list[dict] = []
    skipped = 0

    for r in sl_data:
        name = clean(r.get("senator_rep_name"))
        chamber = clean(r.get("chamber"))  # "Senate" or "House"
        if not name or not chamber:
            skipped += 1
            continue

        title = f"State {chamber}"
        if (name, "state", title) in seen:
            skipped += 1
            continue

        # Combine district number and area description into one readable string
        district_num = clean(r.get("district"))
        district_area = clean(r.get("district_area_cities"))
        if district_num and district_area:
            district = f"{district_num} — {district_area}"
        else:
            district = district_num or district_area

        to_insert.append({
            "state_id": wa_id,
            "level": "state",
            "office_title": title,
            "office_category": chamber,
            "official_name": name,
            "party": clean(r.get("party")),
            "district": district,
            "term_start": clean(r.get("term_start")),
            "term_end": clean(r.get("term_expires")),
            "phone": clean(r.get("phone")),
            "email": clean(r.get("email")),
            "official_website": clean(r.get("official_website")),
            "key_committees": clean(r.get("position")),
            "notes": clean(r.get("notes")),
        })

    inserted = batch_insert(supabase, "officials", to_insert, "state legislators")
    print(f"  officials    — state:    {inserted} inserted, {skipped} skipped")


def seed_federal_legislators(supabase: Client, wa_id: str) -> None:
    fl_data = load("federal_legislators.json")
    seen = existing_officials(supabase, "federal")
    to_insert: list[dict] = []
    skipped = 0

    for r in fl_data:
        name = clean(r.get("official_name"))
        chamber = clean(r.get("chamber"))  # "U.S. Senate" or "U.S. House"
        if not name or not chamber:
            skipped += 1
            continue

        if (name, "federal", chamber) in seen:
            skipped += 1
            continue

        to_insert.append({
            "state_id": wa_id,
            "level": "federal",
            "office_title": chamber,
            "office_category": chamber,
            "official_name": name,
            "party": clean(r.get("party")),
            "district": clean(r.get("district_position")),
            "term_start": clean(r.get("term_start")),
            "term_end": clean(r.get("term_end_next_election")),
            "phone": clean(r.get("phone")),
            "official_website": clean(r.get("official_website")),
            "ballotpedia_url": clean(r.get("ballotpedia")),
            "key_committees": clean(r.get("key_committees")),
            "notes": clean(r.get("notes")),
        })

    inserted = batch_insert(supabase, "officials", to_insert, "federal legislators")
    print(f"  officials    — federal:  {inserted} inserted, {skipped} skipped")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")

    supabase: Client = create_client(url, key)
    print("Connected to Supabase. Seeding...\n")

    wa_id = seed_state(supabase)
    if not wa_id or not isinstance(wa_id, str) or len(wa_id) != 36:
        sys.exit(f"ERROR: states upsert did not return a valid UUID (got {wa_id!r}). Aborting before any officials are inserted.")

    county_map = seed_counties(supabase, wa_id)
    if not county_map:
        sys.exit("ERROR: no counties were returned from upsert. Aborting before any officials are inserted.")

    seed_county_officials(supabase, wa_id, county_map)
    seed_state_legislators(supabase, wa_id)
    seed_federal_legislators(supabase, wa_id)

    print("\nDone.")


if __name__ == "__main__":
    main()
