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
import provenance
import os
import re
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
        "+https://www.communeusa.com)"
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
    # The directory moved to a new page in 2026; try it first.
    try:
        resp0 = requests.get(NEW_DIRECTORY, headers=HEADERS, timeout=30)
        if resp0.status_code == 200 and "category-staff" in resp0.text:
            log.info("Using the new directory page")
            return resp0.text
        log.warning("New directory page not usable (HTTP %s); trying the old route", resp0.status_code)
    except requests.RequestException as exc:
        log.warning("New directory page failed (%s); trying the old route", exc)

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


CREDENTIALS = re.compile(r",?\s+(D-ABMDI|F-ABMDI|MD|M\.D\.|DO|PhD)\s*$", re.I)


def clean_person_name(raw: str) -> Optional[str]:
    """'Bill Leach, D-ABMDI' -> 'Bill Leach'. Returns None for vacant seats."""
    name = re.sub(r"\s+", " ", raw).strip()
    if not name or re.fullmatch(r"(vacant\s*)+", name, re.I):
        return None
    for _ in range(2):
        name = CREDENTIALS.sub("", name).strip()
    return name or None


def base_title(title: str) -> str:
    """'Sheriff (Interim)' -> 'sheriff'."""
    return re.sub(r"\s*\(.*?\)", "", title or "").strip().lower()


WSAC_DIRECTORY = "https://wsac.org/member-directory/"
BOARD_TITLE = re.compile(r"commissioner|council|executive|chair", re.I)


def board_covers(title: str) -> bool:
    """Offices the WSAC directory is responsible for (including old chair rows)."""
    return bool(BOARD_TITLE.search(title or ""))


SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "wsac-board-snapshot.json"


def fetch_board_page() -> Optional[str]:
    """Live WSAC page, or None when the site refuses us (it returns 403 to GitHub's servers)."""
    try:
        resp = requests.get(WSAC_DIRECTORY, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            return resp.text
        log.warning("WSAC directory returned HTTP %s", resp.status_code)
    except requests.RequestException as exc:
        log.warning("WSAC directory could not be fetched: %s", exc)
    return None


def board_district(raw: Optional[str]) -> Optional[str]:
    """'District 3' (also the directory's 'Disrtict 3') -> '3'; 'At-Large (A)' -> 'At-Large'; anything else -> None."""
    text = (raw or "").strip()
    if re.search(r"at[- ]?large", text, re.I):
        return "At-Large"
    m = re.search(r"\b(\d{1,2})\b", text)
    return m.group(1) if m else None


def board_rows_to_officials(table_rows: list[list[str]]) -> list[dict]:
    """Rows of [county, district, title, first name, last name] -> officials."""
    results: list[dict] = []
    for cells in table_rows:
        if len(cells) < 5 or not cells[0] or not cells[0].endswith(" County"):
            continue
        county, raw_district, raw_title, first, last = [clean(c) for c in cells[:5]]
        if not first or not last or first.lower() == "vacant":
            continue
        m = re.match(r"^\S+\s*\((.+)\)$", first)      # 'Charles (Chuck)' -> 'Chuck'
        first = m.group(1) if m else first
        low = (raw_title or "").lower()
        if "executive" in low and "pro" not in low:
            title = "County Executive"
        elif "commissioner" in low:
            title = "County Commissioner"
        elif "council" in low:
            title = "County Councilmember"
        else:
            continue
        results.append({
            "county_name": strip_county_suffix(county),
            "official_name": re.sub(r"\s+", " ", f"{first} {last}").strip(),
            "office_title": title, "phone": None, "email": None,
            "district": board_district(raw_district),
        })
    log.info("Parsed %d county board members across %d counties (WSAC)",
             len(results), len({r["county_name"] for r in results}))
    return results


def parse_board(html: str) -> list[dict]:
    """Parse the WSAC member directory table (County, District, Title, First Name, Last Name)."""
    soup = BeautifulSoup(html, "html.parser")
    rows = [[clean(td.get_text(" ", strip=True)) or "" for td in tr.find_all("td")]
            for tr in soup.select("table tbody tr")]
    return board_rows_to_officials(rows)


def load_board() -> list[dict]:
    """Live page when reachable, otherwise the dated snapshot saved in data/."""
    import json
    html = fetch_board_page()
    if html:
        officials = parse_board(html)
        if officials:
            return officials
        log.warning("WSAC page loaded but no board members were found")
    snap = json.loads(SNAPSHOT.read_text())
    msg = (f"Using the saved WSAC snapshot from {snap.get('captured')} because the live page "
           "is not reachable. Board members will not update until the snapshot is refreshed.")
    log.warning(msg)
    print(f"::warning::{msg}")
    return board_rows_to_officials(snap["rows"])


NEW_DIRECTORY = "https://www.countyofficials.org/m/directory/department?did=193"


def parse_officials_new(html: str) -> list[dict]:
    """
    Parse the redesigned countyofficials.org page (2026): an <h3> per county followed by a
    <ul class="category-staff"> whose <li> rows hold name link, title and phone link.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []
    for h3 in soup.find_all("h3"):
        heading = h3.get_text(" ", strip=True)
        if not heading.endswith(" County"):
            continue
        county = strip_county_suffix(heading)
        ul = h3.find_next("ul", class_="category-staff")
        if ul is None:
            continue
        for li in ul.find_all("li", recursive=False):
            link = li.find("a", href=lambda h: h and "directory/employee" in h)
            if link is None:
                continue
            name = clean(link.get_text(" ", strip=True))
            title_div = link.find_next("div")
            title = clean(title_div.get_text(" ", strip=True)) if title_div else None
            tel = li.find("a", href=lambda h: h and h.startswith("tel:"))
            phone = clean(tel.get_text(" ", strip=True)) if tel else None
            mail = li.find("a", href=lambda h: h and h.startswith("mailto:"))
            email = clean(mail.get_text(" ", strip=True)) if mail else None
            if not name or not title:
                continue
            name = clean_person_name(name)
            if not name:
                continue   # vacant seat
            results.append({
                "county_name": county, "official_name": name,
                "office_title": title, "phone": phone, "email": email,
            })
    log.info("Parsed %d county officials across %d counties (new layout)",
             len(results), len({r["county_name"] for r in results}))
    return results


def parse_officials(html: str) -> list[dict]:
    results = parse_officials_new(html)
    if results:
        return results
    return parse_officials_old(html)


def parse_officials_old(html: str) -> list[dict]:
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
        .select("id,official_name,office_title,county_id,phone,email,district,is_active")
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
    covers=None,
    source: str = "source page",
    source_key: str = "waco",
) -> None:
    """
    Reconcile scraped officials against the DB.

    `covers(office_title) -> bool` says which offices this source is responsible for. Only
    those can be flagged inactive. Default: the offices that appear in `incoming`.

    - INSERT   rows not in DB
    - UPDATE   rows where phone, email, official_name, or office_title changed
    - INACTIVE rows in DB but absent from the scraped data
    """
    existing = get_existing_county_officials(supabase)
    now = ts()

    # Guardrail: a broken or blocked source page parses as "no officials". Without this check
    # the sync would mark every county official inactive (it did, on 2026-11-08).
    if covers is None:
        covered_titles = {base_title(r["office_title"]) for r in incoming}
        covers = lambda title: base_title(title) in covered_titles   # noqa: E731
    active_now = sum(1 for k, r in existing.items() if r.get("is_active", True) and covers(k[1]))
    if not incoming:
        sys.exit(f"ERROR: {source} returned 0 county officials. Not changing anything. "
                 "The page layout may have changed or the site may be blocking this runner.")
    if active_now and len(incoming) < 0.7 * active_now:
        sys.exit(f"ERROR: {source} returned only {len(incoming)} county officials but "
                 f"{active_now} are active in the database for those offices. Not changing anything.")

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
                "district":      api_row.get("district"),
                "is_active":     True,
                **provenance.fields(source_key),
            }
            supabase.table("officials").insert(payload).execute()
            log.info("[%s] INSERT  county  %s  —  %s  (%s)",
                     now, api_row["county_name"], official_name, office_title)
            inserted += 1
            continue

        changes: dict = {}
        for field in ("phone", "email", "official_name", "office_title", "district"):
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

    confirmed = provenance.stamp(
        supabase, [existing[k]["id"] for k in incoming_keyed if k in existing], source_key)
    log.info("Stamped %d county officials as verified against %s", confirmed, source_key)

    # ── FLAG INACTIVE ─────────────────────────────────────────────────────────
    for key, db_row in existing.items():
        if key not in incoming_keyed and db_row.get("is_active", True) \
                and covers(key[1]):
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

    sync_county_officials(supabase, wa_id, county_map, officials, source="the WACO directory", source_key="waco")

    board = load_board()
    sync_county_officials(supabase, wa_id, county_map, board, covers=board_covers,
                          source="the WSAC directory", source_key="wsac")

    log.info("=== county-sync complete ===")


if __name__ == "__main__":
    main()
