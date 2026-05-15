"""
CommuneUSA City Sync Agent

Scrapes all 281 Washington municipality officials from the MRSC Officials
Directory at https://mrsc.org/research-tools/washington-city-and-town-profiles.

For each municipality:
  1. Upserts the city row in the municipalities table (keyed by mrsc_city_id).
  2. Fetches the city officials page.
  3. Inserts new officials, updates changed fields, and flags removed officials
     as inactive — same is_active pattern used by county-sync.py and data-sync.py.

Required env vars (loaded from communeusa-data/.env):
    SUPABASE_URL         — e.g. https://xyzxyz.supabase.co
    SUPABASE_SERVICE_KEY — service-role key (not anon) for write access

Run:
    pip install supabase requests beautifulsoup4 python-dotenv
    python agents/city-sync.py

Prerequisites:
    Run migrations/add_municipalities.sql in Supabase before the first run.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("city-sync")

# ── Constants ─────────────────────────────────────────────────────────────────

MRSC_PROFILES_URL  = "https://mrsc.org/research-tools/washington-city-and-town-profiles"
MRSC_OFFICIALS_URL = (
    "https://mrsc.org/research-tools/washington-city-and-town-profiles/city-officials"
)
REQUEST_DELAY = 1.0  # seconds between HTTP requests — be polite to MRSC

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CommuneUSA-bot/1.0; "
        "+https://communeusa.org)"
    )
}

MUTABLE_OFFICIAL_FIELDS     = {"official_name", "office_title", "phone", "email"}
MUTABLE_MUNICIPALITY_FIELDS = {"name", "type", "population", "government_form", "official_website"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def clean(val) -> Optional[str]:
    """Strip strings; coerce empty / whitespace-only values to None."""
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def strip_county_suffix(name: str) -> str:
    """'Grays Harbor County' → 'Grays Harbor'  (DB stores bare names)."""
    return name.removesuffix(" County").strip()


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_data_data(html: str, selector: str) -> list[dict]:
    """
    Extract and parse the JSON embedded in a Bootstrap Table's data-data
    attribute. BeautifulSoup decodes HTML entities automatically.

    Returns the parsed list, or [] if the element is not found or the JSON
    is malformed.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one(selector)
    if not table:
        return []
    raw = table.get("data-data", "")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("JSON parse error for selector %r: %s", selector, exc)
        return []


# ── Scraping ──────────────────────────────────────────────────────────────────

def fetch_municipalities() -> list[dict]:
    """
    Fetch the MRSC profiles page and return the embedded municipality JSON.

    Each record has at minimum: CityID (int), CityName, County, Class,
    FormofGov, Population, Website.
    """
    log.info("Fetching MRSC city profiles page …")
    resp = requests.get(MRSC_PROFILES_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY)

    rows = parse_data_data(resp.text, "#tableCityProfiles")
    if not rows:
        sys.exit(
            "ERROR: Could not parse municipality data from MRSC page. "
            "The page structure may have changed — check #tableCityProfiles."
        )

    log.info("Parsed %d municipalities", len(rows))
    return rows


def fetch_city_officials(city_id: int) -> list[dict]:
    """
    Fetch current officials for a single city from MRSC.

    The page contains two Bootstrap Tables:
      - Pending Updates (no data-sort-name attribute)
      - Current Officials (data-sort-name='Title')  ← the one we want

    Returns a list of dicts with at minimum: FullName, Title, Phone, Email.
    Returns [] on HTTP error rather than crashing the whole sync.
    """
    try:
        resp = requests.get(
            MRSC_OFFICIALS_URL,
            params={"cityID": city_id},
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("HTTP error fetching officials for cityID=%s: %s", city_id, exc)
        return []
    finally:
        time.sleep(REQUEST_DELAY)

    return parse_data_data(resp.text, "table[data-sort-name='Title']")


# ── Supabase helpers ──────────────────────────────────────────────────────────

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
    """Return {county_name: county_id} for all WA counties."""
    res = (
        supabase.table("counties")
        .select("id,name")
        .eq("state_id", wa_id)
        .execute()
    )
    if not res.data:
        sys.exit("ERROR: No counties found for WA. Run seed.py first.")
    return {r["name"]: r["id"] for r in res.data}


def upsert_municipality(
    supabase: Client,
    wa_id: str,
    county_id: str,
    mrsc_row: dict,
) -> str:
    """
    Upsert a municipality by mrsc_city_id (unique column).
    Returns the municipality's DB uuid.
    """
    city_id    = mrsc_row["CityID"]
    pop_raw    = mrsc_row.get("Population")
    population = int(pop_raw) if pop_raw is not None and str(pop_raw).strip().isdigit() else None

    payload = {
        "state_id":        wa_id,
        "county_id":       county_id,
        "mrsc_city_id":    city_id,
        "name":            clean(mrsc_row.get("CityName")) or "Unknown",
        "type":            clean(mrsc_row.get("Class")),
        "population":      population,
        "government_form": clean(mrsc_row.get("FormofGov")),
        "official_website": clean(mrsc_row.get("Website")),
    }

    res = (
        supabase.table("municipalities")
        .upsert(payload, on_conflict="mrsc_city_id")
        .execute()
    )

    if res.data:
        return res.data[0]["id"]

    # Fallback if upsert doesn't return data (older supabase-py versions)
    lookup = (
        supabase.table("municipalities")
        .select("id")
        .eq("mrsc_city_id", city_id)
        .single()
        .execute()
    )
    if not lookup.data:
        raise RuntimeError(
            f"Upsert succeeded but lookup found nothing for mrsc_city_id={city_id}"
        )
    return lookup.data["id"]


def get_existing_city_officials(
    supabase: Client,
    municipality_id: str,
) -> dict[tuple, dict]:
    """
    Return existing city officials for one municipality keyed by
    (official_name, office_title). Includes inactive rows so we can reactivate.
    """
    res = (
        supabase.table("officials")
        .select("id,official_name,office_title,phone,email,is_active")
        .eq("municipality_id", municipality_id)
        .eq("level", "city")
        .execute()
    )
    return {
        (r["official_name"], r["office_title"]): r
        for r in (res.data or [])
    }


# ── Core sync logic ───────────────────────────────────────────────────────────

def sync_city_officials(
    supabase: Client,
    wa_id: str,
    municipality_id: str,
    city_name: str,
    mrsc_officials: list[dict],
    now: str,
) -> tuple[int, int, int]:
    """
    Reconcile MRSC officials against the DB for one city.

    - INSERT   rows not in DB
    - UPDATE   rows where name, title, phone, or email changed
    - INACTIVE rows in DB but absent from MRSC data

    Returns (inserted, updated, flagged_inactive).
    """
    existing = get_existing_city_officials(supabase, municipality_id)

    # Build incoming lookup, skipping rows with no name or no title
    incoming: dict[tuple, dict] = {}
    for row in mrsc_officials:
        name  = clean(row.get("FullName"))
        # MRSC uses "Title" for standard titles; custom titles come in as "EnteredTitle"
        title = clean(row.get("Title") or row.get("EnteredTitle"))
        if not name or not title:
            continue
        key = (name, title)
        incoming[key] = {
            "official_name": name,
            "office_title":  title,
            "phone":         clean(row.get("Phone")),
            "email":         clean(row.get("Email")),
        }

    inserted = updated = flagged = 0

    # ── INSERT or UPDATE ──────────────────────────────────────────────────────
    for key, api_row in incoming.items():
        db_row = existing.get(key)

        if db_row is None:
            payload = {
                "state_id":        wa_id,
                "municipality_id": municipality_id,
                "level":           "city",
                "official_name":   api_row["official_name"],
                "office_title":    api_row["office_title"],
                "phone":           api_row["phone"],
                "email":           api_row["email"],
                "is_active":       True,
            }
            supabase.table("officials").insert(payload).execute()
            log.info(
                "[%s] INSERT  city  %-30s  —  %s  (%s)",
                now, city_name, api_row["official_name"], api_row["office_title"],
            )
            inserted += 1
            continue

        changes: dict = {}
        for field in ("phone", "email", "official_name", "office_title"):
            new_val = clean(api_row.get(field))
            old_val = clean(db_row.get(field))
            if new_val != old_val and new_val is not None:
                changes[field] = new_val

        if not db_row.get("is_active", True):
            changes["is_active"] = True

        if changes:
            supabase.table("officials").update(changes).eq("id", db_row["id"]).execute()
            log.info(
                "[%s] UPDATE  city  %-30s  —  %s  —  %s",
                now, city_name, api_row["official_name"], changes,
            )
            updated += 1

    # ── FLAG INACTIVE ─────────────────────────────────────────────────────────
    for key, db_row in existing.items():
        if key not in incoming and db_row.get("is_active", True):
            supabase.table("officials").update({"is_active": False}).eq("id", db_row["id"]).execute()
            official_name, office_title = key
            log.info(
                "[%s] INACTIVE city  %-30s  —  %s  (%s)",
                now, city_name, official_name, office_title,
            )
            flagged += 1

    return inserted, updated, flagged


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")

    missing = [
        name for name, val in {
            "SUPABASE_URL":         supabase_url,
            "SUPABASE_SERVICE_KEY": supabase_key,
        }.items() if not val
    ]
    if missing:
        sys.exit(f"ERROR: missing environment variables: {', '.join(missing)}")

    log.info("=== CommuneUSA city-sync starting ===")

    supabase: Client = create_client(supabase_url, supabase_key)
    wa_id      = get_wa_state_id(supabase)
    county_map = get_county_map(supabase, wa_id)
    log.info("Connected to Supabase — %d WA counties loaded", len(county_map))

    municipalities = fetch_municipalities()
    log.info("Processing %d municipalities …", len(municipalities))

    skipped_counties: set[str] = set()
    total_inserted = total_updated = total_flagged = 0
    cities_processed = cities_skipped = 0
    now = ts()

    for muni in municipalities:
        city_name   = clean(muni.get("CityName")) or "Unknown"
        city_id     = muni.get("CityID")
        # MRSC lists some cities that span county lines as "King/Snohomish" or
        # "Douglas/ Grant/ Okanogan". Split on "/" and use the first county
        # that exists in our DB so these cities are not silently skipped.
        raw_county = clean(muni.get("County")) or ""
        county_candidates = [strip_county_suffix(c.strip()) for c in raw_county.split("/")]

        if not city_id:
            log.warning("Skipping row with no CityID: %s", muni)
            cities_skipped += 1
            continue

        county_id = next((county_map[c] for c in county_candidates if c in county_map), None)
        if not county_id:
            skipped_counties.add(raw_county)
            log.warning(
                "County %r not found in DB — skipping %s (cityID=%s)",
                raw_county, city_name, city_id,
            )
            cities_skipped += 1
            continue

        # Upsert the municipality row so it always exists before we add officials
        try:
            municipality_id = upsert_municipality(supabase, wa_id, county_id, muni)
        except Exception as exc:
            log.error("Failed to upsert municipality %s (cityID=%s): %s", city_name, city_id, exc)
            cities_skipped += 1
            continue

        # Fetch and sync officials for this city
        officials_raw = fetch_city_officials(city_id)

        if not officials_raw:
            log.info("No officials listed for %s (cityID=%s)", city_name, city_id)
            cities_processed += 1
            continue

        ins, upd, flg = sync_city_officials(
            supabase, wa_id, municipality_id, city_name, officials_raw, now
        )
        total_inserted += ins
        total_updated  += upd
        total_flagged  += flg
        cities_processed += 1

        if cities_processed % 50 == 0:
            log.info(
                "Progress: %d/%d cities processed (inserted=%d updated=%d flagged=%d)",
                cities_processed, len(municipalities),
                total_inserted, total_updated, total_flagged,
            )

    if skipped_counties:
        log.warning(
            "Skipped %d cities due to unknown counties: %s",
            cities_skipped, ", ".join(sorted(skipped_counties)),
        )

    log.info(
        "=== city-sync complete — %d cities processed, %d skipped, "
        "%d officials inserted, %d updated, %d flagged inactive ===",
        cities_processed, cities_skipped,
        total_inserted, total_updated, total_flagged,
    )


if __name__ == "__main__":
    main()
