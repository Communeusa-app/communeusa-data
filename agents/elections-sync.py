"""
CommuneUSA Elections Sync Agent

Scrapes Washington 2026 election data from two sources and syncs to Supabase:

  Ballotpedia (primary)
    https://ballotpedia.org/Washington_elections,_2026
    Provides races, candidates, party affiliations, incumbent status, and
    policy positions from individual candidate pages.

  WA Secretary of State (supplementary)
    https://results.vote.wa.gov/results/20261103/
    Pre-election contest list when available; full results post-election.

Flow:
  1. Fetch WA 2026 race list from Ballotpedia overview page.
  2. For each race: classify level, resolve county/municipality FKs, upsert
     into elections table.
  3. For each race page: scrape candidates with party + incumbent status,
     link incumbents to official_id by name, upsert into candidates table.
  4. For each candidate with a Ballotpedia page: scrape policy positions,
     upsert into candidate_positions table.
  5. Check WA SoS results API; if contest list exists, fill any gaps missed
     by Ballotpedia.

Required env vars (.env):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY

Run:
    pip install supabase requests beautifulsoup4 python-dotenv
    python3 agents/elections-sync.py
"""

import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("elections-sync")

# ── Constants ──────────────────────────────────────────────────────────────────

BP_BASE        = "https://ballotpedia.org"
BP_WA_2026     = "https://ballotpedia.org/Washington_elections,_2026"
SOS_RESULTS    = "https://results.vote.wa.gov/results/20261103"
REQUEST_DELAY  = 1.5   # seconds between HTTP requests

ELECTION_DATE_GENERAL = "2026-11-03"
ELECTION_DATE_PRIMARY = "2026-08-04"
FILING_DEADLINE       = "2026-05-22"   # typical WA filing deadline

WA_FIPS = "53"

WA_COUNTIES = {
    "Adams", "Asotin", "Benton", "Chelan", "Clallam", "Clark", "Columbia",
    "Cowlitz", "Douglas", "Ferry", "Franklin", "Garfield", "Grant",
    "Grays Harbor", "Island", "Jefferson", "King", "Kitsap", "Kittitas",
    "Klickitat", "Lewis", "Lincoln", "Mason", "Okanogan", "Pacific",
    "Pend Oreille", "Pierce", "San Juan", "Skagit", "Skamania", "Snohomish",
    "Spokane", "Stevens", "Thurston", "Wahkiakum", "Walla Walla", "Whatcom",
    "Whitman", "Yakima",
}

# Keywords that identify state-level races
STATE_KEYWORDS = {
    "governor", "lieutenant governor", "attorney general", "secretary of state",
    "state treasurer", "state auditor", "commissioner of public lands",
    "insurance commissioner", "superintendent of public instruction",
    "state senate", "state house", "state representative", "state senator",
    "washington state", "supreme court justice", "court of appeals",
}

# Keywords that identify federal-level races
FEDERAL_KEYWORDS = {
    "u.s. senate", "u.s. house", "u.s. representative", "u.s. senator",
    "congress", "congressional", "united states senate", "united states house",
}


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "CommuneUSA-elections-sync/1.0 "
            "(civic data aggregator; contact jacksonsharkey@hotmail.com)"
        ),
        "Accept": "text/html,application/xhtml+xml",
    })
    return s


def get_html(url: str, session: requests.Session, *, delay: bool = True) -> Optional[BeautifulSoup]:
    """Fetch URL and return parsed HTML, or None on failure."""
    if delay:
        time.sleep(REQUEST_DELAY)
    try:
        resp = session.get(url, timeout=30, allow_redirects=True)
        if resp.status_code == 404:
            log.debug("404 — %s", url)
            return None
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.warning("HTTP error fetching %s: %s", url, exc)
        return None


def get_json(url: str, session: requests.Session, *, delay: bool = True) -> Optional[Union[dict, list]]:
    """Fetch URL and return parsed JSON, or None on failure."""
    if delay:
        time.sleep(REQUEST_DELAY)
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.debug("JSON fetch failed for %s: %s", url, exc)
        return None


# ── Race level classifier ──────────────────────────────────────────────────────

def classify_race(office_name: str) -> tuple[str, Optional[str], Optional[str]]:
    """
    Return (level, county_name_or_none, city_name_or_none).
    Level is one of: "federal", "state", "county", "city".
    """
    name_lower = office_name.lower()

    # Federal
    if any(kw in name_lower for kw in FEDERAL_KEYWORDS):
        return "federal", None, None

    # State
    if any(kw in name_lower for kw in STATE_KEYWORDS):
        return "state", None, None

    # County — look for "[Name] County" pattern
    for county in WA_COUNTIES:
        if f"{county.lower()} county" in name_lower or name_lower.startswith(county.lower()):
            return "county", county, None

    # City — everything else at local level
    # Try to extract city name from leading words before a dash or keyword
    city_match = re.match(
        r"^([A-Za-z\s]+?)(?:\s+(?:mayor|city council|council|school|port|fire|water|metro|transit))",
        office_name,
        re.IGNORECASE,
    )
    city_name = city_match.group(1).strip() if city_match else None
    return "city", None, city_name


# ── Ballotpedia scrapers ───────────────────────────────────────────────────────

def _bp_content(soup: BeautifulSoup) -> Tag:
    """Return the main content div of a Ballotpedia page."""
    return soup.find("div", {"id": "mw-content-text"}) or soup


def fetch_bp_wa_race_urls(session: requests.Session) -> list[dict]:
    """
    Scrape the WA 2026 elections overview page and return a list of
    {name, url, raw_section} dicts for each linked race page.
    """
    soup = get_html(BP_WA_2026, session, delay=False)
    if not soup:
        log.error("Could not fetch Ballotpedia WA 2026 overview page")
        return []

    content = _bp_content(soup)
    races: list[dict] = []
    seen_urls: set[str] = set()

    # Current section heading as context for level guessing
    current_section = ""

    for el in content.find_all(["h2", "h3", "h4", "a"]):
        if el.name in ("h2", "h3", "h4"):
            current_section = el.get_text(strip=True).lower()
            continue

        if el.name != "a":
            continue

        href = el.get("href", "")
        text = el.get_text(strip=True)

        # Only follow links to Ballotpedia race pages for 2026
        if not href or href in seen_urls:
            continue
        if not text or len(text) < 8:
            continue

        # Relative Ballotpedia links that look like race pages
        is_bp_internal = (
            href.startswith("/") and
            not href.startswith("//") and
            not href.startswith("/wiki/") and
            ":" not in href
        )
        if not is_bp_internal:
            continue

        # Must reference 2026 or be in a 2026-elections context
        full_url = f"{BP_BASE}{href}"
        if "2026" not in full_url and "2026" not in text:
            continue

        # Skip non-race links (categories, templates, help pages)
        skip_patterns = ["Category:", "Template:", "Help:", "File:", "Special:"]
        if any(p in href for p in skip_patterns):
            continue

        seen_urls.add(href)
        races.append({
            "name":        text,
            "url":         full_url,
            "raw_section": current_section,
        })

    log.info("Ballotpedia overview: found %d race links", len(races))
    return races


def fetch_bp_race_detail(url: str, session: requests.Session) -> dict:
    """
    Scrape a Ballotpedia race page and return:
      {
        office_name, candidates: [{name, party, is_incumbent, bp_url}],
        election_date, description
      }
    Returns empty dict on failure.
    """
    soup = get_html(url, session)
    if not soup:
        return {}

    # Office name from page title (h1)
    h1 = soup.find("h1", {"id": "firstHeading"}) or soup.find("h1")
    office_name = h1.get_text(strip=True) if h1 else ""

    # Strip common suffixes like ", 2026" or " election, 2026"
    office_name = re.sub(r",?\s*(election[,]?\s*)?2026$", "", office_name, flags=re.IGNORECASE).strip()

    # Infobox election date
    election_date = ELECTION_DATE_GENERAL
    infobox = soup.find("table", class_=re.compile(r"infobox|wikitable"))
    if infobox:
        for row in infobox.find_all("tr"):
            cells = row.find_all(["th", "td"])
            label_text = cells[0].get_text(strip=True).lower() if cells else ""
            if "election day" in label_text or "general" in label_text:
                if len(cells) > 1:
                    raw_date = cells[1].get_text(strip=True)
                    parsed = _parse_date(raw_date)
                    if parsed:
                        election_date = parsed

    # Candidates — try multiple common Ballotpedia table formats
    candidates: list[dict] = []
    seen_names: set[str] = set()

    # Format 1: table with "Candidate" and "Party" columns
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if "candidate" not in " ".join(headers):
            continue

        candidate_col = next(
            (i for i, h in enumerate(headers) if "candidate" in h), None
        )
        party_col = next(
            (i for i, h in enumerate(headers) if "party" in h), None
        )
        if candidate_col is None:
            continue

        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= candidate_col:
                continue

            cell = cells[candidate_col]
            raw_name = cell.get_text(strip=True)
            if not raw_name or len(raw_name) < 2:
                continue

            # Incumbent marker "(I)" or "(i)" at end of name
            is_incumbent = bool(re.search(r"\(i\)", raw_name, re.IGNORECASE))
            name = re.sub(r"\s*\([Ii]\)\s*$", "", raw_name).strip()
            name = re.sub(r"\s*\*+\s*$", "", name).strip()

            if not name or name.lower() in seen_names:
                continue
            seen_names.add(name.lower())

            # Ballotpedia profile link
            link_tag = cell.find("a", href=True)
            bp_url = (
                f"{BP_BASE}{link_tag['href']}"
                if link_tag and link_tag["href"].startswith("/")
                else (link_tag["href"] if link_tag else None)
            )

            party = ""
            if party_col is not None and len(cells) > party_col:
                party = cells[party_col].get_text(strip=True)

            candidates.append({
                "name":         name,
                "party":        _normalise_party(party),
                "is_incumbent": is_incumbent,
                "bp_url":       bp_url,
            })

    # Format 2: candidate cards / divs with class containing "candidate"
    if not candidates:
        for div in soup.find_all(class_=re.compile(r"candidate", re.IGNORECASE)):
            name_el = div.find(class_=re.compile(r"name", re.IGNORECASE)) or div.find("a")
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            if not name or name.lower() in seen_names:
                continue
            seen_names.add(name.lower())

            is_incumbent = "incumbent" in div.get_text(strip=True).lower()
            party_el = div.find(class_=re.compile(r"party", re.IGNORECASE))
            party = party_el.get_text(strip=True) if party_el else ""

            link_tag = name_el if name_el.name == "a" else name_el.find("a")
            bp_url = (
                f"{BP_BASE}{link_tag['href']}"
                if link_tag and link_tag.get("href", "").startswith("/")
                else None
            )

            candidates.append({
                "name":         name,
                "party":        _normalise_party(party),
                "is_incumbent": is_incumbent,
                "bp_url":       bp_url,
            })

    # Short page description (first non-empty paragraph in content)
    description = None
    content_div = _bp_content(soup)
    for p in content_div.find_all("p"):
        txt = p.get_text(strip=True)
        if len(txt) > 40:
            description = txt[:400]
            break

    log.info(
        "  Race: %-55s  candidates: %d",
        (office_name or url)[:55],
        len(candidates),
    )
    return {
        "office_name":   office_name,
        "candidates":    candidates,
        "election_date": election_date,
        "description":   description,
    }


def fetch_bp_candidate_positions(bp_url: str, session: requests.Session) -> list[dict]:
    """
    Scrape a Ballotpedia candidate page and return a list of:
      {issue_area, position_statement, source_url}

    Looks for the "Issues and policy positions" section.
    Returns [] if the section doesn't exist or is empty.
    """
    soup = get_html(bp_url, session)
    if not soup:
        return []

    positions: list[dict] = []
    content = _bp_content(soup)

    # Find the issues section heading
    issues_section = None
    for heading in content.find_all(["h2", "h3"]):
        text = heading.get_text(strip=True).lower()
        if "issue" in text or "position" in text or "policy" in text or "platform" in text:
            issues_section = heading
            break

    if not issues_section:
        return []

    # Collect all content between this heading and the next same-level heading
    current = issues_section.find_next_sibling()
    current_issue: Optional[str] = None
    statements: list[str] = []

    while current:
        if current.name in ("h2", "h3", "h4"):
            # Save any accumulated issue+statements before moving to next section
            if current_issue and statements:
                positions.append({
                    "issue_area":         current_issue,
                    "position_statement": " ".join(statements).strip(),
                    "source_url":         bp_url,
                })
            # Stop at next top-level section (h2)
            if current.name == "h2":
                break
            # h3/h4 within issues section = new issue area
            current_issue = current.get_text(strip=True)
            statements = []

        elif current.name == "p":
            txt = current.get_text(strip=True)
            if txt and len(txt) > 20:
                if current_issue:
                    statements.append(txt)
                else:
                    # No sub-heading — create a generic issue area
                    positions.append({
                        "issue_area":         None,
                        "position_statement": txt[:800],
                        "source_url":         bp_url,
                    })

        elif current.name in ("ul", "ol"):
            items = [li.get_text(strip=True) for li in current.find_all("li")]
            joined = " | ".join(i for i in items if i)
            if joined:
                if current_issue:
                    statements.append(joined)
                else:
                    positions.append({
                        "issue_area":         None,
                        "position_statement": joined[:800],
                        "source_url":         bp_url,
                    })

        current = current.find_next_sibling()

    # Flush last accumulated issue
    if current_issue and statements:
        positions.append({
            "issue_area":         current_issue,
            "position_statement": " ".join(statements).strip(),
            "source_url":         bp_url,
        })

    if positions:
        log.debug("  Positions for %s: %d found", bp_url.split("/")[-1], len(positions))

    return positions[:20]  # cap at 20 per candidate


# ── WA SoS supplementary source ───────────────────────────────────────────────

def fetch_sos_contests(session: requests.Session) -> list[dict]:
    """
    Try to fetch a pre-election contest list from the WA SoS results API.
    Returns [] if data is not yet available (the election hasn't happened).
    Each returned dict: {office_name, level, election_date}.
    """
    # Try the ContestResults.json export path (available after election night)
    contest_url = f"{SOS_RESULTS}/export/ContestResults.json"
    data = get_json(contest_url, session)
    if not data:
        log.info("WA SoS results not yet available (election has not occurred)")
        return []

    # The WA SoS JSON structure wraps contests in a list
    if isinstance(data, dict):
        contests_raw = data.get("Contests") or data.get("contests") or []
    else:
        contests_raw = data if isinstance(data, list) else []

    races: list[dict] = []
    for c in contests_raw:
        name = (
            c.get("RaceName") or c.get("ContestName") or c.get("Office") or ""
        ).strip()
        if not name:
            continue
        level, county, city = classify_race(name)
        races.append({
            "office_name":   name,
            "level":         level,
            "county_name":   county,
            "city_name":     city,
            "election_date": ELECTION_DATE_GENERAL,
            "source_url":    SOS_RESULTS,
        })

    log.info("WA SoS: found %d contests", len(races))
    return races


# ── String helpers ─────────────────────────────────────────────────────────────

def _normalise_party(raw: str) -> Optional[str]:
    """Map various Ballotpedia party strings to clean names."""
    raw = (raw or "").strip()
    mapping = {
        "democratic": "Democratic",
        "democrat":   "Democratic",
        "dem":        "Democratic",
        "republican": "Republican",
        "rep":        "Republican",
        "gop":        "Republican",
        "libertarian": "Libertarian",
        "green":      "Green",
        "independent": "Independent",
        "nonpartisan": "Nonpartisan",
        "no party preference": "Nonpartisan",
    }
    return mapping.get(raw.lower(), raw) if raw else None


def _parse_date(raw: str) -> Optional[str]:
    """Try to parse common US date strings into YYYY-MM-DD. Returns None on failure."""
    formats = ["%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"]
    for fmt in formats:
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


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
    """Return {bare_county_name: county_id} for all WA counties."""
    res = (
        supabase.table("counties")
        .select("id,name")
        .eq("state_id", wa_id)
        .execute()
    )
    return {r["name"]: r["id"] for r in (res.data or [])}


def get_municipality_map(supabase: Client, wa_id: str) -> dict[str, str]:
    """Return {lowercase_city_name: municipality_id}."""
    res = (
        supabase.table("municipalities")
        .select("id,name")
        .eq("state_id", wa_id)
        .execute()
    )
    return {r["name"].lower(): r["id"] for r in (res.data or [])}


def build_official_lookup(supabase: Client) -> dict[str, Optional[str]]:
    """Return {lowercase_name: official_id} for all active officials."""
    res = (
        supabase.table("officials")
        .select("id,official_name")
        .eq("is_active", True)
        .execute()
    )
    candidates: dict[str, list[str]] = {}
    for r in res.data or []:
        key = (r["official_name"] or "").strip().lower()
        if key:
            candidates.setdefault(key, []).append(r["id"])
    return {
        k: ids[0] if len(ids) == 1 else None
        for k, ids in candidates.items()
    }


def load_existing_elections(supabase: Client) -> dict[tuple, str]:
    """Return {(office_name, election_date, county_id, municipality_id): election_id}."""
    res = (
        supabase.table("elections")
        .select("id,office_name,election_date,county_id,municipality_id")
        .execute()
    )
    return {
        (
            r["office_name"],
            r.get("election_date"),
            r.get("county_id"),
            r.get("municipality_id"),
        ): r["id"]
        for r in (res.data or [])
    }


def load_existing_candidates(supabase: Client) -> set[tuple]:
    """Return {(election_id, lowercase_name)} for dedup."""
    res = supabase.table("candidates").select("election_id,name").execute()
    return {
        (r["election_id"], (r["name"] or "").lower())
        for r in (res.data or [])
    }


def load_existing_positions(supabase: Client) -> set[tuple]:
    """Return {(candidate_id, issue_area)} for dedup."""
    res = supabase.table("candidate_positions").select("candidate_id,issue_area").execute()
    return {
        (r["candidate_id"], (r.get("issue_area") or "").lower())
        for r in (res.data or [])
    }


# ── Core sync ──────────────────────────────────────────────────────────────────

def upsert_election(
    supabase:         Client,
    wa_id:            str,
    office_name:      str,
    level:            str,
    election_date:    str,
    county_id:        Optional[str],
    municipality_id:  Optional[str],
    source_url:       Optional[str],
    description:      Optional[str],
    existing:         dict[tuple, str],
    now:              str,
) -> Optional[str]:
    """
    Insert election if not already present; return its DB id.
    Updates source_url / description if the row exists but those fields are empty.
    """
    key = (office_name, election_date, county_id, municipality_id)
    if key in existing:
        return existing[key]

    primary_date    = ELECTION_DATE_PRIMARY
    filing_deadline = FILING_DEADLINE

    payload = {
        "state_id":        wa_id,
        "county_id":       county_id,
        "municipality_id": municipality_id,
        "office_name":     office_name,
        "level":           level,
        "election_date":   election_date,
        "primary_date":    primary_date,
        "filing_deadline": filing_deadline,
        "description":     description,
        "source_url":      source_url,
    }
    res = supabase.table("elections").insert(payload).execute()
    if not res.data:
        log.error("[%s] INSERT election failed: %s", now, office_name)
        return None

    db_id = res.data[0]["id"]
    existing[key] = db_id
    log.info("[%s] INSERT election  [%s]  %s", now, level, office_name)
    return db_id


def upsert_candidate(
    supabase:          Client,
    election_id:       str,
    name:              str,
    party:             Optional[str],
    is_incumbent:      bool,
    bp_url:            Optional[str],
    official_lookup:   dict[str, Optional[str]],
    existing:          set[tuple],
    now:               str,
) -> Optional[str]:
    """Insert candidate if not present; return candidate DB id."""
    cand_key = (election_id, name.lower())
    if cand_key in existing:
        return None

    official_id: Optional[str] = None
    if is_incumbent:
        official_id = official_lookup.get(name.lower())
        if official_id:
            log.info("[%s]   Linked incumbent %r → official %s", now, name, official_id)
        else:
            log.debug("[%s]   No official match for incumbent %r", now, name)

    payload = {
        "election_id":    election_id,
        "official_id":    official_id,
        "name":           name,
        "party":          party,
        "is_incumbent":   is_incumbent,
        "website":        None,
        "ballotpedia_url": bp_url,
    }
    res = supabase.table("candidates").insert(payload).execute()
    if not res.data:
        log.error("[%s] INSERT candidate failed: %s", now, name)
        return None

    db_id = res.data[0]["id"]
    existing.add(cand_key)
    log.info(
        "[%s]   INSERT candidate  %s%s",
        now,
        name,
        " (inc)" if is_incumbent else "",
    )
    return db_id


def upsert_positions(
    supabase:    Client,
    cand_id:     str,
    positions:   list[dict],
    existing:    set[tuple],
    now:         str,
) -> int:
    """Insert new candidate_positions rows; return count inserted."""
    inserted = 0
    for pos in positions:
        issue_area = (pos.get("issue_area") or "").lower()
        key = (cand_id, issue_area)
        if key in existing:
            continue
        payload = {
            "candidate_id":       cand_id,
            "issue_area":         pos.get("issue_area"),
            "position_statement": pos.get("position_statement"),
            "source_url":         pos.get("source_url"),
        }
        supabase.table("candidate_positions").insert(payload).execute()
        existing.add(key)
        inserted += 1
    if inserted:
        log.info("[%s]     Inserted %d position(s)", now, inserted)
    return inserted


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")

    log.info("=== elections-sync starting ===")

    supabase: Client = create_client(supabase_url, supabase_key)
    wa_id            = get_wa_state_id(supabase)
    county_map       = get_county_map(supabase, wa_id)
    municipality_map = get_municipality_map(supabase, wa_id)
    official_lookup  = build_official_lookup(supabase)
    existing_elecs   = load_existing_elections(supabase)
    existing_cands   = load_existing_candidates(supabase)
    existing_pos     = load_existing_positions(supabase)

    log.info(
        "DB state — %d elections, %d candidates, %d counties, %d municipalities, %d officials",
        len(existing_elecs), len(existing_cands),
        len(county_map), len(municipality_map), len(official_lookup),
    )

    session = make_session()

    # ── Phase 1: Collect races from Ballotpedia ────────────────────────────────
    race_links = fetch_bp_wa_race_urls(session)

    e_inserted = c_inserted = p_inserted = 0

    for race_link in race_links:
        now = ts()
        race_url  = race_link["url"]
        race_name = race_link["name"]

        detail = fetch_bp_race_detail(race_url, session)
        if not detail:
            log.warning("[%s] Skipping (no detail): %s", now, race_name)
            continue

        office_name   = detail.get("office_name") or race_name
        election_date = detail.get("election_date", ELECTION_DATE_GENERAL)
        description   = detail.get("description")
        candidates    = detail.get("candidates", [])

        if not office_name:
            continue

        # Classify level and resolve location FKs
        level, county_name, city_name = classify_race(office_name)

        county_id: Optional[str] = None
        if county_name:
            county_id = county_map.get(county_name)
            if not county_id:
                # Try bare name lookup (strip " County" suffix if present)
                bare = county_name.replace(" County", "").strip()
                county_id = county_map.get(bare)
            if not county_id:
                log.warning("County not found in DB: %r", county_name)

        municipality_id: Optional[str] = None
        if level == "city" and city_name:
            municipality_id = municipality_map.get(city_name.lower())
            if not municipality_id:
                log.warning("Municipality not found in DB: %r", city_name)

        # Upsert election
        election_id = upsert_election(
            supabase, wa_id, office_name, level, election_date,
            county_id, municipality_id, race_url, description,
            existing_elecs, now,
        )
        if election_id and election_id not in {v for v in existing_elecs.values() if v != election_id}:
            e_inserted += 1

        if not election_id:
            continue

        # Upsert candidates
        for cand in candidates:
            name = (cand.get("name") or "").strip()
            if not name:
                continue

            cand_id = upsert_candidate(
                supabase, election_id, name,
                cand.get("party"), cand.get("is_incumbent", False),
                cand.get("bp_url"), official_lookup, existing_cands, now,
            )
            if cand_id:
                c_inserted += 1

            # Phase 3: scrape positions from candidate's own Ballotpedia page
            bp_url = cand.get("bp_url")
            if not bp_url or not cand_id:
                continue

            positions = fetch_bp_candidate_positions(bp_url, session)
            if positions:
                p_inserted += upsert_positions(supabase, cand_id, positions, existing_pos, now)

    # ── Phase 2: Supplement with WA SoS results API ───────────────────────────
    sos_races = fetch_sos_contests(session)
    now = ts()
    for race in sos_races:
        office_name = race["office_name"]
        level       = race["level"]
        county_name = race.get("county_name")
        city_name   = race.get("city_name")

        county_id = county_map.get(county_name) if county_name else None
        municipality_id = municipality_map.get(city_name.lower()) if city_name else None

        eid = upsert_election(
            supabase, wa_id, office_name, level,
            race["election_date"], county_id, municipality_id,
            race.get("source_url"), None, existing_elecs, now,
        )
        if eid:
            e_inserted += 1

    log.info(
        "=== elections-sync complete — %d elections, %d candidates, %d positions ===",
        e_inserted, c_inserted, p_inserted,
    )


if __name__ == "__main__":
    main()
