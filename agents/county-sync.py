"""
CommuneUSA County Sync Agent

Scrapes all 39 Washington county officials from the MRSC Officials Directory,
which sources its data from countyofficials.org. Inserts new officials, updates
changed fields, and flags removed officials as inactive rather than deleting them.

Source:  https://mrsc.org/research-tools/county-profiles
         → Officials column → https://www.countyofficials.org/Directory.aspx?DID=193

Required env vars (loaded from communeusa-data/.env):
    SUPABASE_URL         — e.g. https://xyzxyz.supabase.co
    SUPABASE_SERVICE_KEY — service-role key (not anon) for write access

Run:
    pip install supabase requests beautifulsoup4 python-dotenv
    python agents/county-sync.py
"""

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
log = logging.getLogger("county-sync")

# ── Constants ─────────────────────────────────────────────────────────────────

MRSC_COUNTY_PROFILES = "https://mrsc.org/research-tools/county-profiles"
OFFICIALS_DIRECTORY  = "https://www.countyofficials.org/Directory.aspx?DID=193"
REQUEST_DELAY        = 1.0  # seconds between HTTP requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CommuneUSA-bot/1.0; "
        "+https://communeusa.org)"
    )
}

MUTABLE_FIELDS = {
    "official_name", "office_title", "phone", "email", "is_active"
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def clean(val) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def last_first_to_first_last(name: str) -> str:
    """Convert 'Smith, Jane A.' → 'Jane A. Smith'."""
    parts = name.split(",", 1)
    if len(parts) == 2:
        return f"{parts[1].strip()} {parts[0].strip()}"
    return name.strip()


def strip_county_suffix(name: str) -> str:
    """'Adams County' → 'Adams'  (DB stores bare county names)."""
    return name.removesuffix(" County").strip()


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Scraping ──────────────────────────────────────────────────────────────────

def fetch_officials_page() -> str:
    """
    Fetch the single page that contains all 39 WA county officials tables.
    First verifies the URL is still correct by checking the MRSC county profiles
    page, then fetches the officials directory.
    """
    # Step 1 — confirm the officials directory URL from MRSC
    log.info("Fetching MRSC county profiles page to verify officials URL …")
    resp = requests.get(MRSC_COUNTY_PROFILES, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", id="tblCountyProfiles")
    officials_url = OFFICIALS_DIRECTORY  # fallback

    if table and table.get("data-data"):
        import json
        county_data = json.loads(table["data-data"])
        urls = {d.get("Officials") for d in county_data if d.get("Officials")}
        if urls:
            officials_url = next(iter(urls))
            log.info("Officials directory URL confirmed: %s", officials_url)

    time.sleep(REQUEST_DELAY)

    # Step 2 — fetch the officials directory
    log.info("Fetching officials directory …")
    resp2 = requests.get(officials_url, headers=HEADERS, timeout=30)
    resp2.raise_for_status()
    return resp2.text


def parse_officials(html: str) -> list[dict]:
    """
    Parse the countyofficials.org directory page.

    Returns a list of dicts with keys:
        county_name   — bare name matching DB  (e.g. 'Adams')
        official_name — first-last order
        office_title  — as printed
        phone         — cleaned, or None
        email         — cleaned, or None
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []
    current_county = ""

    for tag in soup.find_all(True):
        # County section headers
        if "DirectoryCategoryText" in (tag.get("class") or []):
            text = tag.get_text(strip=True)
            if text:
                current_county = strip_county_suffix(text)
            continue

        # Official rows inside summary="City Directory" tables
        if tag.name == "table" and tag.get("summary") == "City Directory":
            for row in tag.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
                if len(cells) < 2:
                    continue
                raw_name = clean(cells[0])
                title    = clean(cells[1])
                email    = clean(cells[2]) if len(cells) > 2 else None
                phone    = clean(cells[3]) if len(cells) > 3 else None

                # Skip the header row and empty rows
                if not raw_name or raw_name.lower() == "name":
                    continue
                if not title or title.lower() == "title":
                    continue

                official_name = last_first_to_first_last(raw_name)

                results.append({
                    "county_name":   current_county,
                    "official_name": official_name,
                    "office_title":  title,
                    "phone":         phone,
                    "email":         email,
                })

    log.info("Parsed %d county officials across %d counties",
             len(results),
             len({r["county_name"] for r in results}))
    return results


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


def get_existing_county_officials(supabase: Client) -> dict[tuple, dict]:
    """
    Return existing county-level officials keyed by (official_name, office_title, county_id).
    Includes inactive rows so we can reactivate if they return.
    """
    res = (
        supabase.table("officials")
        .select("id,official_name,office_title,county_id,phone,email,is_active")
        .eq("level", "county")
        .execute()
    )
    return {
        (r["official_name"], r["office_title"], r["county_id"]): r
        for r in (res.data or [])
    }


# ── Core sync logic ───────────────────────────────────────────────────────────

def sync_county_officials(
    supabase: Client,
    wa_id: str,
    county_map: dict[str, str],
    incoming: list[dict],
) -> None:
    """
    Reconcile scraped officials against the DB.

    - INSERT   rows not in DB
    - UPDATE   rows where phone, email, official_name, or office_title changed
    - INACTIVE rows in DB but absent from the scraped data
    """
    existing = get_existing_county_officials(supabase)
    now = ts()

    # Build incoming lookup, skipping officials whose county isn't in the DB
    incoming_keyed: dict[tuple, dict] = {}
    skipped_counties: set[str] = set()

    for row in incoming:
        county_name = row["county_name"]
        county_id   = county_map.get(county_name)
        if not county_id:
            skipped_counties.add(county_name)
            continue
        key = (row["official_name"], row["office_title"], county_id)
        incoming_keyed[key] = {**row, "county_id": county_id}

    if skipped_counties:
        log.warning(
            "Skipped officials for counties not found in DB: %s",
            ", ".join(sorted(skipped_counties))
        )

    inserted = updated = flagged = 0

    # ── INSERT or UPDATE ──────────────────────────────────────────────────────
    for key, api_row in incoming_keyed.items():
        official_name, office_title, county_id = key
        db_row = existing.get(key)

        if db_row is None:
            payload = {
                "state_id":      wa_id,
                "county_id":     county_id,
                "level":         "county",
                "official_name": official_name,
                "office_title":  office_title,
                "phone":         api_row.get("phone"),
                "email":         api_row.get("email"),
                "is_active":     True,
            }
            supabase.table("officials").insert(payload).execute()
            log.info("[%s] INSERT  county  %s  —  %s  (%s)",
                     now, api_row["county_name"], official_name, office_title)
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
            log.info("[%s] UPDATE  county  %s  —  %s  —  %s",
                     now, api_row["county_name"], official_name, changes)
            updated += 1

    # ── FLAG INACTIVE ─────────────────────────────────────────────────────────
    for key, db_row in existing.items():
        if key not in incoming_keyed and db_row.get("is_active", True):
            supabase.table("officials").update({"is_active": False}).eq("id", db_row["id"]).execute()
            official_name, office_title, _ = key
            log.info("[%s] INACTIVE county  %s  —  %s", now, official_name, office_title)
            flagged += 1

    log.info(
        "COUNTY sync complete — %d inserted, %d updated, %d flagged inactive",
        inserted, updated, flagged,
    )


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

    log.info("=== CommuneUSA county-sync starting ===")

    supabase: Client = create_client(supabase_url, supabase_key)
    wa_id      = get_wa_state_id(supabase)
    county_map = get_county_map(supabase, wa_id)
    log.info("Connected to Supabase — %d WA counties loaded", len(county_map))

    html      = fetch_officials_page()
    officials = parse_officials(html)

    sync_county_officials(supabase, wa_id, county_map, officials)

    log.info("=== county-sync complete ===")


if __name__ == "__main__":
    main()
