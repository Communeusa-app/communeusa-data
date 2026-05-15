"""
CommuneUSA Elections Seed

Reads output/elections.json and output/candidates.json (produced by migrate.py)
and inserts the records into Supabase. Skips elections that already exist
(matched by office_name + election_date + municipality_level_raw).
Links incumbent candidates to their official_id by name matching.

Run after migrate.py:
    python3 seed_elections.py
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("seed-elections")

OUTPUT_DIR = Path(__file__).parent / "output"


# ── Supabase helpers ───────────────────────────────────────────────────────────

def get_wa_state_id(supabase: Client) -> str:
    res = (
        supabase.table("states")
        .select("id")
        .eq("abbreviation", "WA")
        .single()
        .execute()
    )
    if not res.data:
        sys.exit("ERROR: Washington state row not found. Run seed.py first.")
    return res.data["id"]


def get_county_map(supabase: Client, wa_id: str) -> dict[str, str]:
    """Return {bare_county_name: id} for all WA counties."""
    res = (
        supabase.table("counties")
        .select("id,name")
        .eq("state_id", wa_id)
        .execute()
    )
    return {r["name"]: r["id"] for r in (res.data or [])}


def get_municipality_map(supabase: Client, wa_id: str) -> dict[str, str]:
    """Return {lowercase_city_name: id} for all WA municipalities."""
    res = (
        supabase.table("municipalities")
        .select("id,name")
        .eq("state_id", wa_id)
        .execute()
    )
    return {r["name"].lower(): r["id"] for r in (res.data or [])}


def build_official_lookup(supabase: Client) -> dict[str, Optional[str]]:
    """
    Return {lowercase_name: official_id} for all active officials.
    Returns None for ambiguous names (different profiles with same name).
    """
    res = (
        supabase.table("officials")
        .select("id,official_name,level,office_title")
        .eq("is_active", True)
        .execute()
    )
    candidates: dict[str, list[tuple]] = {}
    for row in res.data or []:
        key = (row["official_name"] or "").strip().lower()
        if not key:
            continue
        candidates.setdefault(key, []).append(
            (row["id"], row.get("level", ""), row.get("office_title") or "")
        )

    lookup: dict[str, Optional[str]] = {}
    for key, entries in candidates.items():
        if len(entries) == 1:
            lookup[key] = entries[0][0]
        else:
            unique_profiles = {(lvl, title) for _, lvl, title in entries}
            lookup[key] = entries[0][0] if len(unique_profiles) == 1 else None
    return lookup


def load_existing_elections(supabase: Client) -> set[tuple]:
    """Return (office_name, election_date, county_id, municipality_id) for existing rows.

    Including county_id and municipality_id prevents false-positive deduplication
    of same-named races in different jurisdictions (e.g. "City Council — Multiple
    Positions" for Renton vs Bellingham on the same date).
    """
    res = (
        supabase.table("elections")
        .select("office_name,election_date,county_id,municipality_id")
        .execute()
    )
    return {
        (
            r["office_name"],
            r.get("election_date"),
            r.get("county_id"),
            r.get("municipality_id"),
        )
        for r in (res.data or [])
    }


def load_existing_candidates(supabase: Client) -> set[tuple]:
    """Return (election_id, lowercase_name) for all existing candidates."""
    res = supabase.table("candidates").select("election_id,name").execute()
    return {
        (r["election_id"], (r["name"] or "").lower())
        for r in (res.data or [])
    }


# ── Location resolvers ─────────────────────────────────────────────────────────

def resolve_location(
    election: dict,
    county_map: dict[str, str],
    municipality_map: dict[str, str],
) -> tuple[Optional[str], Optional[str]]:
    """Return (county_id, municipality_id) based on level and municipality_level_raw."""
    level = election.get("level", "")
    raw = (election.get("municipality_level_raw") or "").strip()

    if level == "county":
        # "King County" → "King"
        bare = raw.replace(" County", "").strip()
        county_id = county_map.get(bare)
        if not county_id:
            log.warning("County not found in DB: %r (raw=%r)", bare, raw)
        return county_id, None

    if level == "city":
        municipality_id = municipality_map.get(raw.lower())
        if not municipality_id:
            log.warning("Municipality not found in DB: %r", raw)
        return None, municipality_id

    # state / federal — no county or municipality FK needed
    return None, None


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")

    elections_path  = OUTPUT_DIR / "elections.json"
    candidates_path = OUTPUT_DIR / "candidates.json"
    for p in (elections_path, candidates_path):
        if not p.exists():
            sys.exit(f"ERROR: {p} not found — run migrate.py first")

    elections_data  = json.loads(elections_path.read_text())
    candidates_data = json.loads(candidates_path.read_text())
    log.info("Loaded %d elections and %d candidates from JSON",
             len(elections_data), len(candidates_data))

    supabase: Client = create_client(supabase_url, supabase_key)
    wa_id            = get_wa_state_id(supabase)
    county_map       = get_county_map(supabase, wa_id)
    municipality_map = get_municipality_map(supabase, wa_id)
    official_lookup  = build_official_lookup(supabase)
    existing_elections = load_existing_elections(supabase)

    log.info("Connected to Supabase — %d counties, %d municipalities, %d officials loaded",
             len(county_map), len(municipality_map), len(official_lookup))

    # ── Insert elections ───────────────────────────────────────────────────────
    key_to_db_id: dict[str, str] = {}  # election._key → DB uuid
    e_inserted = e_skipped = 0

    for election in elections_data:
        election_key  = election["_key"]
        office_name   = election["office_name"]
        election_date = election.get("election_date")

        # Resolve location first — needed for the 4-part dedup key.
        county_id, municipality_id = resolve_location(election, county_map, municipality_map)

        # Skip if already in DB using (office_name, date, county_id, municipality_id).
        # A 2-field key (office_name, date) caused same-named races in different
        # jurisdictions (e.g. Renton/Bellingham "City Council — Multiple Positions"
        # on the same date) to be incorrectly deduplicated.
        skip_key = (office_name, election_date, county_id, municipality_id)
        if skip_key in existing_elections:
            log.info("SKIP  election already exists: %s (%s)", office_name, election_date)
            # Still need the DB id for candidate insertion.
            q = (
                supabase.table("elections")
                .select("id")
                .eq("office_name", office_name)
                .eq("election_date", election_date)
            )
            if county_id:
                q = q.eq("county_id", county_id)
            else:
                q = q.is_("county_id", "null")
            if municipality_id:
                q = q.eq("municipality_id", municipality_id)
            else:
                q = q.is_("municipality_id", "null")
            res = q.limit(1).execute()
            if res.data:
                key_to_db_id[election_key] = res.data[0]["id"]
            e_skipped += 1
            continue

        payload = {
            "state_id":        wa_id,
            "county_id":       county_id,
            "municipality_id": municipality_id,
            "office_name":     office_name,
            "level":           election["level"],
            "election_date":   election_date,
            "primary_date":    election.get("primary_date"),
            "filing_deadline": election.get("filing_deadline"),
            "description":     election.get("description"),
            "source_url":      election.get("source_url"),
        }

        res = supabase.table("elections").insert(payload).execute()
        if not res.data:
            log.error("Insert failed for election: %s", office_name)
            continue

        db_id = res.data[0]["id"]
        key_to_db_id[election_key] = db_id
        existing_elections.add(skip_key)
        log.info("INSERT election [%s] %s (%s)", election["level"], office_name, election_date)
        e_inserted += 1

    log.info("Elections: %d inserted, %d skipped (already existed)", e_inserted, e_skipped)

    # ── Insert candidates ──────────────────────────────────────────────────────
    existing_candidates = load_existing_candidates(supabase)
    c_inserted = c_skipped = c_linked = 0

    for candidate in candidates_data:
        election_key = candidate.get("election_key")
        election_id  = key_to_db_id.get(election_key)

        if not election_id:
            log.warning("No election_id for candidate %r (key=%r) — skipping",
                        candidate.get("name"), election_key)
            c_skipped += 1
            continue

        cand_key = (election_id, (candidate.get("name") or "").lower())
        if cand_key in existing_candidates:
            log.info("SKIP  candidate already exists: %s → %s", candidate["name"], election_key[:50])
            c_skipped += 1
            continue

        # Try to link incumbents to their official profile
        official_id: Optional[str] = None
        if candidate.get("is_incumbent"):
            name_key = (candidate.get("name") or "").lower()
            official_id = official_lookup.get(name_key)
            if official_id:
                c_linked += 1
                log.info("Linked incumbent %r → official_id %s", candidate["name"], official_id)
            else:
                log.debug("No official match for incumbent %r", candidate["name"])

        payload = {
            "election_id":    election_id,
            "official_id":    official_id,
            "name":           candidate["name"],
            "party":          candidate.get("party"),
            "is_incumbent":   candidate.get("is_incumbent", False),
            "website":        candidate.get("website"),
            "ballotpedia_url": candidate.get("ballotpedia_url"),
        }

        supabase.table("candidates").insert(payload).execute()
        existing_candidates.add(cand_key)
        log.info("INSERT candidate %s%s → %s",
                 candidate["name"],
                 " (inc)" if candidate.get("is_incumbent") else "",
                 election_key[:50])
        c_inserted += 1

    log.info(
        "Candidates: %d inserted (%d linked to officials), %d skipped",
        c_inserted, c_linked, c_skipped,
    )
    log.info("=== seed-elections complete ===")


if __name__ == "__main__":
    main()
