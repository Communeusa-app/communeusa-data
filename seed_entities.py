"""
CommuneUSA Entity Seed

Reads 7 entity JSON files from output/ and inserts them into Supabase.

  school_boards.json      → school_districts + school_board_members
  law_enforcement.json    → law_enforcement_agencies
  fire_ems.json           → fire_ems_agencies
  hospitals.json          → hospitals
  utilities_transit.json  → utilities_transit
  state_agencies.json     → state_agencies
  judiciary.json          → judiciary

Duplicate detection: name + state_id (school_board_members: district_id + name).
Skips records whose _needs_detail stub flag is set for board-member insertion.

Run:
    pip install supabase python-dotenv
    python3 seed_entities.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
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
log = logging.getLogger("seed-entities")

OUTPUT_DIR = Path(__file__).parent / "output"
BATCH_SIZE = 100


# ── Helpers ────────────────────────────────────────────────────────────────────

def parse_int(val) -> Optional[int]:
    """Extract the first integer from a value; return None if unparseable."""
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val) if val == int(val) else None
    s = str(val).replace(",", "").replace("~", "").strip()
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def bare_county(raw: str) -> str:
    """'King County' → 'King', 'King' → 'King'."""
    return re.sub(r"\s+[Cc]ounty$", "", (raw or "").strip()).strip()


def load_json(filename: str) -> list[dict]:
    path = OUTPUT_DIR / filename
    if not path.exists():
        log.warning("File not found: %s — skipping", path)
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def batch_insert(supabase: Client, table: str, rows: list[dict], dry_run: bool) -> int:
    if not rows:
        return 0
    if dry_run:
        log.info("  [dry-run] would insert %d rows into %s", len(rows), table)
        return 0
    inserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i : i + BATCH_SIZE]
        supabase.table(table).insert(chunk).execute()
        inserted += len(chunk)
    return inserted


# ── Supabase reference data ────────────────────────────────────────────────────

def get_wa_state_id(supabase: Client) -> str:
    res = supabase.table("states").select("id").eq("abbreviation", "WA").single().execute()
    if not res.data:
        sys.exit("ERROR: Washington state row not found — run seed.py first.")
    return res.data["id"]


def get_county_map(supabase: Client, wa_id: str) -> dict[str, str]:
    """Return {bare_county_name: county_id} for all WA counties."""
    res = supabase.table("counties").select("id,name").eq("state_id", wa_id).execute()
    return {r["name"]: r["id"] for r in (res.data or [])}


def resolve_county(raw: Optional[str], county_map: dict[str, str]) -> Optional[str]:
    if not raw:
        return None
    key = bare_county(raw)
    cid = county_map.get(key)
    if not cid and key:
        log.debug("County not in DB: %r", key)
    return cid


def load_existing_names(supabase: Client, table: str, wa_id: str) -> set[str]:
    """Return {lowercase name} for all rows in table belonging to WA."""
    res = supabase.table(table).select("name").eq("state_id", wa_id).execute()
    return {(r["name"] or "").lower() for r in (res.data or [])}


# ── 1. School districts & board members ───────────────────────────────────────

def seed_school_boards(supabase: Client, wa_id: str, county_map: dict[str, str],
                       dry_run: bool) -> None:
    records = load_json("school_boards.json")
    if not records:
        return

    # ── Pass 1: upsert school_districts ───────────────────────────────────────
    existing_districts = load_existing_names(supabase, "school_districts", wa_id)

    # Collect one canonical row per district (first occurrence wins)
    districts_seen: dict[str, dict] = {}
    for rec in records:
        dname = (rec.get("district_name") or "").strip()
        if not dname or dname.lower() in districts_seen:
            continue
        districts_seen[dname.lower()] = {
            "state_id":         wa_id,
            "county_id":        resolve_county(rec.get("county"), county_map),
            "name":             dname,
            "official_website": rec.get("website"),
            "phone":            rec.get("phone"),
        }

    new_districts = [
        v for k, v in districts_seen.items()
        if k not in existing_districts
    ]
    inserted_d = batch_insert(supabase, "school_districts", new_districts, dry_run)
    log.info("[school_districts] %d new / %d already existed / %d total districts",
             inserted_d, len(existing_districts), len(districts_seen))

    # ── Build district name → DB id lookup ───────────────────────────────────
    res = supabase.table("school_districts").select("id,name").eq("state_id", wa_id).execute()
    district_id_map: dict[str, str] = {
        (r["name"] or "").lower(): r["id"] for r in (res.data or [])
    }

    # ── Pass 2: insert school_board_members ───────────────────────────────────
    # Load existing members keyed by (district_id, lowercase name)
    existing_members_res = supabase.table("school_board_members").select("district_id,name").execute()
    existing_members: set[tuple[str, str]] = {
        (r["district_id"], (r["name"] or "").lower())
        for r in (existing_members_res.data or [])
    }

    new_members: list[dict] = []
    skipped_m = 0
    for rec in records:
        director = (rec.get("director_name") or "").strip()
        if not director:
            continue  # stub record with no member data

        dname = (rec.get("district_name") or "").strip().lower()
        district_id = district_id_map.get(dname)
        if not district_id:
            log.warning("[school_board_members] district not in DB: %r", dname)
            skipped_m += 1
            continue

        key = (district_id, director.lower())
        if key in existing_members:
            skipped_m += 1
            continue

        new_members.append({
            "district_id":    district_id,
            "name":           director,
            "position":       rec.get("position"),
            "party":          rec.get("party_affiliation"),
            "term_start":     rec.get("term_start"),
            "term_end":       rec.get("term_end"),
            "phone":          rec.get("phone"),
            "official_website": rec.get("website"),
        })
        existing_members.add(key)

    inserted_m = batch_insert(supabase, "school_board_members", new_members, dry_run)
    log.info("[school_board_members] %d inserted / %d skipped", inserted_m, skipped_m)


# ── 2. Law enforcement ─────────────────────────────────────────────────────────

def seed_law_enforcement(supabase: Client, wa_id: str, county_map: dict[str, str],
                         dry_run: bool) -> None:
    records = load_json("law_enforcement.json")
    if not records:
        return

    existing = load_existing_names(supabase, "law_enforcement_agencies", wa_id)
    new_rows: list[dict] = []
    skipped = 0

    for rec in records:
        name = (rec.get("name") or "").strip()
        if not name or name.lower() in existing:
            skipped += 1
            continue

        new_rows.append({
            "state_id":       wa_id,
            "county_id":      resolve_county(rec.get("jurisdiction"), county_map),
            "agency_type":    rec.get("agency_type"),
            "name":           name,
            "jurisdiction":   rec.get("jurisdiction"),
            "chief_name":     rec.get("chief_name"),
            "sworn_officers": parse_int(rec.get("sworn_officers")),
            "headquarters":   rec.get("headquarters"),
            "phone":          rec.get("phone"),
            "website":        rec.get("website"),
        })
        existing.add(name.lower())

    inserted = batch_insert(supabase, "law_enforcement_agencies", new_rows, dry_run)
    log.info("[law_enforcement_agencies] %d inserted / %d skipped", inserted, skipped)


# ── 3. Fire & EMS ─────────────────────────────────────────────────────────────

def seed_fire_ems(supabase: Client, wa_id: str, county_map: dict[str, str],
                  dry_run: bool) -> None:
    records = load_json("fire_ems.json")
    if not records:
        return

    existing = load_existing_names(supabase, "fire_ems_agencies", wa_id)
    new_rows: list[dict] = []
    skipped = 0

    for rec in records:
        name = (rec.get("name") or "").strip()
        if not name or name.lower() in existing:
            skipped += 1
            continue

        new_rows.append({
            "state_id":    wa_id,
            "county_id":   resolve_county(rec.get("jurisdiction"), county_map),
            "agency_type": rec.get("agency_type"),
            "name":        name,
            "jurisdiction": rec.get("jurisdiction"),
            "fire_chief":  rec.get("chief_name"),
            "stations":    parse_int(rec.get("stations")),
            "personnel":   parse_int(rec.get("personnel")),
            "headquarters": rec.get("headquarters"),
            "phone":       rec.get("phone"),
            "website":     rec.get("website"),
            "service_type": rec.get("service_type"),
        })
        existing.add(name.lower())

    inserted = batch_insert(supabase, "fire_ems_agencies", new_rows, dry_run)
    log.info("[fire_ems_agencies] %d inserted / %d skipped", inserted, skipped)


# ── 4. Hospitals ──────────────────────────────────────────────────────────────

def seed_hospitals(supabase: Client, wa_id: str, county_map: dict[str, str],
                   dry_run: bool) -> None:
    records = load_json("hospitals.json")
    if not records:
        return

    existing = load_existing_names(supabase, "hospitals", wa_id)
    new_rows: list[dict] = []
    skipped = 0

    for rec in records:
        name = (rec.get("name") or "").strip()
        if not name or name.lower() in existing:
            skipped += 1
            continue

        new_rows.append({
            "state_id":      wa_id,
            "county_id":     resolve_county(rec.get("county"), county_map),
            "ownership_type": rec.get("ownership_type"),
            "name":          name,
            "beds":          parse_int(rec.get("beds")),
            "trauma_level":  rec.get("trauma_level"),
            "health_system": rec.get("health_system"),
            "ceo":           rec.get("ceo"),
            "phone":         rec.get("phone"),
            "website":       rec.get("website"),
        })
        existing.add(name.lower())

    inserted = batch_insert(supabase, "hospitals", new_rows, dry_run)
    log.info("[hospitals] %d inserted / %d skipped", inserted, skipped)


# ── 5. Utilities & Transit ────────────────────────────────────────────────────

def seed_utilities_transit(supabase: Client, wa_id: str, county_map: dict[str, str],
                            dry_run: bool) -> None:
    records = load_json("utilities_transit.json")
    if not records:
        return

    existing = load_existing_names(supabase, "utilities_transit", wa_id)
    new_rows: list[dict] = []
    skipped = 0

    for rec in records:
        name = (rec.get("name") or "").strip()
        if not name or name.lower() in existing:
            skipped += 1
            continue

        # county_region may be "King County" or "King / Snohomish" — resolve first token
        county_raw = (rec.get("county_region") or "").split("/")[0].strip()

        new_rows.append({
            "state_id":        wa_id,
            "county_id":       resolve_county(county_raw, county_map),
            "category":        rec.get("category"),
            "name":            name,
            "service_type":    rec.get("service_type"),
            "customers_riders": rec.get("customers_riders"),
            "ceo":             rec.get("ceo"),
            "phone":           rec.get("phone"),
            "website":         rec.get("website"),
            "governing_board": rec.get("governing_board"),
        })
        existing.add(name.lower())

    inserted = batch_insert(supabase, "utilities_transit", new_rows, dry_run)
    log.info("[utilities_transit] %d inserted / %d skipped", inserted, skipped)


# ── 6. State agencies ─────────────────────────────────────────────────────────

def seed_state_agencies(supabase: Client, wa_id: str, dry_run: bool) -> None:
    records = load_json("state_agencies.json")
    if not records:
        return

    existing = load_existing_names(supabase, "state_agencies", wa_id)
    new_rows: list[dict] = []
    skipped = 0

    for rec in records:
        name = (rec.get("name") or "").strip()
        if not name or name.lower() in existing:
            skipped += 1
            continue

        new_rows.append({
            "state_id":        wa_id,
            "category":        rec.get("category"),
            "name":            name,
            "abbreviation":    rec.get("abbreviation"),
            "director":        rec.get("director_name"),
            "selection_method": rec.get("selection_method"),
            "budget":          rec.get("budget"),
            "employees":       rec.get("employees"),
            "headquarters":    rec.get("headquarters"),
            "phone":           rec.get("phone"),
            "website":         rec.get("website"),
            "mission":         rec.get("mission_summary"),
        })
        existing.add(name.lower())

    inserted = batch_insert(supabase, "state_agencies", new_rows, dry_run)
    log.info("[state_agencies] %d inserted / %d skipped", inserted, skipped)


# ── 7. Judiciary ──────────────────────────────────────────────────────────────

def seed_judiciary(supabase: Client, wa_id: str, county_map: dict[str, str],
                   dry_run: bool) -> None:
    records = load_json("judiciary.json")
    if not records:
        return

    existing = load_existing_names(supabase, "judiciary", wa_id)
    new_rows: list[dict] = []
    skipped = 0

    for rec in records:
        # Dedup key: judge_name (falls back to position if no name)
        name = (rec.get("judge_name") or rec.get("position") or "").strip()
        if not name or name.lower() in existing:
            skipped += 1
            continue

        new_rows.append({
            "state_id":        wa_id,
            "county_id":       resolve_county(rec.get("jurisdiction"), county_map),
            "court_level":     rec.get("court_level"),
            "court_name":      rec.get("court_name"),
            "position":        rec.get("position"),
            "judge_name":      name,
            "selection_method": rec.get("selection_method"),
            "appointed_by":    rec.get("appointed_by"),
            "term_start":      rec.get("term_start"),
            "term_end":        rec.get("term_end"),
            "jurisdiction":    rec.get("jurisdiction"),
            "official_website": rec.get("website"),
        })
        existing.add(name.lower())

    inserted = batch_insert(supabase, "judiciary", new_rows, dry_run)
    log.info("[judiciary] %d inserted / %d skipped", inserted, skipped)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Seed entity tables into Supabase")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be inserted without writing to DB")
    args = parser.parse_args()

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")

    supabase: Client = create_client(supabase_url, supabase_key)
    log.info("Connected to Supabase%s", " [DRY RUN]" if args.dry_run else "")

    wa_id = get_wa_state_id(supabase)
    county_map = get_county_map(supabase, wa_id)
    log.info("WA state_id=%s  counties loaded=%d", wa_id, len(county_map))

    seed_school_boards(supabase, wa_id, county_map, args.dry_run)
    seed_law_enforcement(supabase, wa_id, county_map, args.dry_run)
    seed_fire_ems(supabase, wa_id, county_map, args.dry_run)
    seed_hospitals(supabase, wa_id, county_map, args.dry_run)
    seed_utilities_transit(supabase, wa_id, county_map, args.dry_run)
    seed_state_agencies(supabase, wa_id, args.dry_run)
    seed_judiciary(supabase, wa_id, county_map, args.dry_run)

    log.info("=== seed-entities complete ===")


if __name__ == "__main__":
    main()
