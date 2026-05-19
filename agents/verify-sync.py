"""
CommuneUSA Verification & Gap-Fill Agent

Cross-references each extracted JSON file against authoritative Washington State
sources, fills gaps where data is available, and produces a gap report.

Sources
-------
  School Districts — OSPI via data.wa.gov (343 WA districts)
  Law Enforcement  — WA UCR via data.wa.gov (276 agencies)
  State Agencies   — wa.gov/agency HTML directory (276 agencies)
  Judiciary        — courts.wa.gov (JS-rendered; reports gaps only)

Output
------
  output/school_boards.json     — updated in-place
  output/law_enforcement.json   — updated in-place
  output/state_agencies.json    — updated in-place
  output/verify-report.json     — gap report

Run
---
  pip install requests python-dotenv rapidfuzz
  python3 agents/verify-sync.py [--dry-run]
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

try:
    from rapidfuzz import fuzz as _fuzz
    def fuzzy(a: str, b: str) -> float:
        return _fuzz.token_sort_ratio(a, b)
except ImportError:
    import difflib
    def fuzzy(a: str, b: str) -> float:  # type: ignore[misc]
        return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100

load_dotenv(Path(__file__).parent.parent / ".env")

OUTPUT_DIR = Path(__file__).parent.parent / "output"

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("verify-sync")

# ── Constants ──────────────────────────────────────────────────────────────────

OSPI_DISTRICTS_URL = "https://data.wa.gov/resource/rxjk-6ieq.json"
UCR_AGENCIES_URL   = "https://data.wa.gov/resource/6njs-53y5.json"
WA_GOV_AGENCY_URL  = "https://wa.gov/agency"

SOCRATA_PAGE_SIZE  = 1000
FUZZY_THRESHOLD    = 85.0   # score ≥ this → consider record already present
API_DELAY          = 0.3    # seconds between data.wa.gov pages
WEB_DELAY          = 1.5    # seconds between wa.gov scrape requests

# Catch-all rows in our law_enforcement.json to exclude from coverage counts
_LE_CATCHALL_RE = re.compile(
    r"^all\s+other", re.IGNORECASE
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    """Lowercase, strip punctuation/extra whitespace for fuzzy comparison."""
    s = html.unescape(str(name or "")).lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Canonicalise "department of x" ↔ "x, department of"
    s = re.sub(r"^(department|office|board|commission|bureau|division)\s+of\s+(.+)$",
               r"\2 \1", s)
    s = re.sub(r",\s*(department|office|board|commission|bureau|division)\s+of$",
               r" \1", s)
    return s


def _best_match_score(query: str, corpus: list[str]) -> float:
    """Return the highest fuzzy score between query and any item in corpus."""
    if not corpus:
        return 0.0
    q = _normalize(query)
    return max(fuzzy(q, _normalize(c)) for c in corpus)


def safe_get(url: str, params: dict | None = None, timeout: int = 30) -> requests.Response:
    resp = requests.get(url, params=params, timeout=timeout,
                        headers={"User-Agent": "CommuneUSA-VerifySync/1.0"})
    resp.raise_for_status()
    return resp


def load_json(filename: str) -> list[dict]:
    path = OUTPUT_DIR / filename
    if not path.exists():
        log.warning("Output file not found: %s", path)
        return []
    return json.load(path.open(encoding="utf-8"))


def save_json(filename: str, records: list[dict]) -> None:
    path = OUTPUT_DIR / filename
    with path.open("w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


# ── School Districts ───────────────────────────────────────────────────────────

def fetch_ospi_districts() -> list[dict]:
    """Fetch all distinct WA school districts from OSPI via data.wa.gov."""
    log.info("[OSPI] fetching district list …")
    seen: dict[str, str] = {}  # districtname → county

    offset = 0
    while True:
        params = {
            "$select":  "districtname,county",
            "$where":   "organizationlevel='District'",
            "$group":   "districtname,county",
            "$limit":   SOCRATA_PAGE_SIZE,
            "$offset":  offset,
            "$order":   "districtname ASC",
        }
        try:
            data = safe_get(OSPI_DISTRICTS_URL, params=params).json()
        except Exception as exc:
            log.error("[OSPI] fetch failed at offset %d: %s", offset, exc)
            break

        if not isinstance(data, list) or not data:
            break

        for row in data:
            name = (row.get("districtname") or "").strip()
            county = (row.get("county") or "").strip()
            if name and name not in seen:
                seen[name] = county

        log.info("[OSPI] fetched %d total districts so far", len(seen))
        if len(data) < SOCRATA_PAGE_SIZE:
            break
        offset += SOCRATA_PAGE_SIZE
        time.sleep(API_DELAY)

    return [{"district_name": n, "county": c} for n, c in sorted(seen.items())]


def verify_school_districts(dry_run: bool) -> dict:
    """Compare OSPI district list against school_boards.json; fill gaps."""
    existing = load_json("school_boards.json")
    our_districts = {r["district_name"] for r in existing if r.get("district_name")}

    source_districts = fetch_ospi_districts()
    log.info("[School] OSPI: %d districts | ours: %d distinct districts",
             len(source_districts), len(our_districts))

    added: list[str] = []
    manual_review: list[str] = []

    new_records: list[dict] = []
    for row in source_districts:
        name = row["district_name"]
        if _best_match_score(name, list(our_districts)) >= FUZZY_THRESHOLD:
            continue  # already covered

        # Gap found — add stub record
        log.info("[School] GAP: %r", name)
        stub = {
            "district_name":    name,
            "county":           row["county"].title(),
            "director_name":    None,
            "position":         None,
            "party_affiliation": None,
            "term_start":       None,
            "term_end":         None,
            "phone":            None,
            "website":          None,
            "_data_source":     "OSPI via data.wa.gov",
            "_needs_detail":    True,
        }
        new_records.append(stub)
        added.append(name)

    if not dry_run and new_records:
        save_json("school_boards.json", existing + new_records)
        log.info("[School] added %d stub records", len(new_records))
    elif dry_run and new_records:
        log.info("[School] [dry-run] would add %d records", len(new_records))

    return {
        "source":           "OSPI via data.wa.gov (rxjk-6ieq)",
        "source_count":     len(source_districts),
        "our_district_count_before": len(our_districts),
        "gaps_found":       len(new_records),
        "records_added":    len(added) if not dry_run else 0,
        "dry_run_would_add": len(added) if dry_run else 0,
        "note": (
            "Gap-filled records contain district_name and county only. "
            "Board director names require lookup at ospi.k12.wa.us/FinanceAdmin/BoardDirectors."
        ),
        "added_districts":  added[:50],
    }


# ── Law Enforcement ────────────────────────────────────────────────────────────

def fetch_ucr_agencies() -> list[dict]:
    """Fetch all distinct WA law enforcement agencies from the UCR dataset."""
    log.info("[LE] fetching UCR agency list …")
    seen: dict[str, str] = {}  # agency name → county

    offset = 0
    while True:
        params = {
            "$select":  "location,county",
            "$group":   "location,county",
            "$limit":   SOCRATA_PAGE_SIZE,
            "$offset":  offset,
            "$order":   "location ASC",
        }
        try:
            data = safe_get(UCR_AGENCIES_URL, params=params).json()
        except Exception as exc:
            log.error("[LE] fetch failed at offset %d: %s", offset, exc)
            break

        if not isinstance(data, list) or not data:
            break

        for row in data:
            name = (row.get("location") or "").strip()
            county = (row.get("county") or "").strip()
            if name and name not in seen:
                seen[name] = county

        if len(data) < SOCRATA_PAGE_SIZE:
            break
        offset += SOCRATA_PAGE_SIZE
        time.sleep(API_DELAY)

    log.info("[LE] UCR: %d distinct agencies", len(seen))
    return [{"name": n, "county": c} for n, c in sorted(seen.items())]


def _infer_agency_type(name: str) -> str:
    n = name.lower()
    if "sheriff" in n:
        return "County Sheriff"
    if "state patrol" in n or "wsp" in n:
        return "State Patrol"
    if "tribal" in n or "nation" in n:
        return "Tribal Police"
    if "port" in n:
        return "Port Police"
    if "transit" in n or "metro" in n or "sound" in n:
        return "Transit Police"
    if "university" in n or "college" in n or "wsu" in n or "uwm" in n:
        return "Campus Police"
    return "Municipal Police"


def verify_law_enforcement(dry_run: bool) -> dict:
    """Compare UCR agency list against law_enforcement.json; fill gaps."""
    existing = load_json("law_enforcement.json")

    # Exclude our catch-all summary rows
    real_existing = [r for r in existing if not _LE_CATCHALL_RE.match(r.get("name") or "")]
    our_names = [r["name"] for r in real_existing if r.get("name")]

    source_agencies = fetch_ucr_agencies()
    log.info("[LE] UCR: %d agencies | ours (excl. catch-alls): %d",
             len(source_agencies), len(our_names))

    added: list[str] = []
    new_records: list[dict] = []

    for row in source_agencies:
        name = row["name"]
        if _best_match_score(name, our_names) >= FUZZY_THRESHOLD:
            continue

        log.info("[LE] GAP: %r (%s County)", name, row["county"].title())
        stub = {
            "agency_type":   _infer_agency_type(name),
            "name":          name,
            "jurisdiction":  f"{row['county'].title()} County",
            "chief_name":    None,
            "sworn_officers": None,
            "headquarters":  None,
            "phone":         None,
            "website":       None,
            "_data_source":  "WA UCR via data.wa.gov (6njs-53y5)",
            "_needs_detail": True,
        }
        new_records.append(stub)
        added.append(name)

    if not dry_run and new_records:
        save_json("law_enforcement.json", existing + new_records)
        log.info("[LE] added %d stub records", len(new_records))
    elif dry_run and new_records:
        log.info("[LE] [dry-run] would add %d records", len(new_records))

    return {
        "source":           "WA UCR via data.wa.gov (6njs-53y5)",
        "source_count":     len(source_agencies),
        "our_count_before": len(our_names),
        "gaps_found":       len(new_records),
        "records_added":    len(added) if not dry_run else 0,
        "dry_run_would_add": len(added) if dry_run else 0,
        "note": (
            "Gap-filled records contain agency name and county only. "
            "Chief/sheriff names and contact info require lookup at waspc.org or individual agency sites."
        ),
        "added_agencies":   added[:50],
    }


# ── State Agencies ─────────────────────────────────────────────────────────────

def fetch_wa_gov_agencies() -> list[dict]:
    """Parse the wa.gov/agency HTML directory for all WA state agencies."""
    log.info("[Agencies] fetching wa.gov/agency …")
    try:
        resp = safe_get(WA_GOV_AGENCY_URL, timeout=20)
        page_html = resp.text
    except Exception as exc:
        log.error("[Agencies] fetch failed: %s", exc)
        return []
    time.sleep(WEB_DELAY)

    # Extract all <a> tags within <li> elements
    raw_links = re.findall(
        r'<li[^>]*>.*?href="(https?://[^"]+)"\s*[^>]*>([^<]+)</a>',
        page_html, re.DOTALL
    )

    # Keep only top-level agency entries: skip sub-service description links
    _skip_prefixes = (
        "learn", "search", "report", "find", "apply", "get", "view", "check",
        "register", "access", "download", "pay", "submit", "track", "consumer",
        "regulated", "assistance", "ask", "protecting", "safeguarding",
        "help", "file", "managing", "about", "see", "explore", "connect",
        "read", "contact", "information",
    )
    # Must contain at least one of these words to qualify as an agency entry
    agency_words = {
        "department", "office", "commission", "board", "agency", "authority",
        "council", "bureau", "division", "administration", "institute",
        "center", "program", "guard", "patrol", "court", "legislature",
        "university", "college", "association", "committee", "network", "tvw",
        "solutions", "services", "affairs", "foundation", "partnership",
    }
    agencies = []
    seen_names: set[str] = set()
    for href, raw_name in raw_links:
        name = html.unescape(raw_name.strip())
        if not name or len(name) > 90:
            continue
        first_word = name.split()[0].lower().rstrip(",") if name.split() else ""
        if first_word in _skip_prefixes:
            continue
        # Require at least one agency indicator word
        name_lower = name.lower()
        if not any(w in name_lower for w in agency_words):
            continue
        norm = _normalize(name)
        if norm in seen_names:
            continue
        seen_names.add(norm)
        # Normalise website to bare domain
        domain_match = re.search(r"https?://(?:www\.)?([^/]+)", href)
        website = domain_match.group(1) if domain_match else href
        agencies.append({"name": name, "website": website})

    log.info("[Agencies] parsed %d agency entries from wa.gov/agency", len(agencies))
    return agencies


def verify_state_agencies(dry_run: bool) -> dict:
    """Compare wa.gov agency list against state_agencies.json; fill gaps."""
    existing = load_json("state_agencies.json")
    our_names = [r["name"] for r in existing if r.get("name")]

    source_agencies = fetch_wa_gov_agencies()
    log.info("[Agencies] wa.gov: %d | ours: %d", len(source_agencies), len(our_names))

    added: list[str] = []
    new_records: list[dict] = []

    for row in source_agencies:
        name = row["name"]
        if _best_match_score(name, our_names) >= FUZZY_THRESHOLD:
            continue

        log.info("[Agencies] GAP: %r", name)
        stub = {
            "category":        _infer_agency_category(name),
            "name":            name,
            "abbreviation":    None,
            "director_name":   None,
            "selection_method": None,
            "budget":          None,
            "employees":       None,
            "headquarters":    None,
            "phone":           None,
            "website":         row["website"],
            "mission_summary": None,
            "_data_source":    "wa.gov/agency",
            "_needs_detail":   True,
        }
        new_records.append(stub)
        added.append(name)

    if not dry_run and new_records:
        save_json("state_agencies.json", existing + new_records)
        log.info("[Agencies] added %d stub records", len(new_records))
    elif dry_run and new_records:
        log.info("[Agencies] [dry-run] would add %d records", len(new_records))

    return {
        "source":           "wa.gov/agency HTML directory",
        "source_count":     len(source_agencies),
        "our_count_before": len(our_names),
        "gaps_found":       len(new_records),
        "records_added":    len(added) if not dry_run else 0,
        "dry_run_would_add": len(added) if dry_run else 0,
        "note": (
            "Gap-filled records contain agency name and website only. "
            "Director, budget, and mission details require lookup at the agency website."
        ),
        "added_agencies":   added[:50],
    }


def _infer_agency_category(name: str) -> str:
    n = name.lower()
    if any(w in n for w in ("university", "college", "wsu", "wwu", "ewu", "cwu")):
        return "Higher Education"
    if any(w in n for w in ("health", "medical", "hospital")):
        return "Health"
    if any(w in n for w in ("transport", "highway", "wsdot", "rail")):
        return "Transportation"
    if any(w in n for w in ("environment", "ecology", "natural resource", "wildlife", "fish")):
        return "Environment & Natural Resources"
    if any(w in n for w in ("labor", "employment", "workforce", "commerce")):
        return "Labor & Commerce"
    if any(w in n for w in ("social", "children", "family", "aging", "human service", "dshs")):
        return "Social Services"
    if any(w in n for w in ("education", "school", "ospi", "student")):
        return "Education"
    if any(w in n for w in ("finance", "revenue", "budget", "treasury", "tax", "ofm")):
        return "Finance & Revenue"
    if any(w in n for w in ("military", "guard", "emergency", "wng")):
        return "Military & Emergency"
    if any(w in n for w in ("corrections", "prison", "doc ")):
        return "Corrections"
    if any(w in n for w in ("agriculture", "dairy", "livestock", "crop")):
        return "Agriculture"
    return "General Government"


# ── Judiciary ─────────────────────────────────────────────────────────────────

def verify_judiciary() -> dict:
    """
    Report on judiciary coverage.

    courts.wa.gov renders its directory entirely via JavaScript (web components)
    and ColdFusion; no static HTML or JSON API is accessible for automated
    scraping. All gap-filling is flagged for manual review.
    """
    existing = load_json("judiciary.json")
    our_count = len(existing)

    court_levels = {}
    for rec in existing:
        lvl = rec.get("court_level") or "Unknown"
        court_levels[lvl] = court_levels.get(lvl, 0) + 1

    # Known WA court structure for reference
    known_structure = {
        "Washington Supreme Court": 9,
        "Washington Court of Appeals (3 divisions)": "~22 judges",
        "Superior Courts (39 counties)": "~200+ judges",
        "District Courts": "~140+ judges",
        "Municipal Courts": "~100+ judges",
    }

    manual_review = []
    for court, expected in known_structure.items():
        matched_level = None
        for rec in existing:
            if fuzzy(_normalize(rec.get("court_name") or ""), _normalize(court)) >= 70:
                matched_level = rec.get("court_level")
                break
        if matched_level is None:
            manual_review.append({
                "court": court,
                "expected_judges": expected,
                "reason": "Not found in judiciary.json — manual lookup required at courts.wa.gov",
            })

    log.info("[Judiciary] our records: %d | manual review items: %d",
             our_count, len(manual_review))

    return {
        "source":       "courts.wa.gov/court_dir (JS-rendered — automated fetch not possible)",
        "source_count": "unknown (JS-rendered)",
        "our_count":    our_count,
        "coverage_by_level": court_levels,
        "known_wa_court_structure": known_structure,
        "records_added": 0,
        "gaps_found": len(manual_review),
        "note": (
            "courts.wa.gov renders its directory via JavaScript web components. "
            "No accessible JSON API or static HTML was found. "
            "Manual lookup required for Superior Court, District Court, and Municipal Court judges. "
            "Recommended source: courts.wa.gov/court_dir — browse by court type in a browser."
        ),
        "manual_review": manual_review,
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify extracted JSON files against authoritative WA sources"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report gaps but do not modify output files")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    mode = "DRY RUN — no files modified" if args.dry_run else "LIVE — output files will be updated"
    log.info("Starting verify-sync  [%s]", mode)

    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "categories": {},
    }

    log.info("── School Districts ───────────────────────────────────────")
    report["categories"]["school_boards"] = verify_school_districts(args.dry_run)

    log.info("── Law Enforcement ────────────────────────────────────────")
    report["categories"]["law_enforcement"] = verify_law_enforcement(args.dry_run)

    log.info("── State Agencies ─────────────────────────────────────────")
    report["categories"]["state_agencies"] = verify_state_agencies(args.dry_run)

    log.info("── Judiciary ──────────────────────────────────────────────")
    report["categories"]["judiciary"] = verify_judiciary()

    # ── Summary ──
    total_added = sum(
        v.get("records_added", 0)
        for v in report["categories"].values()
    )
    total_gaps = sum(
        v.get("gaps_found", 0)
        for v in report["categories"].values()
    )
    report["summary"] = {
        "total_gaps_found":    total_gaps,
        "total_records_added": total_added,
        "categories_checked":  list(report["categories"].keys()),
    }

    report_path = OUTPUT_DIR / "verify-report.json"
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    log.info("verify-report.json written to %s", report_path)
    log.info("Summary — gaps found: %d  records added: %d", total_gaps, total_added)

    # Print per-category table
    print("\n┌─────────────────────────┬─────────────┬───────────┬──────────────┐")
    print("│ Category                │ Source Count│ Our Count │ Records Added│")
    print("├─────────────────────────┼─────────────┼───────────┼──────────────┤")
    for cat, data in report["categories"].items():
        src = str(data.get("source_count", "?"))[:11].rjust(11)
        ours_key = next(
            (k for k in ("our_district_count_before", "our_count_before", "our_count")
             if k in data), None
        )
        ours = str(data.get(ours_key, "?"))[:9].rjust(9) if ours_key else "?".rjust(9)
        added_key = "records_added" if not args.dry_run else "dry_run_would_add"
        added = str(data.get(added_key, 0))[:12].rjust(12)
        print(f"│ {cat:<23} │{src} │{ours} │{added} │")
    print("└─────────────────────────┴─────────────┴───────────┴──────────────┘")


if __name__ == "__main__":
    main()
