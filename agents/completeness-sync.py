#!/usr/bin/env python3
"""
completeness-sync.py  (v2 — comprehensive)

Pulls authoritative data for all nine directory categories from official
Washington State sources, compares against Supabase, inserts missing records,
fills NULL fields on existing records, and prints a completeness report.

Sources
-------
hospitals        WA Dept of Health licensed facilities    data.wa.gov/resource/qxh8-f4bd.json
law_enforcement  WA CJTC / WASPC agency list              data.wa.gov/resource/yb87-7aqt.json
fire_ems         WA L&I fire dept licensing + fallbacks   data.wa.gov (discovery search)
school_districts OSPI complete district list              data.wa.gov/resource/rxjk-6ieq.json
utilities        WA UTC regulated companies               data.wa.gov/resource/bfag-c5ht.json
transit          WSDOT transit directory + seed list      wsdot.wa.gov / seed
state_agencies   WA.gov agency directory                  www.wa.gov/agency-directory
courts           WA Courts directory (institutions)       www.courts.wa.gov/court_dir/
judiciary        WA Courts directory (judges)             www.courts.wa.gov/court_dir/
colleges         College Scorecard API                    api.data.gov/ed/collegescorecard/

Prerequisites
-------------
  Run migrations/add_colleges.sql in Supabase SQL editor before first run.

Usage
-----
  python agents/completeness-sync.py
  python agents/completeness-sync.py --dry-run
  python agents/completeness-sync.py --only hospitals,colleges
  python agents/completeness-sync.py --skip-court-details   # skip per-court detail pages
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import Client, create_client

# ── Config ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
DATA_GOV_KEY = os.environ["FEC_API_KEY"]      # College Scorecard + data.gov APIs

DELAY   = 1.0   # seconds between HTTP requests (respect rate limits)
TIMEOUT = 20

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ALL_CATEGORIES = [
    "hospitals", "law_enforcement", "fire_ems", "school_districts",
    "utilities_transit", "state_agencies", "courts", "judiciary", "colleges",
]

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("completeness-sync")

# ── HTTP ──────────────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers["User-Agent"] = UA
_last_req: float = 0.0


def http_get(url: str, params: dict | None = None, *, delay: float = DELAY) -> requests.Response | None:
    global _last_req
    gap = time.time() - _last_req
    if gap < delay:
        time.sleep(delay - gap)
    try:
        r = SESSION.get(url, params=params, timeout=TIMEOUT, allow_redirects=True)
        _last_req = time.time()
        if r.ok:
            return r
        log.debug("HTTP %d for %s", r.status_code, url)
        return None
    except Exception as exc:
        _last_req = time.time()
        log.warning("GET %s → %s", url, exc)
        return None


def paginate_socrata(base_url: str, extra: dict | None = None, size: int = 1000) -> list[dict]:
    """Fetch all rows from a Socrata JSON endpoint via offset pagination."""
    params = dict(extra or {})
    params["$limit"] = size
    rows: list[dict] = []
    offset = 0
    while True:
        params["$offset"] = offset
        r = http_get(base_url, params)
        if not r:
            break
        try:
            batch = r.json()
        except Exception:
            break
        if not isinstance(batch, list):
            break
        rows.extend(batch)
        if len(batch) < size:
            break
        offset += size
    return rows


def socrata_discover(domain: str, query: str, limit: int = 5) -> list[str]:
    """Return dataset resource IDs from the Socrata discovery API matching a query."""
    r = http_get(
        "https://api.us.socrata.com/api/catalog/v1",
        {"domains": domain, "q": query, "limit": limit},
    )
    if not r:
        return []
    try:
        items = r.json().get("results", [])
        return [
            item["resource"]["id"]
            for item in items
            if item.get("resource", {}).get("id")
        ]
    except Exception:
        return []


def paginate_scorecard(params: dict) -> list[dict]:
    """Fetch all pages from the College Scorecard API."""
    rows: list[dict] = []
    page, per_page = 0, 100
    while True:
        r = http_get(
            "https://api.data.gov/ed/collegescorecard/v1/schools.json",
            {**params, "page": page, "per_page": per_page},
        )
        if not r:
            break
        try:
            body = r.json()
        except Exception:
            break
        results = body.get("results", [])
        rows.extend(results)
        total = body.get("metadata", {}).get("total", 0)
        if (page + 1) * per_page >= total or not results:
            break
        page += 1
    return rows


# ── Value helpers ─────────────────────────────────────────────────────────────

def c(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def to_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def norm(s: str | None) -> str:
    """Normalise a name for duplicate detection."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.strip().lower())


def norm_url(url: Any) -> str | None:
    s = c(url)
    if not s:
        return None
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    return s


_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}")


def first_phone(text: str) -> str | None:
    m = _PHONE_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group())
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:10]}" if len(digits) == 10 else m.group()


# ── Stats ─────────────────────────────────────────────────────────────────────

@dataclass
class Stats:
    category: str
    source_count: int = 0
    db_before: int = 0
    db_after: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0


# ── County cache ──────────────────────────────────────────────────────────────

_counties: dict[str, str] = {}   # norm(name) → uuid


def load_counties(db: Client, wa_id: str) -> None:
    rows = db.table("counties").select("id,name").eq("state_id", wa_id).execute().data or []
    for r in rows:
        _counties[norm(r["name"])] = r["id"]
    log.info("Loaded %d WA counties", len(_counties))


def county_id(raw: str | None) -> str | None:
    if not raw:
        return None
    key = norm(raw).removesuffix(" county").strip()
    return _counties.get(key)


# ── DB helpers ────────────────────────────────────────────────────────────────

def db_count(db: Client, table: str, wa_id: str) -> int:
    try:
        r = db.table(table).select("id", count="exact").eq("state_id", wa_id).execute()
        if r.count is not None:
            return r.count
        # Older supabase-py versions may not populate r.count — fall back to data length
        if r.data is not None:
            log.warning("db_count %s: r.count is None, falling back to len(r.data)=%d", table, len(r.data))
            return len(r.data)
        log.warning("db_count %s: both r.count and r.data are None", table)
        return 0
    except Exception as exc:
        log.error("db_count %s: %s", table, exc)
        return -1


def load_existing(db: Client, table: str, wa_id: str, name_col: str = "name") -> dict[str, dict]:
    """Load all existing rows as {norm(name) → row}."""
    try:
        r = db.table(table).select("*").eq("state_id", wa_id).execute()
        out: dict[str, dict] = {}
        for row in (r.data or []):
            key = norm(row.get(name_col))
            if key:
                out[key] = row
        return out
    except Exception as exc:
        log.error("load_existing %s: %s", table, exc)
        return {}


def partial_update(existing: dict, candidate: dict) -> dict:
    """Fields in candidate that are NULL (None) in existing, skipping pk/meta cols."""
    skip = {"id", "state_id", "created_at"}
    return {
        k: v
        for k, v in candidate.items()
        if k not in skip and existing.get(k) is None and v is not None
    }


def safe_insert(db: Client, table: str, row: dict, dry_run: bool) -> bool:
    if dry_run:
        return True
    try:
        db.table(table).insert(row).execute()
        return True
    except Exception as exc:
        # Build a rich error string by pulling any structured fields off the exception
        parts = [str(exc)]
        for attr in ("message", "details", "hint", "code"):
            val = getattr(exc, attr, None)
            if val and str(val) not in parts[0]:
                parts.append(f"{attr}={val}")
        log.error(
            "INSERT %s FAILED | name=%s | columns=%s | error=%s",
            table, row.get("name", "?"), list(row.keys()), " | ".join(parts),
        )
        return False


def safe_update(db: Client, table: str, row_id: str, updates: dict, dry_run: bool) -> bool:
    if dry_run or not updates:
        return bool(updates)
    try:
        db.table(table).update(updates).eq("id", row_id).execute()
        return True
    except Exception as exc:
        log.error("UPDATE %s id=%s: %s", table, row_id, exc)
        return False


def upsert_record(
    db: Client, table: str, row: dict, existing: dict[str, dict],
    name_key: str, dry_run: bool, stats: Stats,
) -> None:
    """Insert if name_key not in existing, else partial-update NULL fields."""
    if not name_key:
        stats.skipped += 1
        return
    if name_key not in existing:
        ok = safe_insert(db, table, row, dry_run)
        if ok:
            stats.inserted += 1
        else:
            stats.errors += 1
    else:
        updates = partial_update(existing[name_key], row)
        if updates:
            ok = safe_update(db, table, existing[name_key]["id"], updates, dry_run)
            if ok:
                stats.updated += 1
            else:
                stats.errors += 1
        else:
            stats.skipped += 1


# ── 1. Hospitals ──────────────────────────────────────────────────────────────
# WSHA member directory scrape + seed fallback
# Primary:  https://www.wsha.org/our-members/
# Fallback: WA_HOSPITAL_SEED (20 largest WA hospitals)

WSHA_URL = "https://www.wsha.org/our-members/"

# City → county name covering every city that hosts a WA hospital
_HOSP_CITY_COUNTY: dict[str, str] = {
    "seattle": "King",          "bellevue": "King",        "renton": "King",
    "kirkland": "King",         "auburn": "King",           "kent": "King",
    "redmond": "King",          "issaquah": "King",         "burien": "King",
    "federal way": "King",      "shoreline": "King",        "bothell": "King",
    "tacoma": "Pierce",         "puyallup": "Pierce",       "gig harbor": "Pierce",
    "lakewood": "Pierce",       "sumner": "Pierce",
    "olympia": "Thurston",      "lacey": "Thurston",        "tumwater": "Thurston",
    "spokane": "Spokane",       "spokane valley": "Spokane",
    "everett": "Snohomish",     "edmonds": "Snohomish",     "marysville": "Snohomish",
    "monroe": "Snohomish",      "lynnwood": "Snohomish",
    "bellingham": "Whatcom",    "lynden": "Whatcom",
    "vancouver": "Clark",       "camas": "Clark",           "battle ground": "Clark",
    "longview": "Cowlitz",      "kelso": "Cowlitz",
    "yakima": "Yakima",         "selah": "Yakima",          "sunnyside": "Yakima",
    "kennewick": "Benton",      "richland": "Benton",
    "pasco": "Franklin",
    "walla walla": "Walla Walla",
    "mount vernon": "Skagit",   "anacortes": "Skagit",      "sedro-woolley": "Skagit",
    "port angeles": "Clallam",  "sequim": "Clallam",
    "wenatchee": "Chelan",      "chelan": "Chelan",
    "bremerton": "Kitsap",      "silverdale": "Kitsap",     "port orchard": "Kitsap",
    "aberdeen": "Grays Harbor", "hoquiam": "Grays Harbor",
    "moses lake": "Grant",      "ephrata": "Grant",
    "ellensburg": "Kittitas",
    "pullman": "Whitman",       "colfax": "Whitman",
    "colville": "Stevens",      "chewelah": "Stevens",
    "omak": "Okanogan",         "okanogan": "Okanogan",
    "shelton": "Mason",
    "south bend": "Pacific",
    "stevenson": "Skamania",
    "newport": "Pend Oreille",
    "republic": "Ferry",
    "goldendale": "Klickitat",
}

WA_HOSPITAL_SEED: list[dict] = [
    {"name": "Harborview Medical Center",                  "city": "Seattle",    "county": "King",      "health_system": "UW Medicine",                       "beds": 413},
    {"name": "UW Medical Center - Montlake",               "city": "Seattle",    "county": "King",      "health_system": "UW Medicine",                       "beds": 450},
    {"name": "UW Medical Center - Northwest",              "city": "Seattle",    "county": "King",      "health_system": "UW Medicine",                       "beds": 281},
    {"name": "Swedish Medical Center - First Hill",        "city": "Seattle",    "county": "King",      "health_system": "Providence Swedish",                 "beds": 697},
    {"name": "Virginia Mason Medical Center",              "city": "Seattle",    "county": "King",      "health_system": "Virginia Mason Franciscan Health",   "beds": 336},
    {"name": "Seattle Children's Hospital",                "city": "Seattle",    "county": "King",      "health_system": "Seattle Children's",                 "beds": 407},
    {"name": "EvergreenHealth Medical Center",             "city": "Kirkland",   "county": "King",      "health_system": "EvergreenHealth",                    "beds": 318},
    {"name": "Overlake Medical Center",                    "city": "Bellevue",   "county": "King",      "health_system": "Overlake Medical Center",             "beds": 349},
    {"name": "Valley Medical Center",                      "city": "Renton",     "county": "King",      "health_system": "UW Medicine",                        "beds": 321},
    {"name": "MultiCare Tacoma General Hospital",          "city": "Tacoma",     "county": "Pierce",    "health_system": "MultiCare Health System",            "beds": 437},
    {"name": "St. Joseph Medical Center",                  "city": "Tacoma",     "county": "Pierce",    "health_system": "Virginia Mason Franciscan Health",   "beds": 366},
    {"name": "MultiCare Good Samaritan Hospital",          "city": "Puyallup",   "county": "Pierce",    "health_system": "MultiCare Health System",            "beds": 286},
    {"name": "Providence St. Peter Hospital",              "city": "Olympia",    "county": "Thurston",  "health_system": "Providence",                         "beds": 390},
    {"name": "Providence Sacred Heart Medical Center",     "city": "Spokane",    "county": "Spokane",   "health_system": "Providence",                         "beds": 644},
    {"name": "MultiCare Deaconess Hospital",               "city": "Spokane",    "county": "Spokane",   "health_system": "MultiCare Health System",            "beds": 352},
    {"name": "Providence Regional Medical Center Everett", "city": "Everett",    "county": "Snohomish", "health_system": "Providence",                         "beds": 574},
    {"name": "PeaceHealth St. Joseph Medical Center",      "city": "Bellingham", "county": "Whatcom",   "health_system": "PeaceHealth",                        "beds": 253},
    {"name": "PeaceHealth Southwest Medical Center",       "city": "Vancouver",  "county": "Clark",     "health_system": "PeaceHealth",                        "beds": 450},
    {"name": "Kadlec Regional Medical Center",             "city": "Richland",   "county": "Benton",    "health_system": "Lifepoint Health",                   "beds": 279},
    {"name": "Virginia Mason Memorial",                    "city": "Yakima",     "county": "Yakima",    "health_system": "Virginia Mason Franciscan Health",   "beds": 226},
]

_HOSP_KEYWORDS = frozenset([
    "hospital", "medical center", "health center", "healthcare", "health system",
    "regional medical", "children's", "memorial", "providence", "swedish",
    "evergreen", "multicare", "peacehealth", "virginia mason", "st.", "saint",
])


def _looks_like_hospital(text: str) -> bool:
    lt = text.lower()
    return any(kw in lt for kw in _HOSP_KEYWORDS)


def _extract_city(text: str) -> str | None:
    for pattern in (
        r"[-–,|]\s*([A-Za-z][A-Za-z\s]+?),?\s*WA\b",
        r"\b([A-Za-z][A-Za-z\s]+),\s*Washington\b",
    ):
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1).strip()
    return None


def _city_county(city: str | None) -> str | None:
    if not city:
        return None
    return _HOSP_CITY_COUNTY.get(city.strip().lower())


def _scrape_wsha(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()

    # Pass 1: structured member cards / list items with headings
    for el in soup.find_all(["article", "div", "li"],
                             class_=re.compile(r"member|hospital|facility|card|entry|item", re.I)):
        heading = el.find(["h1", "h2", "h3", "h4", "a"])
        name = c(heading.get_text()) if heading else None
        if not name or not _looks_like_hospital(name):
            continue
        key = norm(name)
        if key in seen:
            continue
        seen.add(key)
        block_text = el.get_text(" ", strip=True)
        city = _extract_city(block_text)
        href = heading.get("href") if heading and heading.name == "a" else None
        out.append({
            "name": name,
            "_county": _city_county(city),
            "health_system": None, "beds": None, "ownership_type": None,
            "trauma_level": None, "ceo": None, "phone": None,
            "website": norm_url(href) if href and href.startswith("http") else None,
        })

    # Pass 2: any link whose text reads like a hospital name
    if not out:
        for a in soup.find_all("a", href=True):
            name = c(a.get_text())
            if not name or len(name) < 8 or not _looks_like_hospital(name):
                continue
            key = norm(name)
            if key in seen:
                continue
            seen.add(key)
            parent_text = c(a.parent.get_text(" ", strip=True)) if a.parent else ""
            city = _extract_city(parent_text or "")
            out.append({
                "name": name,
                "_county": _city_county(city),
                "health_system": None, "beds": None, "ownership_type": None,
                "trauma_level": None, "ceo": None, "phone": None,
                "website": norm_url(a["href"]) if a["href"].startswith("http") else None,
            })

    return out


def fetch_hospitals() -> list[dict]:
    log.info("[hospitals] Fetching WSHA member directory %s", WSHA_URL)
    r = http_get(WSHA_URL)
    if r:
        results = _scrape_wsha(r.text)
        log.info("[hospitals] WSHA parse returned %d hospitals", len(results))
        if results:
            log.info("[hospitals] Fields extracted: name, county (via city lookup), website")
            return results
        log.warning("[hospitals] WSHA page fetched but 0 hospitals parsed — falling back to seed")
    else:
        log.warning("[hospitals] WSHA page fetch failed — falling back to seed")

    log.info("[hospitals] Using WA_HOSPITAL_SEED fallback (%d hospitals)", len(WA_HOSPITAL_SEED))
    return [
        {
            "name": s["name"],
            "_county": s["county"],
            "health_system": s.get("health_system"),
            "beds": s.get("beds"),
            "ownership_type": None, "trauma_level": None,
            "ceo": None, "phone": None, "website": None,
        }
        for s in WA_HOSPITAL_SEED
    ]


def sync_hospitals(db: Client, wa_id: str, dry_run: bool, stats: Stats) -> None:
    source = fetch_hospitals()
    stats.source_count = len(source)
    stats.db_before = db_count(db, "hospitals", wa_id)
    log.info("[hospitals] DB before: %d | Source: %d", stats.db_before, stats.source_count)
    existing = load_existing(db, "hospitals", wa_id)
    for rec in source:
        row = {
            "state_id": wa_id, "county_id": county_id(rec.pop("_county", None)),
            "name": rec["name"], "ownership_type": rec.get("ownership_type"),
            "beds": rec.get("beds"), "trauma_level": rec.get("trauma_level"),
            "health_system": rec.get("health_system"), "ceo": rec.get("ceo"),
            "phone": rec.get("phone"), "website": rec.get("website"),
        }
        upsert_record(db, "hospitals", row, existing, norm(rec["name"]), dry_run, stats)
    stats.db_after = db_count(db, "hospitals", wa_id) if not dry_run else stats.db_before + stats.inserted


# ── 2. Law Enforcement ────────────────────────────────────────────────────────
# WSP / CJTC agency list + WASPC member directory fallback + seed list
# Primary:  https://data.wa.gov/resource/yb87-7aqt.json

LE_SOCRATA_SOURCES = [
    ("https://data.wa.gov/resource/yb87-7aqt.json", {}, "WSP/CJTC yb87-7aqt"),
]

WASPC_URL = "https://www.waspc.org/member-agencies"

WA_LE_SEED: list[dict] = [
    {"name": "Washington State Patrol",              "agency_type": "State",   "county": None,          "website": "https://www.wsp.wa.gov"},
    {"name": "King County Sheriff's Office",         "agency_type": "County",  "county": "King",        "website": "https://www.kingcounty.gov/sheriff"},
    {"name": "Seattle Police Department",            "agency_type": "City",    "county": "King",        "website": "https://www.seattle.gov/police"},
    {"name": "Spokane Police Department",            "agency_type": "City",    "county": "Spokane",     "website": "https://www.spokanecity.org/police"},
    {"name": "Spokane County Sheriff",               "agency_type": "County",  "county": "Spokane",     "website": "https://www.spokanesheriff.org"},
    {"name": "Pierce County Sheriff",                "agency_type": "County",  "county": "Pierce",      "website": "https://www.piercecountywa.gov/sheriff"},
    {"name": "Tacoma Police Department",             "agency_type": "City",    "county": "Pierce",      "website": "https://www.cityoftacoma.org/police"},
    {"name": "Snohomish County Sheriff",             "agency_type": "County",  "county": "Snohomish",   "website": "https://www.snohomishcountywa.gov/sheriff"},
    {"name": "Everett Police Department",            "agency_type": "City",    "county": "Snohomish",   "website": "https://www.everettwa.gov/police"},
    {"name": "Clark County Sheriff",                 "agency_type": "County",  "county": "Clark",       "website": "https://www.clark.wa.gov/sheriff"},
    {"name": "Vancouver Police Department",          "agency_type": "City",    "county": "Clark",       "website": "https://www.cityofvancouver.us/police"},
    {"name": "Thurston County Sheriff",              "agency_type": "County",  "county": "Thurston",    "website": "https://www.co.thurston.wa.us/sheriff"},
    {"name": "Whatcom County Sheriff",               "agency_type": "County",  "county": "Whatcom",     "website": "https://www.whatcomcounty.us/sheriff"},
    {"name": "Bellingham Police Department",         "agency_type": "City",    "county": "Whatcom",     "website": "https://www.cob.org/police"},
    {"name": "Kitsap County Sheriff",                "agency_type": "County",  "county": "Kitsap",      "website": "https://www.kitsapgov.com/sheriff"},
    {"name": "Yakima Police Department",             "agency_type": "City",    "county": "Yakima",      "website": "https://www.yakimawa.gov/police"},
    {"name": "Yakima County Sheriff",                "agency_type": "County",  "county": "Yakima",      "website": "https://www.yakimacounty.us/sheriff"},
    {"name": "Bellevue Police Department",           "agency_type": "City",    "county": "King",        "website": "https://www.bellevuewa.gov/police"},
    {"name": "Renton Police Department",             "agency_type": "City",    "county": "King",        "website": "https://www.rentonwa.gov/police"},
    {"name": "Kent Police Department",               "agency_type": "City",    "county": "King",        "website": "https://www.kentwa.gov/police"},
    {"name": "Federal Way Police Department",        "agency_type": "City",    "county": "King",        "website": "https://www.cityoffederalway.com/police"},
    {"name": "Kennewick Police Department",          "agency_type": "City",    "county": "Benton",      "website": "https://www.kennewickwa.gov/police"},
    {"name": "Richland Police Department",           "agency_type": "City",    "county": "Benton",      "website": "https://www.ci.richland.wa.us/police"},
    {"name": "Pasco Police Department",              "agency_type": "City",    "county": "Franklin",    "website": "https://www.pasco-wa.gov/police"},
    {"name": "Benton County Sheriff",                "agency_type": "County",  "county": "Benton",      "website": "https://www.co.benton.wa.us/sheriff"},
    {"name": "Skagit County Sheriff",                "agency_type": "County",  "county": "Skagit",      "website": "https://www.skagitcounty.net/sheriff"},
    {"name": "Chelan County Sheriff",                "agency_type": "County",  "county": "Chelan",      "website": "https://www.co.chelan.wa.us/sheriff"},
    {"name": "Grant County Sheriff",                 "agency_type": "County",  "county": "Grant",       "website": "https://www.grantcountywa.gov/sheriff"},
    {"name": "Island County Sheriff",                "agency_type": "County",  "county": "Island",      "website": "https://www.islandcountywa.gov/sheriff"},
    {"name": "Jefferson County Sheriff",             "agency_type": "County",  "county": "Jefferson",   "website": "https://www.co.jefferson.wa.us/sheriff"},
    {"name": "Lewis County Sheriff",                 "agency_type": "County",  "county": "Lewis",       "website": "https://www.lewiscountywa.gov/sheriff"},
    {"name": "Mason County Sheriff",                 "agency_type": "County",  "county": "Mason",       "website": "https://www.co.mason.wa.us/sheriff"},
    {"name": "Okanogan County Sheriff",              "agency_type": "County",  "county": "Okanogan",    "website": "https://www.okanogancounty.org/sheriff"},
    {"name": "San Juan County Sheriff",              "agency_type": "County",  "county": "San Juan",    "website": "https://www.sanjuanco.com/sheriff"},
    {"name": "Stevens County Sheriff",               "agency_type": "County",  "county": "Stevens",     "website": "https://www.stevenscountywa.gov/sheriff"},
    {"name": "Walla Walla County Sheriff",           "agency_type": "County",  "county": "Walla Walla", "website": "https://www.co.walla-walla.wa.us/sheriff"},
    {"name": "Pacific County Sheriff",               "agency_type": "County",  "county": "Pacific",     "website": None},
]


def _parse_le_row(row: dict) -> dict | None:
    name = c(
        row.get("agency_name") or row.get("agencyname") or row.get("name")
        or row.get("department_name") or row.get("org_name") or row.get("organization")
    )
    if not name:
        return None
    county_raw = c(
        row.get("county") or row.get("county_name") or row.get("countyname")
        or row.get("county_description")
    )
    return {
        "name": name,
        "_county": county_raw,
        "agency_type": c(
            row.get("agency_type") or row.get("agencytype") or row.get("type")
            or row.get("org_type") or row.get("organization_type")
        ),
        "jurisdiction": c(
            row.get("jurisdiction") or row.get("city") or row.get("municipality") or county_raw
        ),
        "chief_name": c(
            row.get("chief_name") or row.get("chief") or row.get("sheriff")
            or row.get("director") or row.get("chiefname") or row.get("contact_name")
        ),
        "sworn_officers": to_int(
            row.get("sworn_officers") or row.get("authorized_strength")
            or row.get("total_sworn") or row.get("sworn") or row.get("officer_count")
        ),
        "headquarters": c(
            row.get("headquarters") or row.get("address") or row.get("street_address")
            or row.get("physical_address") or row.get("location")
        ),
        "phone": c(row.get("phone") or row.get("phone_number") or row.get("telephone")),
        "website": norm_url(row.get("website") or row.get("url") or row.get("web_address")),
    }


def _scrape_waspc(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        name = c(a.get_text())
        if not name or len(name) < 5:
            continue
        lname = name.lower()
        if not any(kw in lname for kw in ("police", "sheriff", "patrol", "enforcement", "marshal")):
            continue
        key = norm(name)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": name, "_county": None,
            "agency_type": "County" if "sheriff" in lname else "City",
            "jurisdiction": None, "chief_name": None, "sworn_officers": None,
            "headquarters": None, "phone": None,
            "website": norm_url(a["href"]) if a["href"].startswith("http") else None,
        })
    return out


def fetch_law_enforcement() -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()

    def add(recs: list[dict], label: str) -> None:
        added = 0
        for r in recs:
            key = norm(r.get("name", ""))
            if key and key not in seen:
                seen.add(key)
                results.append(r)
                added += 1
        if added:
            log.info("[law_enforcement] +%d from %s", added, label)

    # Primary: Socrata datasets
    for url, extra, label in LE_SOCRATA_SOURCES:
        log.info("[law_enforcement] Trying %s — %s", label, url)
        raw = paginate_socrata(url, extra)
        if raw:
            log.info("[law_enforcement] %d rows | columns: %s", len(raw), sorted(raw[0].keys()))
            parsed = [r for r in (_parse_le_row(row) for row in raw) if r]
            add(parsed, label)
        else:
            log.info("[law_enforcement] 0 rows from %s", label)

    # Fallback: WASPC member directory (HTML scrape)
    if not results:
        log.info("[law_enforcement] Trying WASPC directory %s", WASPC_URL)
        r = http_get(WASPC_URL)
        if r:
            add(_scrape_waspc(r.text), "WASPC scrape")

    # Last resort: seed list of major known agencies
    if not results:
        log.info("[law_enforcement] Using seed list as last resort")
        seed_recs = [
            {
                "name": s["name"], "_county": s.get("county"),
                "agency_type": s.get("agency_type"), "jurisdiction": None,
                "chief_name": None, "sworn_officers": None,
                "headquarters": None, "phone": None, "website": s.get("website"),
            }
            for s in WA_LE_SEED
        ]
        add(seed_recs, "seed list")

    log.info("[law_enforcement] Total unique: %d", len(results))
    return results


def sync_law_enforcement(db: Client, wa_id: str, dry_run: bool, stats: Stats) -> None:
    source = fetch_law_enforcement()
    stats.source_count = len(source)
    stats.db_before = db_count(db, "law_enforcement_agencies", wa_id)
    log.info("[law_enforcement] DB before: %d | Source: %d", stats.db_before, stats.source_count)
    existing = load_existing(db, "law_enforcement_agencies", wa_id)
    for rec in source:
        row = {
            "state_id": wa_id, "county_id": county_id(rec.pop("_county", None)),
            "name": rec["name"], "agency_type": rec.get("agency_type"),
            "jurisdiction": rec.get("jurisdiction"), "chief_name": rec.get("chief_name"),
            "sworn_officers": rec.get("sworn_officers"), "headquarters": rec.get("headquarters"),
            "phone": rec.get("phone"), "website": rec.get("website"),
        }
        upsert_record(db, "law_enforcement_agencies", row, existing, norm(rec["name"]), dry_run, stats)
    stats.db_after = db_count(db, "law_enforcement_agencies", wa_id) if not dry_run else stats.db_before + stats.inserted


# ── 3. Fire & EMS ─────────────────────────────────────────────────────────────
# WA State Fire Marshal certified agency list + L&I/Socrata fallbacks + HTML scrape
# Primary:  https://data.wa.gov/resource/iqve-wu4d.json  (WA Fire Marshal)

FIRE_PRIMARY = "https://data.wa.gov/resource/iqve-wu4d.json"

FIRE_FALLBACK_URLS = [
    "https://data.wa.gov/resource/qbhw-t5ip.json",
    "https://data.wa.gov/resource/tnhz-aeqc.json",
    "https://data.wa.gov/resource/4rp5-8thz.json",
    "https://data.wa.gov/resource/rsx5-d9ea.json",
]

FIRE_HTML_URLS = [
    "https://fortress.wa.gov/rco/ratf/firestation/",
    "https://www.lni.wa.gov/licensing-permits/fire-departments/",
]


def _rows_to_fire(rows: list[dict], label: str = "") -> list[dict]:
    if not rows:
        return []
    log.info("[fire_ems] %s columns: %s", label, sorted(rows[0].keys()))
    out = []
    for row in rows:
        name = c(
            row.get("agency_name") or row.get("department_name") or row.get("name")
            or row.get("station_name") or row.get("fire_department")
            or row.get("org_name") or row.get("organization_name")
            or row.get("deptname") or row.get("dept_name")
        )
        if not name:
            continue
        county_raw = c(
            row.get("county") or row.get("county_name") or row.get("county_description")
        )
        out.append({
            "name":         name,
            "_county":      county_raw,
            "agency_type":  c(
                row.get("agency_type") or row.get("type") or row.get("department_type")
                or row.get("org_type") or row.get("cert_type") or row.get("certification_type")
            ),
            "jurisdiction": c(
                row.get("jurisdiction") or row.get("city") or row.get("municipality")
                or row.get("city_name") or county_raw
            ),
            "fire_chief":   c(
                row.get("fire_chief") or row.get("chief") or row.get("director")
                or row.get("chief_name") or row.get("contact_name")
            ),
            "stations":     to_int(
                row.get("stations") or row.get("num_stations") or row.get("number_of_stations")
            ),
            "personnel":    to_int(
                row.get("personnel") or row.get("total_personnel") or row.get("staff")
                or row.get("members") or row.get("volunteer_count") or row.get("career_count")
            ),
            "headquarters": c(
                row.get("headquarters") or row.get("address") or row.get("station_address")
                or row.get("physical_address") or row.get("mailing_address")
            ),
            "service_type": c(
                row.get("service_type") or row.get("services") or row.get("type_of_service")
                or row.get("fire_district_type") or row.get("district_type")
            ),
            "phone":        c(
                row.get("phone") or row.get("telephone") or row.get("phone_number")
                or row.get("contact_phone")
            ),
            "website":      norm_url(row.get("website") or row.get("url") or row.get("web_address")),
        })
    return out


def _scrape_fire_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
        if not any(kw in " ".join(headers) for kw in ("agency", "name", "department", "station", "fire")):
            continue

        def col(cells: list, *keys: str) -> str | None:
            for k in keys:
                for i, h in enumerate(headers):
                    if k in h and i < len(cells):
                        return c(cells[i].get_text(strip=True))
            return None

        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            name = col(cells, "agency", "department", "station", "name") or (c(cells[0].get_text()) if cells else None)
            if not name or norm(name) in ("", "n/a"):
                continue
            county_raw = col(cells, "county")
            out.append({
                "name": name, "_county": county_raw,
                "agency_type":  col(cells, "type", "class"),
                "jurisdiction": col(cells, "city", "jurisdiction") or county_raw,
                "fire_chief":   col(cells, "chief", "director"),
                "stations":     to_int(col(cells, "station")),
                "personnel":    to_int(col(cells, "personnel", "staff")),
                "service_type": col(cells, "service", "ems"),
                "phone":        col(cells, "phone", "telephone"),
                "website":      norm_url(col(cells, "website", "url")),
                "headquarters": col(cells, "address", "headquarters"),
            })
    return out


def fetch_fire_ems() -> list[dict]:
    # Primary: WA State Fire Marshal certified agency list
    log.info("[fire_ems] Trying WA Fire Marshal dataset %s", FIRE_PRIMARY)
    raw = paginate_socrata(FIRE_PRIMARY)
    if raw:
        results = _rows_to_fire(raw, "Fire Marshal iqve-wu4d")
        if results:
            log.info("[fire_ems] %d records from Fire Marshal dataset", len(results))
            return results
        log.warning("[fire_ems] Fire Marshal dataset returned %d rows but 0 parsed — see columns above", len(raw))

    # Fallback: known L&I / other fixed Socrata datasets
    log.info("[fire_ems] Trying fixed Socrata fallback URLs")
    for url in FIRE_FALLBACK_URLS:
        log.info("[fire_ems] Trying %s", url)
        raw = paginate_socrata(url)
        if not raw:
            continue
        results = _rows_to_fire(raw, url)
        if results:
            log.info("[fire_ems] %d records from %s", len(results), url)
            return results

    # Fallback: Socrata catalog discovery
    log.info("[fire_ems] Discovering fire department datasets on data.wa.gov ...")
    discovered = socrata_discover("data.wa.gov", "fire department")
    seen_urls = {FIRE_PRIMARY} | set(FIRE_FALLBACK_URLS)
    for rid in discovered:
        url = f"https://data.wa.gov/resource/{rid}.json"
        if url in seen_urls:
            continue
        log.info("[fire_ems] Trying discovered %s", url)
        raw = paginate_socrata(url)
        if not raw:
            continue
        results = _rows_to_fire(raw, url)
        if results:
            log.info("[fire_ems] %d records from %s", len(results), url)
            return results

    # Final fallback: HTML sources
    log.info("[fire_ems] Trying HTML sources")
    for html_url in FIRE_HTML_URLS:
        r = http_get(html_url)
        if not r or not r.text.strip():
            continue
        results = _scrape_fire_html(r.text)
        if results:
            log.info("[fire_ems] %d records scraped from %s", len(results), html_url)
            return results

    log.warning("[fire_ems] No data retrieved from any source")
    return []


def sync_fire_ems(db: Client, wa_id: str, dry_run: bool, stats: Stats) -> None:
    source = fetch_fire_ems()
    stats.source_count = len(source)
    stats.db_before = db_count(db, "fire_ems_agencies", wa_id)
    log.info("[fire_ems] DB before: %d | Source: %d", stats.db_before, stats.source_count)
    existing = load_existing(db, "fire_ems_agencies", wa_id)
    for rec in source:
        row = {
            "state_id": wa_id, "county_id": county_id(rec.pop("_county", None)),
            "name": rec["name"], "agency_type": rec.get("agency_type"),
            "jurisdiction": rec.get("jurisdiction"), "fire_chief": rec.get("fire_chief"),
            "stations": rec.get("stations"), "personnel": rec.get("personnel"),
            "headquarters": rec.get("headquarters"), "service_type": rec.get("service_type"),
            "phone": rec.get("phone"), "website": rec.get("website"),
        }
        upsert_record(db, "fire_ems_agencies", row, existing, norm(rec["name"]), dry_run, stats)
    stats.db_after = db_count(db, "fire_ems_agencies", wa_id) if not dry_run else stats.db_before + stats.inserted


# ── 4. School Districts ───────────────────────────────────────────────────────
# OSPI enrollment + contacts — target 295 WA districts

OSPI_SOURCES: list[tuple[str, dict]] = [
    ("https://data.wa.gov/resource/rxjk-6ieq.json", {"organizationlevel": "District"}),
    ("https://data.wa.gov/resource/e3va-vv7m.json", {}),
    ("https://data.wa.gov/resource/7s2n-cxwk.json", {}),
    ("https://data.wa.gov/resource/6y7d-9bwt.json", {}),
    ("https://data.wa.gov/resource/yx2k-qxgm.json", {}),   # OSPI district contacts
]
EXPECTED_DISTRICTS = 295


def _parse_ospi(row: dict) -> dict | None:
    name = c(
        row.get("districtname") or row.get("district_name") or row.get("organizationname")
        or row.get("schooldistrictname") or row.get("name")
    )
    if not name:
        return None
    return {
        "name":             name,
        "_county":          c(row.get("county") or row.get("county_name") or row.get("countynamecode")),
        "enrollment":       to_int(row.get("totalenrollment") or row.get("enrollment") or row.get("headcount") or row.get("total_enrollment")),
        "superintendent":   c(row.get("superintendent") or row.get("superintendent_name") or row.get("superint")),
        "phone":            c(row.get("phone") or row.get("telephone") or row.get("districtphone")),
        "official_website": norm_url(row.get("website") or row.get("url") or row.get("districtwebsite")),
    }


def fetch_school_districts() -> list[dict]:
    seen: set[str] = set()
    results: list[dict] = []

    for url, extra in OSPI_SOURCES:
        log.info("[school_districts] Trying %s", url)
        raw = paginate_socrata(url, extra)
        if not raw:
            continue
        log.info("[school_districts] %d rows, cols: %s", len(raw), sorted(raw[0].keys())[:12])
        added = 0
        for row in raw:
            parsed = _parse_ospi(row)
            if not parsed:
                continue
            key = norm(parsed["name"])
            if key and key not in seen:
                seen.add(key)
                results.append(parsed)
                added += 1
        log.info("[school_districts] +%d new unique districts", added)
        if len(results) >= EXPECTED_DISTRICTS:
            break

    log.info("[school_districts] Total from source: %d (expected %d)", len(results), EXPECTED_DISTRICTS)
    if len(results) < EXPECTED_DISTRICTS:
        log.warning("[school_districts] Below expected count — some districts may be missing")
    return results


def sync_school_districts(db: Client, wa_id: str, dry_run: bool, stats: Stats) -> None:
    source = fetch_school_districts()
    stats.source_count = len(source)
    stats.db_before = db_count(db, "school_districts", wa_id)
    log.info("[school_districts] DB before: %d | Source: %d", stats.db_before, stats.source_count)
    existing = load_existing(db, "school_districts", wa_id)
    for rec in source:
        row = {
            "state_id": wa_id, "county_id": county_id(rec.pop("_county", None)),
            "name": rec["name"], "enrollment": rec.get("enrollment"),
            "superintendent": rec.get("superintendent"), "phone": rec.get("phone"),
            "official_website": rec.get("official_website"),
        }
        upsert_record(db, "school_districts", row, existing, norm(rec["name"]), dry_run, stats)
    stats.db_after = db_count(db, "school_districts", wa_id) if not dry_run else stats.db_before + stats.inserted


# ── 5. Utilities & Transit ────────────────────────────────────────────────────
# WA UTC regulated companies + WSDOT transit agencies + seed list

UTC_URL     = "https://data.wa.gov/resource/bfag-c5ht.json"
WSDOT_URL   = "https://wsdot.wa.gov/travel/public-transportation/public-transit-systems"

# Authoritative seed of known WA public transit agencies (ensures completeness
# even if scraping yields nothing). All data publicly available.
WA_TRANSIT_SEED: list[dict] = [
    {"name": "King County Metro Transit",              "county": "King",         "service_type": "Bus",          "website": "https://kingcounty.gov/metro"},
    {"name": "Sound Transit",                          "county": "King",         "service_type": "Rail/Bus",     "website": "https://www.soundtransit.org"},
    {"name": "Pierce Transit",                         "county": "Pierce",       "service_type": "Bus",          "website": "https://www.piercetransit.org"},
    {"name": "Community Transit",                      "county": "Snohomish",    "service_type": "Bus",          "website": "https://www.communitytransit.org"},
    {"name": "Everett Transit",                        "county": "Snohomish",    "service_type": "Bus",          "website": "https://www.everetttransit.org"},
    {"name": "Spokane Transit Authority",              "county": "Spokane",      "service_type": "Bus",          "website": "https://www.spokanetransit.com"},
    {"name": "Intercity Transit",                      "county": "Thurston",     "service_type": "Bus",          "website": "https://www.intercitytransit.com"},
    {"name": "Ben Franklin Transit",                   "county": "Benton",       "service_type": "Bus",          "website": "https://www.bft.org"},
    {"name": "Valley Transit",                         "county": "Walla Walla",  "service_type": "Bus",          "website": "https://www.valleytransit.com"},
    {"name": "Link Transit",                           "county": "Chelan",       "service_type": "Bus",          "website": "https://www.linktransit.com"},
    {"name": "Whatcom Transportation Authority",       "county": "Whatcom",      "service_type": "Bus",          "website": "https://www.ridewta.com"},
    {"name": "Kitsap Transit",                         "county": "Kitsap",       "service_type": "Bus/Ferry",    "website": "https://www.kitsaptransit.com"},
    {"name": "Skagit Transit",                         "county": "Skagit",       "service_type": "Bus",          "website": "https://www.skagittransit.org"},
    {"name": "Island Transit",                         "county": "Island",       "service_type": "Bus",          "website": "https://www.islandtransit.org"},
    {"name": "Mason Transit Authority",                "county": "Mason",        "service_type": "Bus",          "website": "https://www.masontransit.org"},
    {"name": "Pacific Transit System",                 "county": "Pacific",      "service_type": "Bus",          "website": "https://www.pacifictransit.org"},
    {"name": "Clallam Transit System",                 "county": "Clallam",      "service_type": "Bus",          "website": "https://www.clallamtransit.com"},
    {"name": "Jefferson Transit",                      "county": "Jefferson",    "service_type": "Bus",          "website": "https://jeffersontransit.com"},
    {"name": "Grays Harbor Transit",                   "county": "Grays Harbor", "service_type": "Bus",          "website": "https://www.ghtransit.com"},
    {"name": "C-TRAN",                                 "county": "Clark",        "service_type": "Bus",          "website": "https://www.c-tran.com"},
    {"name": "RiverCities Transit",                    "county": "Cowlitz",      "service_type": "Bus",          "website": "https://www.rivercities.us"},
    {"name": "Grant Transit Authority",                "county": "Grant",        "service_type": "Bus",          "website": "https://www.granttransit.com"},
    {"name": "Big Bend Transit",                       "county": "Adams",        "service_type": "Bus",          "website": "https://www.bigbendtransit.org"},
    {"name": "People for People",                      "county": "Yakima",       "service_type": "Bus",          "website": "https://www.pfp.org"},
    {"name": "Yakima Transit",                         "county": "Yakima",       "service_type": "Bus",          "website": "https://www.yakimawa.gov/transit"},
    {"name": "Okanogan County Transportation",         "county": "Okanogan",     "service_type": "Bus",          "website": "https://www.okanoganct.org"},
    {"name": "Asotin County Public Transit",           "county": "Asotin",       "service_type": "Bus",          "website": None},
    {"name": "Columbia County Public Transit",         "county": "Columbia",     "service_type": "Bus",          "website": None},
    {"name": "Ferry County Transportation",            "county": "Ferry",        "service_type": "Bus",          "website": None},
    {"name": "Garfield County Transportation",         "county": "Garfield",     "service_type": "Bus",          "website": None},
    {"name": "Wahkiakum On Demand",                    "county": "Wahkiakum",    "service_type": "On-demand",    "website": None},
    {"name": "Skamania County Public Transportation",  "county": "Skamania",     "service_type": "Bus",          "website": None},
    {"name": "Whitman County Transportation",          "county": "Whitman",      "service_type": "Bus",          "website": None},
    {"name": "Douglas County Transportation",          "county": "Douglas",      "service_type": "Bus",          "website": None},
]


def _rows_to_utility(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    log.info("[utilities] Columns: %s", sorted(rows[0].keys())[:15])
    out: list[dict] = []
    for row in rows:
        name = c(row.get("company_name") or row.get("utility_name") or row.get("name"))
        if not name:
            continue
        county_raw = c(row.get("county") or row.get("county_name") or row.get("counties"))
        svc = c(row.get("company_type") or row.get("utility_type") or row.get("service_type") or row.get("type"))
        # Map UTC service types to a readable category
        cat = "Transit" if svc and "transit" in svc.lower() else "Utility"
        out.append({
            "name":            name,
            "_county":         county_raw,
            "category":        cat,
            "service_type":    svc,
            "county_region":   c(row.get("county_region") or row.get("region") or county_raw),
            "customers_riders":c(row.get("customers") or row.get("customers_served") or row.get("service_area_population")),
            "ceo":             c(row.get("ceo") or row.get("president") or row.get("director") or row.get("general_manager")),
            "phone":           c(row.get("phone") or row.get("telephone")),
            "website":         norm_url(row.get("website") or row.get("url")),
            "governing_board": c(row.get("governing_board") or row.get("board")),
        })
    return out


def _scrape_wsdot_transit(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        name = c(a.get_text())
        if not name or len(name) < 5:
            continue
        lname = name.lower()
        if not any(kw in lname for kw in ("transit", "transport", "bus", "metro", "rail", "ferry")):
            continue
        key = norm(name)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": name, "_county": None, "category": "Transit",
            "service_type": "Bus", "county_region": None, "customers_riders": None,
            "ceo": None, "phone": None, "website": norm_url(a["href"]) if a["href"].startswith("http") else None,
            "governing_board": None,
        })
    return out


def fetch_utilities_transit() -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()

    def add(recs: list[dict], label: str) -> None:
        added = 0
        for r in recs:
            key = norm(r.get("name", ""))
            if key and key not in seen:
                seen.add(key)
                results.append(r)
                added += 1
        if added:
            log.info("[utilities_transit] +%d from %s", added, label)

    # UTC regulated utilities
    log.info("[utilities_transit] Fetching WA UTC dataset %s", UTC_URL)
    utc_rows = paginate_socrata(UTC_URL)
    add(_rows_to_utility(utc_rows), "UTC Socrata")

    # WSDOT transit directory
    log.info("[utilities_transit] Fetching WSDOT transit directory")
    r = http_get(WSDOT_URL)
    if r:
        add(_scrape_wsdot_transit(r.text), "WSDOT scrape")

    # Seed list ensures all known public transit agencies are present
    seed_recs = [
        {
            "name": s["name"], "_county": s["county"], "category": "Transit",
            "service_type": s["service_type"], "county_region": s["county"],
            "customers_riders": None, "ceo": None, "phone": None,
            "website": s["website"], "governing_board": None,
        }
        for s in WA_TRANSIT_SEED
    ]
    add(seed_recs, "transit seed list")

    log.info("[utilities_transit] Total unique: %d", len(results))
    return results


def sync_utilities_transit(db: Client, wa_id: str, dry_run: bool, stats: Stats) -> None:
    source = fetch_utilities_transit()
    stats.source_count = len(source)
    stats.db_before = db_count(db, "utilities_transit", wa_id)
    log.info("[utilities_transit] DB before: %d | Source: %d", stats.db_before, stats.source_count)
    existing = load_existing(db, "utilities_transit", wa_id)
    # Log the exact column set this sync will attempt so mismatches are visible up front
    _sample_cols = [
        "state_id", "county_id", "name", "category", "service_type",
        "county_region", "customers_riders", "ceo", "phone", "website", "governing_board",
    ]
    log.info("[utilities_transit] Columns this sync inserts: %s", _sample_cols)
    for rec in source:
        row = {
            "state_id": wa_id, "county_id": county_id(rec.pop("_county", None)),
            "name": rec["name"], "category": rec.get("category"),
            "service_type": rec.get("service_type"), "county_region": rec.get("county_region"),
            "customers_riders": rec.get("customers_riders"), "ceo": rec.get("ceo"),
            "phone": rec.get("phone"), "website": rec.get("website"),
            "governing_board": rec.get("governing_board"),
        }
        upsert_record(db, "utilities_transit", row, existing, norm(rec["name"]), dry_run, stats)
    stats.db_after = db_count(db, "utilities_transit", wa_id) if not dry_run else stats.db_before + stats.inserted


# ── 6. State Agencies ─────────────────────────────────────────────────────────
# WA.gov agency directory — target 190+ agencies

AGENCY_DIR_URL = "https://www.wa.gov/agency-directory"
AGENCY_INDEX_URLS = [
    "https://www.wa.gov/agency-directory",
    "https://www.wa.gov/government/agencies-boards-and-commissions",
    "https://access.wa.gov/agency",
]

AGENCY_KEYWORDS = {
    "washington", "state", "department", "dept", "office", "board", "commission",
    "authority", "bureau", "division", "council", "agency", "institute", "center",
    "program", "administration", "service", "patrol", "treasury",
}

AGENCY_CATEGORIES = [
    ("Licensing",       ["licensing", "licenses", "professional"]),
    ("Transportation",  ["transportation", "transit", "highway", "ferry", "wsdot"]),
    ("Health",          ["health", "medical", "mental health", "aging", "hca", "doh"]),
    ("Education",       ["education", "schools", "college", "university", "ospi"]),
    ("Environment",     ["ecology", "environment", "natural resources", "fish", "wildlife", "parks"]),
    ("Public Safety",   ["corrections", "patrol", "emergency", "fire", "military", "wsp"]),
    ("Finance",         ["revenue", "financial", "treasury", "budget", "finance", "ofm"]),
    ("Labor",           ["labor", "employment", "workforce", "industry", "lni"]),
    ("Agriculture",     ["agriculture", "food", "farm"]),
    ("Social Services", ["social", "children", "family", "veterans", "disability", "dshs"]),
    ("Commerce",        ["commerce", "economic", "business", "tourism"]),
    ("Utilities",       ["utilities", "energy", "power", "water", "utc"]),
    ("Courts & Legal",  ["courts", "judicial", "attorney", "law", "legal"]),
]


def _classify_agency(name: str) -> str:
    lower = name.lower()
    for cat, keywords in AGENCY_CATEGORIES:
        if any(kw in lower for kw in keywords):
            return cat
    return "General Government"


def _abbr(name: str) -> str | None:
    m = re.search(r"\(([A-Z]{2,8})\)", name)
    return m.group(1) if m else None


def _is_agency_link(name: str) -> bool:
    if not name or len(name) < 6:
        return False
    words = set(name.lower().split())
    return bool(words & AGENCY_KEYWORDS)


def fetch_state_agencies() -> list[dict]:
    seen: set[str] = set()
    results: list[dict] = []

    for url in AGENCY_INDEX_URLS:
        log.info("[state_agencies] Trying %s", url)
        r = http_get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        added = 0
        # First pass: any <a> whose text looks like an agency name
        for a in soup.find_all("a", href=True):
            name = c(a.get_text())
            if not _is_agency_link(name):
                continue
            key = norm(name)
            if not key or key in seen:
                continue
            seen.add(key)
            href = a["href"]
            if href.startswith("http"):
                site = href
            elif href.startswith("/"):
                site = f"https://www.wa.gov{href}"
            else:
                site = None
            results.append({
                "name":             name,
                "abbreviation":     _abbr(name),
                "category":         _classify_agency(name),
                "website":          site,
                "director":         None,
                "headquarters":     "Olympia, WA",
                "phone":            None,
                "mission":          None,
                "selection_method": None,
                "budget":           None,
                "employees":        None,
            })
            added += 1
        log.info("[state_agencies] +%d from %s", added, url)
        if len(results) >= 190:
            break

    log.info("[state_agencies] Total: %d (target 190+)", len(results))
    return results


def sync_state_agencies(db: Client, wa_id: str, dry_run: bool, stats: Stats) -> None:
    source = fetch_state_agencies()
    stats.source_count = len(source)
    stats.db_before = db_count(db, "state_agencies", wa_id)
    log.info("[state_agencies] DB before: %d | Source: %d", stats.db_before, stats.source_count)
    existing = load_existing(db, "state_agencies", wa_id)
    for rec in source:
        row = {
            "state_id": wa_id, "name": rec["name"],
            "abbreviation": rec.get("abbreviation"), "category": rec.get("category"),
            "website": rec.get("website"), "director": rec.get("director"),
            "headquarters": rec.get("headquarters"), "phone": rec.get("phone"),
            "mission": rec.get("mission"), "selection_method": rec.get("selection_method"),
            "budget": rec.get("budget"), "employees": rec.get("employees"),
        }
        upsert_record(db, "state_agencies", row, existing, norm(rec["name"]), dry_run, stats)
    stats.db_after = db_count(db, "state_agencies", wa_id) if not dry_run else stats.db_before + stats.inserted


# ── 7 & 8. Courts & Judiciary ─────────────────────────────────────────────────
# WA Courts directory — courts.wa.gov/court_dir/
# Syncs:  courts    → new `courts` table (court institution records)
#         judiciary → existing `judiciary` table (individual judges)

COURTS_BASE = "https://www.courts.wa.gov/court_dir/"

COURT_LEVEL_PARAMS = [
    ("superior",   "SUPERIOR"),
    ("district",   "DISTRICT"),
    ("municipal",  "MUNICIPAL"),
    ("appellate",  "APPEALS"),
    ("supreme",    "SUPREME"),
]


def _parse_courts_list(html: str, level_key: str) -> list[dict]:
    """Parse the list page for a given court level."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
        if not any(kw in " ".join(headers) for kw in ("court", "name", "county", "address")):
            continue

        def col(cells: list, *keys: str) -> str | None:
            for k in keys:
                for i, h in enumerate(headers):
                    if k in h and i < len(cells):
                        return c(cells[i].get_text(strip=True))
            return None

        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            # Court name may be in a link
            a_tag = cells[0].find("a") if cells else None
            name = (c(a_tag.get_text()) if a_tag else None) or col(cells, "court", "name")
            if not name:
                continue
            court_id: str | None = None
            if a_tag and "href" in a_tag.attrs:
                m = re.search(r"courtId=(\d+)", a_tag["href"])
                if m:
                    court_id = m.group(1)

            county_raw = col(cells, "county")
            results.append({
                "name":        name,
                "court_level": level_key,
                "_county":     county_raw,
                "_court_id":   court_id,
                "jurisdiction":county_raw,
                "address":     col(cells, "address", "location"),
                "phone":       col(cells, "phone", "telephone"),
                "website":     norm_url(col(cells, "website", "url")),
                "judge_count": to_int(col(cells, "judge", "bench")),
            })

    # Fallback: look for list items with court names
    if not results:
        for li in soup.find_all("li"):
            a_tag = li.find("a", href=True)
            text  = c(li.get_text())
            if not text or "court" not in text.lower():
                continue
            court_id = None
            if a_tag:
                m = re.search(r"courtId=(\d+)", a_tag.get("href", ""))
                if m:
                    court_id = m.group(1)
            name = c(a_tag.get_text()) if a_tag else text
            if not name:
                continue
            results.append({
                "name": name, "court_level": level_key, "_county": None,
                "_court_id": court_id, "jurisdiction": None,
                "address": None, "phone": None, "website": None, "judge_count": None,
            })

    return results


def _parse_court_detail(html: str) -> dict:
    """Extract address, phone, website, and judge list from a court detail page."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Strip nav noise
    for tag in soup(["nav", "header", "footer", "script", "style"]):
        tag.decompose()

    detail: dict[str, Any] = {}

    # Phone via regex on full text
    phone = first_phone(text)
    if phone:
        detail["phone"] = phone

    # Address: look for a block that matches "N Street, City, WA XXXXX"
    addr_m = re.search(
        r"(\d+\s+[\w\s.]+(?:Ave|St|Blvd|Dr|Rd|Way|Ln|Court|Pl|Loop)[.,\s]*[\w\s]+,\s*WA\s*\d{5})",
        text, re.I,
    )
    if addr_m:
        detail["address"] = addr_m.group(1).strip()

    # Website: external link (not courts.wa.gov itself)
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if href.startswith("http") and "courts.wa.gov" not in href and len(href) > 10:
            detail["website"] = norm_url(href)
            break

    # Judges: look for name patterns near "Judge" or "Commissioner" keyword
    judges: list[dict] = []
    judge_re = re.compile(
        r"(?:Judge|Magistrate|Commissioner|Justice|Presiding)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z.]+){1,3})",
        re.I,
    )
    for m in judge_re.finditer(text):
        jname = m.group(1).strip()
        title = m.group(0).split()[0].strip()
        judges.append({"name": jname, "position": title})

    # Deduplicate judges by name
    seen_judges: set[str] = set()
    unique_judges: list[dict] = []
    for j in judges:
        key = norm(j["name"])
        if key and key not in seen_judges:
            seen_judges.add(key)
            unique_judges.append(j)

    detail["judges"] = unique_judges
    if unique_judges:
        detail["judge_count"] = len(unique_judges)

    return detail


def fetch_all_courts(skip_details: bool = False) -> list[dict]:
    """Fetch courts from courts.wa.gov: list pages then (optionally) detail pages."""
    all_courts: list[dict] = []

    for level_key, wa_code in COURT_LEVEL_PARAMS:
        url = f"{COURTS_BASE}?fa=home.listCourts&courtLevel={wa_code}"
        log.info("[courts] Fetching %s courts from %s", level_key, url)
        r = http_get(url)
        if not r:
            log.warning("[courts] No response for %s", level_key)
            continue
        courts = _parse_courts_list(r.text, level_key)
        log.info("[courts] %d %s courts found on list page", len(courts), level_key)
        all_courts.extend(courts)

    if skip_details:
        log.info("[courts] Skipping detail pages (--skip-court-details)")
        return all_courts

    # Fetch detail page for each court that has a court_id
    courts_with_id = [c for c in all_courts if c.get("_court_id")]
    log.info("[courts] Fetching detail pages for %d courts ...", len(courts_with_id))
    for i, court in enumerate(courts_with_id):
        detail_url = f"{COURTS_BASE}?fa=home.display&courtId={court['_court_id']}"
        if (i + 1) % 10 == 0:
            log.info("[courts] Detail page %d/%d", i + 1, len(courts_with_id))
        r = http_get(detail_url)
        if not r:
            continue
        detail = _parse_court_detail(r.text)
        # Merge detail into court record (don't overwrite existing values)
        for k, v in detail.items():
            if k != "judges" and court.get(k) is None and v is not None:
                court[k] = v
        # Stash judges for judiciary sync
        if detail.get("judges"):
            court["_judges"] = detail["judges"]

    return all_courts


def sync_courts(
    db: Client, wa_id: str, dry_run: bool, stats: Stats,
    all_courts: list[dict],
) -> None:
    stats.source_count = len(all_courts)
    stats.db_before = db_count(db, "courts", wa_id)
    log.info("[courts] DB before: %d | Source: %d", stats.db_before, stats.source_count)
    existing = load_existing(db, "courts", wa_id)
    for court in all_courts:
        row = {
            "state_id":    wa_id,
            "county_id":   county_id(court.get("_county")),
            "name":        court["name"],
            "court_level": court.get("court_level"),
            "address":     court.get("address"),
            "phone":       court.get("phone"),
            "website":     court.get("website"),
            "jurisdiction":court.get("jurisdiction"),
            "judge_count": court.get("judge_count"),
        }
        upsert_record(db, "courts", row, existing, norm(court["name"]), dry_run, stats)
    stats.db_after = db_count(db, "courts", wa_id) if not dry_run else stats.db_before + stats.inserted


def sync_judiciary(
    db: Client, wa_id: str, dry_run: bool, stats: Stats,
    all_courts: list[dict],
) -> None:
    """Sync individual judges extracted from court detail pages into the judiciary table."""
    # Build a composite key: norm(judge_name)|norm(court_name)
    existing_raw = db.table("judiciary").select("*").eq("state_id", wa_id).execute().data or []
    existing: dict[str, dict] = {}
    for row in existing_raw:
        jname = norm(row.get("judge_name"))
        cname = norm(row.get("court_name"))
        key   = f"{jname}|{cname}"
        if key:
            existing[key] = row

    judge_rows: list[dict] = []
    for court in all_courts:
        for judge in court.get("_judges", []):
            jname = judge.get("name")
            if not jname:
                continue
            judge_rows.append({
                "state_id":   wa_id,
                "county_id":  county_id(court.get("_county")),
                "judge_name": jname,
                "court_name": court["name"],
                "court_level":court.get("court_level"),
                "position":   judge.get("position"),
            })

    stats.source_count = len(judge_rows)
    stats.db_before = len(existing)
    log.info("[judiciary] DB before: %d judges | Extracted from detail pages: %d", stats.db_before, stats.source_count)

    for row in judge_rows:
        key = f"{norm(row['judge_name'])}|{norm(row['court_name'])}"
        if key not in existing:
            ok = safe_insert(db, "judiciary", row, dry_run)
            if ok:
                stats.inserted += 1
            else:
                stats.errors += 1
        else:
            updates = partial_update(existing[key], row)
            if updates:
                ok = safe_update(db, "judiciary", existing[key]["id"], updates, dry_run)
                if ok:
                    stats.updated += 1
            else:
                stats.skipped += 1

    stats.db_after = (len(existing) + stats.inserted) if not dry_run else stats.db_before + stats.inserted


# ── 9. Colleges ───────────────────────────────────────────────────────────────
# College Scorecard API (api.data.gov) — FEC_API_KEY

SCORECARD_FIELDS = ",".join([
    "school.name", "school.city", "school.county",
    "school.school_url", "school.phone",
    "school.ownership", "school.carnegie_basic",
    "school.institutional_characteristics.level",
    "latest.student.size",
])

OWNERSHIP_LABEL = {1: "Public", 2: "Private Nonprofit", 3: "Private For-Profit"}


def _college_type(ownership: int | None, level: int | None, carnegie: int | None) -> str:
    if ownership == 3:
        return "for_profit"
    if level == 2:
        return "community_college"
    if level == 3:
        return "technical"
    # Carnegie basic classification 21–23 = doctoral, 14–16 = master's
    if carnegie and carnegie in range(14, 24):
        return "university"
    if ownership == 2:
        return "liberal_arts"
    return "university"


def fetch_colleges() -> list[dict]:
    log.info("[colleges] Fetching from College Scorecard API (school.state=WA)")
    raw = paginate_scorecard({
        "school.state": "WA",
        "fields": SCORECARD_FIELDS,
        "api_key": DATA_GOV_KEY,
    })
    log.info("[colleges] %d WA institutions returned", len(raw))
    out: list[dict] = []
    for row in raw:
        name = c(row.get("school.name"))
        if not name:
            continue
        ownership = to_int(row.get("school.ownership"))
        level     = to_int(row.get("school.institutional_characteristics.level"))
        carnegie  = to_int(row.get("school.carnegie_basic"))
        # County name is FIPS-coded in Scorecard; use city→county lookup in sync
        out.append({
            "name":           name,
            "_city":          c(row.get("school.city")),
            "_county_name":   c(row.get("school.county")),
            "website":        norm_url(row.get("school.school_url")),
            "phone":          c(row.get("school.phone")),
            "enrollment":     to_int(row.get("latest.student.size")),
            "ownership_type": OWNERSHIP_LABEL.get(ownership) if ownership else None,
            "type":           _college_type(ownership, level, carnegie),
            "president":      None,
        })
    return out


def sync_colleges(db: Client, wa_id: str, dry_run: bool, stats: Stats) -> None:
    source = fetch_colleges()
    stats.source_count = len(source)
    try:
        stats.db_before = db_count(db, "colleges", wa_id)
    except Exception as exc:
        log.error("[colleges] Cannot query colleges table — run add_colleges.sql first: %s", exc)
        stats.errors += 1
        return
    log.info("[colleges] DB before: %d | Source: %d", stats.db_before, stats.source_count)

    # Build city→county_id map from municipalities table
    try:
        muni_rows = db.table("municipalities").select("name,county_id").execute().data or []
        city_county: dict[str, str] = {norm(m["name"]): m["county_id"] for m in muni_rows if m.get("county_id")}
    except Exception:
        city_county = {}

    existing = load_existing(db, "colleges", wa_id)
    for rec in source:
        city    = rec.pop("_city", None)
        cty_name= rec.pop("_county_name", None)
        cid     = city_county.get(norm(city)) or county_id(cty_name)
        row = {
            "state_id": wa_id, "county_id": cid, "city": city,
            "name": rec["name"], "type": rec.get("type"),
            "ownership_type": rec.get("ownership_type"), "enrollment": rec.get("enrollment"),
            "president": rec.get("president"), "phone": rec.get("phone"),
            "website": rec.get("website"),
        }
        upsert_record(db, "colleges", row, existing, norm(rec["name"]), dry_run, stats)
    stats.db_after = db_count(db, "colleges", wa_id) if not dry_run else stats.db_before + stats.inserted


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(all_stats: list[Stats], dry_run: bool) -> None:
    W = 76
    cols = ("Category", "Source", "Before", "After", "+Added", "~Updated", "Err")
    widths = (20, 7, 7, 7, 7, 9, 5)

    def fmt_row(*vals: str) -> str:
        return "  " + "  ".join(str(v).rjust(w) if i else str(v).ljust(w)
                                 for i, (v, w) in enumerate(zip(vals, widths)))

    sep = "  " + "-" * (W - 2)
    print()
    print("=" * W)
    print(f"  {'DRY RUN — no writes' if dry_run else 'COMPLETENESS SYNC REPORT'}")
    print("=" * W)
    print(fmt_row(*cols))
    print(sep)

    tot_ins = tot_upd = 0
    for s in all_stats:
        after_str = str(s.db_after) if s.db_after >= 0 else "—"
        err_str   = str(s.errors) if s.errors else "-"
        print(fmt_row(s.category, str(s.source_count), str(s.db_before),
                      after_str, f"+{s.inserted}", f"~{s.updated}", err_str))
        tot_ins += s.inserted
        tot_upd += s.updated

    print(sep)
    print(fmt_row("TOTAL", "", "", "", f"+{tot_ins}", f"~{tot_upd}", ""))
    print("=" * W)
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync WA directory data from authoritative sources into Supabase"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + compare without writing to Supabase")
    parser.add_argument("--only", metavar="CAT[,CAT...]",
                        help=f"Categories to sync: {', '.join(ALL_CATEGORIES)}")
    parser.add_argument("--skip-court-details", action="store_true",
                        help="Skip per-court detail page fetches (faster; no addresses/judges)")
    args = parser.parse_args()

    targets: list[str]
    if args.only:
        targets = [t.strip() for t in args.only.split(",")]
        bad = [t for t in targets if t not in ALL_CATEGORIES]
        if bad:
            parser.error(f"Unknown categories: {bad}. Valid: {ALL_CATEGORIES}")
    else:
        targets = list(ALL_CATEGORIES)

    if args.dry_run:
        log.info("DRY RUN — no Supabase writes")

    db = create_client(SUPABASE_URL, SUPABASE_KEY)
    wa_row = db.table("states").select("id").eq("abbreviation", "WA").single().execute()
    wa_id: str = wa_row.data["id"]
    log.info("WA state_id: %s", wa_id)
    load_counties(db, wa_id)

    all_stats: list[Stats] = []

    # Courts and judiciary share a single HTTP fetch pass; run together
    courts_data: list[dict] | None = None

    for cat in targets:
        stats = Stats(category=cat)
        try:
            if cat == "hospitals":
                sync_hospitals(db, wa_id, args.dry_run, stats)
            elif cat == "law_enforcement":
                sync_law_enforcement(db, wa_id, args.dry_run, stats)
            elif cat == "fire_ems":
                sync_fire_ems(db, wa_id, args.dry_run, stats)
            elif cat == "school_districts":
                sync_school_districts(db, wa_id, args.dry_run, stats)
            elif cat == "utilities_transit":
                sync_utilities_transit(db, wa_id, args.dry_run, stats)
            elif cat == "state_agencies":
                sync_state_agencies(db, wa_id, args.dry_run, stats)
            elif cat in ("courts", "judiciary"):
                # Fetch court data once; reuse for both sync functions
                if courts_data is None:
                    courts_data = fetch_all_courts(skip_details=args.skip_court_details)
                if cat == "courts":
                    sync_courts(db, wa_id, args.dry_run, stats, courts_data)
                else:
                    sync_judiciary(db, wa_id, args.dry_run, stats, courts_data)
            elif cat == "colleges":
                sync_colleges(db, wa_id, args.dry_run, stats)
        except Exception as exc:
            log.exception("[%s] Unhandled error: %s", cat, exc)
            stats.errors += 1
        all_stats.append(stats)

    print_report(all_stats, dry_run=args.dry_run)
    log.info("Done.")


if __name__ == "__main__":
    main()
