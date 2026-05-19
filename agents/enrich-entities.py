#!/usr/bin/env python3
"""
enrich-entities.py

Detail enrichment agent for stub records in entity JSON files.
Fills in missing contact data (website, phone, chief/superintendent/director name)
for records flagged with _needs_detail: true, plus enriches real records that
have a website but are missing leadership or contact fields.

Targets:
  - state_agencies:       scrape {website}/about|leadership|contact for director + phone
  - law_enforcement:      infer city website from agency name, scrape for chief + phone
  - school_districts:     scrape OSPI k12.wa.us district pages for superintendent + website
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv
from supabase import create_client

# ── config ────────────────────────────────────────────────────────────────────

DELAY = 2.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 12

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output"

load_dotenv(ROOT / ".env")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

# ── HTTP helpers ──────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers["User-Agent"] = USER_AGENT

_last_request: float = 0.0


def get(url: str, *, min_delay: float = DELAY) -> requests.Response | None:
    global _last_request
    elapsed = time.time() - _last_request
    if elapsed < min_delay:
        time.sleep(min_delay - elapsed)
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        _last_request = time.time()
        return r if r.status_code == 200 else None
    except Exception as exc:
        _last_request = time.time()
        print(f"    [HTTP] {url} → {exc}")
        return None


def soup(r: requests.Response) -> BeautifulSoup:
    return BeautifulSoup(r.text, "html.parser")


# ── text utilities ────────────────────────────────────────────────────────────

_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}")
_TITLE_WORDS = {
    "director", "secretary", "commissioner", "superintendent", "chair",
    "chairman", "chairwoman", "chief", "administrator", "executive",
    "officer", "president", "manager", "director-general",
}
_NAME_NOISE = re.compile(
    r"\b(the|of|and|at|for|in|on|to|a|an|wa|washington|state|office|"
    r"department|agency|board|commission|authority|bureau|division|"
    r"council|institute|center|program|service|office)\b",
    re.I,
)


def first_phone(text: str) -> str | None:
    m = _PHONE_RE.search(text)
    if not m:
        return None
    raw = m.group()
    digits = re.sub(r"\D", "", raw)
    return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}" if len(digits) == 10 else raw


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s]+", "-", text)


def city_from_agency_name(name: str) -> str:
    """Extract city/jurisdiction name from agency name like 'Airway Heights Police Department'."""
    name = re.sub(
        r"\b(police|sheriff|department|office|division|bureau|tribal|"
        r"port|transit|authority|patrol|county|city|town|of)\b",
        "", name, flags=re.I,
    )
    return re.sub(r"\s+", " ", name).strip()


# ── leadership extraction ─────────────────────────────────────────────────────

_TITLE_PATS = [
    re.compile(
        r"(?:Director|Secretary|Commissioner|Superintendent|Administrator|"
        r"Chief Executive|Chief|Manager|Chair(?:man|woman)?|President)"
        r"[:\s,]+([A-Z][a-z]+(?: [A-Z][a-z]+){1,3})",
        re.I,
    ),
    re.compile(
        r"([A-Z][a-z]+(?: [A-Z][a-z]+){1,3})"
        r"[,\s]+(?:Director|Secretary|Commissioner|Superintendent|"
        r"Administrator|Chief Executive|Chief|Manager|Chair(?:man|woman)?|President)\b",
        re.I,
    ),
]


def extract_leader_name(text: str) -> str | None:
    for pat in _TITLE_PATS:
        m = pat.search(text)
        if m:
            candidate = m.group(1).strip()
            # Basic sanity: 2–4 words, no single-letter words except middle initials
            parts = candidate.split()
            if 2 <= len(parts) <= 4:
                return candidate
    return None


def scrape_page_for_contact(url: str) -> dict[str, str | None]:
    """Fetch a URL and extract leader name, phone, and address."""
    r = get(url)
    if not r:
        return {}
    text = r.text
    s = soup(r)
    result: dict[str, str | None] = {}

    # Strip script/style noise
    for tag in s(["script", "style", "nav", "footer"]):
        tag.decompose()
    clean_text = s.get_text(" ", strip=True)

    name = extract_leader_name(clean_text)
    if name:
        result["leader"] = name

    phone = first_phone(clean_text)
    if phone:
        result["phone"] = phone

    return result


def try_pages(base_url: str, paths: list[str]) -> dict[str, str | None]:
    """Try multiple subpaths on a base URL; return first non-empty result."""
    for path in paths:
        url = urljoin(base_url, path)
        data = scrape_page_for_contact(url)
        if data:
            print(f"    [hit] {url}")
            return data
    return {}


# ── state agencies enrichment ─────────────────────────────────────────────────

AGENCY_SUBPAGES = [
    "/about",
    "/about-us",
    "/leadership",
    "/about/leadership",
    "/about/staff",
    "/contact",
    "/contact-us",
    "/about/contact",
    "/",
]


def enrich_state_agencies(supabase, wa_id: str, dry_run: bool) -> None:
    print("\n[state_agencies] Enriching stubs and records missing director_name...")

    # Load from JSON
    with open(OUTPUT_DIR / "state_agencies.json") as f:
        records = json.load(f)

    stubs = [r for r in records if r.get("_needs_detail") and r.get("website")]
    missing_dir = [
        r for r in records
        if not r.get("_needs_detail") and not r.get("director_name") and r.get("website")
    ]
    targets = stubs + missing_dir
    print(f"  {len(stubs)} stubs with website + {len(missing_dir)} real missing director = {len(targets)} targets")

    enriched = 0
    for rec in targets:
        raw_site = rec["website"].strip()
        if not raw_site.startswith("http"):
            base = f"https://{raw_site}"
        else:
            base = raw_site

        print(f"  → {rec['name']} ({raw_site})")
        data = try_pages(base, AGENCY_SUBPAGES)
        if not data:
            print("    [skip] no data found")
            continue

        updates: dict[str, Any] = {}
        if data.get("leader") and not rec.get("director_name"):
            updates["director"] = data["leader"]
            print(f"    director: {data['leader']}")
        if data.get("phone") and not rec.get("phone"):
            updates["phone"] = data["phone"]
            print(f"    phone: {data['phone']}")

        if not updates:
            print("    [skip] no new fields extracted")
            continue

        if dry_run:
            print("    [dry-run] would update", updates)
            enriched += 1
            continue

        resp = (
            supabase.table("state_agencies")
            .update(updates)
            .eq("state_id", wa_id)
            .eq("name", rec["name"])
            .execute()
        )
        if resp.data:
            enriched += 1
        else:
            print(f"    [warn] upsert returned no rows for {rec['name']}")

    print(f"  [state_agencies] {enriched}/{len(targets)} enriched")


# ── law enforcement enrichment ────────────────────────────────────────────────

# WA city → .gov URL patterns to try
def law_enforcement_candidate_urls(agency_name: str, jurisdiction: str) -> list[str]:
    """Generate candidate base URLs for a WA law enforcement agency."""
    urls: list[str] = []

    # County sheriff
    if "sheriff" in agency_name.lower():
        county_raw = jurisdiction.replace(" County", "").strip().lower()
        county_slug = slug(county_raw)
        # Common county URL patterns in WA
        urls += [
            f"https://www.{county_slug}county.gov",
            f"https://www.co.{county_slug}.wa.us",
            f"https://{county_slug}county.wa.gov",
            f"https://www.{county_slug}so.org",  # sheriff's office pattern
        ]
        return urls

    # Tribal
    if "tribal" in agency_name.lower() or "tribe" in agency_name.lower():
        return []  # Too varied; skip

    # Municipal police → extract city name
    city = city_from_agency_name(agency_name)
    if not city:
        return []

    city_lower = city.lower()
    city_nodash = city_lower.replace(" ", "")
    city_hyphen = city_lower.replace(" ", "-")

    urls += [
        f"https://www.{city_nodash}.gov",
        f"https://www.cityof{city_nodash}.gov",
        f"https://www.ci.{city_hyphen}.wa.us",
        f"https://{city_hyphen}.wa.gov",
        f"https://www.{city_nodash}wa.gov",
    ]
    return urls


POLICE_SUBPAGES = [
    "/police",
    "/police-department",
    "/pd",
    "/public-safety",
    "/departments/police",
    "/government/police",
    "/",
]


def enrich_law_enforcement(supabase, wa_id: str, dry_run: bool) -> None:
    print("\n[law_enforcement] Enriching stub records...")

    with open(OUTPUT_DIR / "law_enforcement.json") as f:
        records = json.load(f)

    stubs = [r for r in records if r.get("_needs_detail")]
    print(f"  {len(stubs)} stubs to enrich")

    enriched = 0
    for rec in stubs:
        name = rec["name"]
        jurisdiction = rec.get("jurisdiction", "")
        print(f"  → {name}")

        candidate_bases = law_enforcement_candidate_urls(name, jurisdiction)
        if not candidate_bases:
            print("    [skip] no URL candidates (tribal or unknown)")
            continue

        found_base: str | None = None
        found_data: dict = {}

        # First: verify a base URL resolves
        for base in candidate_bases:
            r = get(base)
            if r:
                found_base = base
                break

        if not found_base:
            print("    [skip] no reachable URL found")
            continue

        print(f"    base: {found_base}")

        # Try police-specific subpages, then root
        data = try_pages(found_base, POLICE_SUBPAGES)
        if not data:
            print("    [skip] no contact data extracted")
            continue

        updates: dict[str, Any] = {"website": found_base.rstrip("/")}
        if data.get("phone"):
            updates["phone"] = data["phone"]
            print(f"    phone: {data['phone']}")
        if data.get("leader"):
            updates["chief_name"] = data["leader"]
            print(f"    chief: {data['leader']}")

        if dry_run:
            print("    [dry-run] would update", updates)
            enriched += 1
            continue

        resp = (
            supabase.table("law_enforcement_agencies")
            .update(updates)
            .eq("state_id", wa_id)
            .eq("name", name)
            .execute()
        )
        if resp.data:
            enriched += 1

    print(f"  [law_enforcement] {enriched}/{len(stubs)} enriched")


# ── school districts enrichment via OSPI ─────────────────────────────────────

# OSPI district pages: k12.wa.us/about-ospi/school-districts is a UI, not an API.
# We'll try OSPI's data.wa.gov for a "School District Contacts" dataset, then fall
# back to scraping each district's website.

OSPI_CONTACTS_DATASETS = [
    # Try known OSPI dataset IDs that may contain contact info
    "https://data.wa.gov/resource/rxjk-6ieq.json",   # enrollment (known - no contacts)
    "https://data.wa.gov/resource/7s2n-cxwk.json",   # potential contacts dataset
    "https://data.wa.gov/resource/6y7d-9bwt.json",
]


def fetch_ospi_contacts() -> dict[str, dict]:
    """Try OSPI Socrata datasets for district superintendent contacts.
    Returns dict keyed by normalized district name → {superintendent, phone, website}."""
    result: dict[str, dict] = {}

    for url in OSPI_CONTACTS_DATASETS:
        r = get(f"{url}?$limit=500&$select=*&organizationlevel=District")
        if not r:
            r = get(f"{url}?$limit=500")
        if not r:
            continue
        try:
            rows = r.json()
            if not isinstance(rows, list) or not rows:
                continue
            keys = set(rows[0].keys()) if rows else set()
            print(f"    [OSPI] {url} — cols: {sorted(keys)[:8]}")
            # Look for superintendent or contact fields
            if any(k for k in keys if "super" in k.lower() or "contact" in k.lower() or "director" in k.lower()):
                print(f"    [OSPI] Found contact dataset at {url}")
                for row in rows:
                    dist_name = (
                        row.get("districtname") or row.get("district_name") or
                        row.get("organizationname") or ""
                    ).strip()
                    if not dist_name:
                        continue
                    entry: dict = {}
                    for k, v in row.items():
                        if "super" in k.lower():
                            entry["superintendent"] = str(v).strip()
                        if k.lower() in ("phone", "telephone", "phone_number"):
                            entry["phone"] = str(v).strip()
                        if k.lower() in ("website", "url", "web_address"):
                            entry["website"] = str(v).strip()
                    if entry:
                        result[dist_name.lower()] = entry
                if result:
                    return result
        except Exception as exc:
            print(f"    [OSPI] parse error {url}: {exc}")

    return result


def scrape_district_website(website: str) -> dict[str, str | None]:
    """Scrape a school district website for superintendent name and contact."""
    if not website:
        return {}
    if not website.startswith("http"):
        base = f"https://{website}"
    else:
        base = website

    subpages = [
        "/about",
        "/about-us",
        "/district",
        "/district-info",
        "/superintendent",
        "/leadership",
        "/staff",
        "/contact",
        "/",
    ]
    data = try_pages(base, subpages)
    return data


def enrich_school_districts(supabase, wa_id: str, dry_run: bool) -> None:
    print("\n[school_districts] Enriching stub records...")

    with open(OUTPUT_DIR / "school_boards.json") as f:
        records = json.load(f)

    stubs = [r for r in records if r.get("_needs_detail")]
    print(f"  {len(stubs)} district stubs to enrich")

    # Step 1: try OSPI data.wa.gov contacts
    print("  Fetching OSPI contact datasets...")
    ospi_contacts = fetch_ospi_contacts()
    print(f"  OSPI contacts found: {len(ospi_contacts)}")

    enriched_from_ospi = 0
    remaining_stubs = []

    for rec in stubs:
        dist_name = rec["district_name"]
        key = dist_name.lower()
        if key in ospi_contacts:
            ospi = ospi_contacts[key]
            print(f"  [OSPI match] {dist_name}: {ospi}")
            if dry_run:
                enriched_from_ospi += 1
                continue
            updates = {k: v for k, v in ospi.items() if v}
            resp = (
                supabase.table("school_districts")
                .update(updates)
                .eq("state_id", wa_id)
                .eq("name", dist_name)
                .execute()
            )
            if resp.data:
                enriched_from_ospi += 1
        else:
            remaining_stubs.append(rec)

    print(f"  {enriched_from_ospi} enriched via OSPI, {len(remaining_stubs)} remaining for web scrape")

    # Step 2: for remaining stubs, try to find and scrape district website
    enriched_from_web = 0
    for rec in remaining_stubs[:50]:  # cap at 50 to avoid very long runs
        dist_name = rec["district_name"]
        county = rec.get("county", "")

        # Derive a candidate URL from district name
        name_slug = slug(
            dist_name.lower()
            .replace("school district", "")
            .replace("school dist", "")
            .replace("no.", "")
            .strip()
        )
        # Common patterns for WA school districts
        candidate_bases = [
            f"https://www.{name_slug}.wednet.edu",   # WA ed network
            f"https://{name_slug}.wednet.edu",
            f"https://www.{name_slug}sd.org",
            f"https://www.{name_slug.replace('-', '')}sd.org",
        ]

        print(f"  → {dist_name}")
        found_base: str | None = None
        for base in candidate_bases:
            r = get(base)
            if r:
                found_base = base
                print(f"    base: {found_base}")
                break

        if not found_base:
            print("    [skip] no reachable URL found")
            continue

        data = scrape_district_website(found_base)
        if not data:
            print("    [skip] no data extracted")
            continue

        updates: dict[str, Any] = {"official_website": found_base}
        if data.get("leader"):
            updates["superintendent"] = data["leader"]
            print(f"    superintendent: {data['leader']}")
        if data.get("phone"):
            updates["phone"] = data["phone"]
            print(f"    phone: {data['phone']}")

        if dry_run:
            print("    [dry-run] would update", updates)
            enriched_from_web += 1
            continue

        resp = (
            supabase.table("school_districts")
            .update(updates)
            .eq("state_id", wa_id)
            .eq("name", dist_name)
            .execute()
        )
        if resp.data:
            enriched_from_web += 1

    total = enriched_from_ospi + enriched_from_web
    print(f"  [school_districts] {total}/{len(stubs)} enriched "
          f"(OSPI: {enriched_from_ospi}, web: {enriched_from_web})")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich entity stub records with contact data")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    parser.add_argument(
        "--only",
        choices=["state-agencies", "law-enforcement", "school-districts"],
        help="Run only one enrichment target",
    )
    args = parser.parse_args()

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Resolve WA state ID
    wa_row = supabase.table("states").select("id").eq("abbreviation", "WA").single().execute()
    wa_id: str = wa_row.data["id"]
    print(f"WA state_id: {wa_id}")

    if args.dry_run:
        print("[DRY RUN] — no writes to Supabase")

    targets = args.only.split(",") if args.only else ["state-agencies", "law-enforcement", "school-districts"]

    if "state-agencies" in targets:
        enrich_state_agencies(supabase, wa_id, dry_run=args.dry_run)

    if "law-enforcement" in targets:
        enrich_law_enforcement(supabase, wa_id, dry_run=args.dry_run)

    if "school-districts" in targets:
        enrich_school_districts(supabase, wa_id, dry_run=args.dry_run)

    print("\n[done]")


if __name__ == "__main__":
    main()
