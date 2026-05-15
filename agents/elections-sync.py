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

import json
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

RETRY_ATTEMPTS = 3
RETRY_DELAY    = 2.0   # seconds between retries on Supabase insert failure

PROGRESS_FILE  = Path(__file__).parent.parent / "output" / "elections-sync-progress.json"

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
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def get_html(url: str, session: requests.Session, *, delay: bool = True) -> Optional[BeautifulSoup]:
    """Fetch URL and return parsed HTML, or None on failure."""
    if delay:
        time.sleep(REQUEST_DELAY)
    try:
        resp = session.get(url, timeout=30, allow_redirects=True)
        if resp.status_code != 200:
            log.debug("HTTP %d — %s", resp.status_code, url)
            return None
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


def _normalise_party_abbrev(abbrev: str) -> Optional[str]:
    """Expand single-letter party abbreviations like D, R, L, G."""
    mapping = {
        "d": "Democratic", "dem": "Democratic",
        "r": "Republican", "rep": "Republican", "gop": "Republican",
        "i": "Independent", "ind": "Independent",
        "l": "Libertarian", "lib": "Libertarian",
        "g": "Green", "grn": "Green",
        "np": "Nonpartisan",
    }
    return mapping.get(abbrev.lower().strip())


def _parse_name_party_incumbent(raw: str) -> tuple[str, Optional[str], bool]:
    """
    Parse strings like 'Jeff Holy(i)', 'Marie Gluesenkamp Pérez(D)', 'Bob(R)(i)'.
    Returns (clean_name, party_or_None, is_incumbent).
    """
    is_incumbent = bool(re.search(r"\(i\)", raw, re.IGNORECASE))
    text = re.sub(r"\(i\)", "", raw, flags=re.IGNORECASE).strip()

    party_match = re.search(r"\(([A-Za-z]+)\)\s*$", text)
    party = None
    if party_match:
        abbrev = party_match.group(1)
        party = _normalise_party_abbrev(abbrev) or _normalise_party(abbrev)
        text = text[: party_match.start()].strip()

    return text, party, is_incumbent


def _parse_partisan_table(soup: BeautifulSoup, category_url: str) -> list[dict]:
    """
    Parse a Ballotpedia category page that uses candidateListTablePartisan.
    Each row is one district; columns are Office + one per party.
    Returns list of race dicts {office_name, candidates, election_date, source_url, description}.
    """
    races = []
    for table in soup.find_all("table", class_=re.compile(r"candidateListTable", re.IGNORECASE)):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]

        col_office = 0
        col_parties: list[tuple[int, str]] = []
        for i, h in enumerate(headers):
            hl = h.lower()
            if any(k in hl for k in ("office", "district", "seat", "position")):
                col_office = i
            elif h:
                col_parties.append((i, h))

        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= col_office:
                continue

            office_cell = cells[col_office]
            office_text = office_cell.get_text(strip=True)
            if not office_text:
                continue

            office_link = office_cell.find("a", href=True)
            office_url = (
                f"{BP_BASE}{office_link['href']}"
                if office_link and office_link["href"].startswith("/")
                else None
            )

            candidates: list[dict] = []
            seen_names: set[str] = set()

            for col_idx, party_label in col_parties:
                if col_idx >= len(cells):
                    continue
                cell = cells[col_idx]

                links = cell.find_all("a", href=True)
                raw_entries: list[tuple[str, Optional[str]]] = []
                if links:
                    for link in links:
                        text = link.get_text(strip=True)
                        href = link["href"]
                        bp_url = f"{BP_BASE}{href}" if href.startswith("/") else None
                        if text:
                            raw_entries.append((text, bp_url))
                else:
                    cell_text = cell.get_text(strip=True)
                    if cell_text:
                        raw_entries.append((cell_text, None))

                for raw_text, bp_url in raw_entries:
                    name, cand_party, is_incumbent = _parse_name_party_incumbent(raw_text)
                    if not name or len(name) < 2 or name.lower() in seen_names:
                        continue
                    # Skip citation markers ([1]), district numbers, vote counts
                    if re.match(r'^[\d\s\[\].,\-()²-¹]+$', name):
                        continue
                    seen_names.add(name.lower())

                    if party_label.lower() == "other":
                        final_party = cand_party
                    else:
                        final_party = cand_party or _normalise_party(party_label)

                    candidates.append({
                        "name":         name,
                        "party":        final_party,
                        "is_incumbent": is_incumbent,
                        "bp_url":       bp_url,
                    })

            if not candidates:
                continue

            races.append({
                "office_name":   office_text,
                "candidates":    candidates,
                "election_date": ELECTION_DATE_GENERAL,
                "source_url":    office_url or category_url,
                "description":   None,
            })

    return races


def _parse_results_table_candidates(soup: BeautifulSoup) -> list[dict]:
    """Parse candidates from results_table on individual Ballotpedia race pages."""
    candidates: list[dict] = []
    seen_names: set[str] = set()
    skip = {"candidate", "party", "votes", "pct", "%", "total", ""}

    for table in soup.find_all("table", class_=re.compile(r"results_table", re.IGNORECASE)):
        for row in table.find_all("tr"):
            for cell in row.find_all(["td", "th"]):
                raw = cell.get_text(strip=True)
                if not raw:
                    continue
                name, party, is_incumbent = _parse_name_party_incumbent(raw)
                if not name or len(name) < 2 or name.lower() in seen_names:
                    continue
                if name.lower() in skip:
                    continue
                # Skip vote counts, percentages, and citation markers
                if re.match(r'^[\d\s,.()\[\]\-/%]+$', name):
                    continue
                seen_names.add(name.lower())
                link = cell.find("a", href=True)
                bp_url = (
                    f"{BP_BASE}{link['href']}"
                    if link and link["href"].startswith("/")
                    else None
                )
                candidates.append({
                    "name":         name,
                    "party":        party,
                    "is_incumbent": is_incumbent,
                    "bp_url":       bp_url,
                })

    return candidates


def _fetch_individual_race(url: str, session: requests.Session) -> Optional[dict]:
    """
    Fetch a single Ballotpedia race page and extract office name + candidates.
    Tries results_table first, then falls back to generic candidate-column tables.
    """
    soup = get_html(url, session)
    if not soup:
        return None

    h1 = soup.find("h1", {"id": "firstHeading"}) or soup.find("h1")
    office_name = h1.get_text(strip=True) if h1 else ""
    office_name = re.sub(
        r",?\s*(election[,]?\s*)?2026$", "", office_name, flags=re.IGNORECASE
    ).strip()

    candidates = _parse_results_table_candidates(soup)

    if not candidates:
        seen_names: set[str] = set()
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            joined = " ".join(headers)
            if "candidate" not in joined:
                continue
            cand_col = next((i for i, h in enumerate(headers) if "candidate" in h), None)
            party_col = next((i for i, h in enumerate(headers) if "party" in h), None)
            if cand_col is None:
                continue
            for row in table.find_all("tr")[1:]:
                cells = row.find_all(["td", "th"])
                if len(cells) <= cand_col:
                    continue
                cell = cells[cand_col]
                raw = cell.get_text(strip=True)
                if not raw or len(raw) < 2:
                    continue
                is_inc = bool(re.search(r"\(i\)", raw, re.IGNORECASE))
                name = re.sub(r"\s*\([Ii]\)\s*$", "", raw).strip()
                name = re.sub(r"\s*\*+\s*$", "", name).strip()
                if not name or name.lower() in seen_names:
                    continue
                seen_names.add(name.lower())
                link = cell.find("a", href=True)
                bp_url = (
                    f"{BP_BASE}{link['href']}"
                    if link and link["href"].startswith("/")
                    else None
                )
                party = ""
                if party_col is not None and len(cells) > party_col:
                    party = cells[party_col].get_text(strip=True)
                candidates.append({
                    "name":         name,
                    "party":        _normalise_party(party),
                    "is_incumbent": is_inc,
                    "bp_url":       bp_url,
                })

    description = None
    for p in _bp_content(soup).find_all("p"):
        txt = p.get_text(strip=True)
        if len(txt) > 40:
            description = txt[:400]
            break

    log.info("  Race: %-55s  candidates: %d", (office_name or url)[:55], len(candidates))
    return {
        "office_name":   office_name,
        "candidates":    candidates,
        "election_date": ELECTION_DATE_GENERAL,
        "source_url":    url,
        "description":   description,
    }


def _fetch_category_races(category_url: str, session: requests.Session) -> list[dict]:
    """
    Fetch one Ballotpedia category page and return all races found.
    If the page has a candidateListTablePartisan, parse it directly.
    Otherwise follow individual race links.
    """
    soup = get_html(category_url, session)
    if not soup:
        log.warning("Could not fetch category page: %s", category_url)
        return []

    label = category_url.split("/")[-1]

    if soup.find("table", class_=re.compile(r"candidateListTable", re.IGNORECASE)):
        races = _parse_partisan_table(soup, category_url)
        log.info("  Category %-50s  partisan table → %d races", label[:50], len(races))
        return races

    # Fall back to individual race links
    content = _bp_content(soup)
    seen: set[str] = set()
    race_links: list[tuple[str, str]] = []
    for a in content.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("/") or ":" in href or "2026" not in href:
            continue
        if href in seen:
            continue
        text = a.get_text(strip=True)
        if not text or len(text) < 5:
            continue
        seen.add(href)
        race_links.append((f"{BP_BASE}{href}", text))

    log.info("  Category %-50s  individual links → %d", label[:50], len(race_links))
    races = []
    for race_url, _ in race_links:
        detail = _fetch_individual_race(race_url, session)
        if detail:
            races.append(detail)
    return races


def fetch_bp_wa_races(session: requests.Session) -> list[dict]:
    """
    Orchestrate WA 2026 race collection from Ballotpedia.
    1. Fetch overview page → extract marqueetable category links.
    2. For each category page, delegate to _fetch_category_races().
    Returns list of race dicts {office_name, candidates, election_date, source_url, description}.
    """
    soup = get_html(BP_WA_2026, session, delay=False)
    if not soup:
        log.error("Could not fetch Ballotpedia WA 2026 overview page")
        return []

    content = _bp_content(soup)
    if not content:
        log.error("mw-content-text div missing — possible bot challenge response")
        return []

    seen: set[str] = set()
    category_urls: list[str] = []

    marquee = content.find("table", class_=re.compile(r"marqueetable", re.IGNORECASE))
    source = marquee if marquee else content
    for a in source.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/") and ":" not in href and href not in seen:
            seen.add(href)
            category_urls.append(f"{BP_BASE}{href}")

    log.info("Ballotpedia overview: %d category links", len(category_urls))

    all_races: list[dict] = []
    for cat_url in category_urls:
        all_races.extend(_fetch_category_races(cat_url, session))

    log.info("Ballotpedia: %d total races collected", len(all_races))
    return all_races


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


# ── Supabase retry wrapper ─────────────────────────────────────────────────────

def _sb_insert(table_ref, payload: dict) -> Optional[object]:
    """
    Execute a Supabase insert, retrying up to RETRY_ATTEMPTS times on any
    exception or empty-data response (both indicate transient failures).
    Returns the response object on success, None if all attempts fail.
    """
    for attempt in range(RETRY_ATTEMPTS):
        try:
            res = table_ref.insert(payload).execute()
            if res.data:
                return res
            # Empty data without an exception is also a transient failure
            if attempt < RETRY_ATTEMPTS - 1:
                log.warning(
                    "INSERT returned no data (attempt %d/%d) — retrying in %.0fs",
                    attempt + 1, RETRY_ATTEMPTS, RETRY_DELAY,
                )
                time.sleep(RETRY_DELAY)
        except Exception as exc:
            if attempt < RETRY_ATTEMPTS - 1:
                log.warning(
                    "INSERT error (attempt %d/%d): %s — retrying in %.0fs",
                    attempt + 1, RETRY_ATTEMPTS, exc, RETRY_DELAY,
                )
                time.sleep(RETRY_DELAY)
            else:
                log.error("INSERT failed after %d attempts: %s", RETRY_ATTEMPTS, exc)
    return None


# ── Progress checkpoint ────────────────────────────────────────────────────────

def load_progress() -> set[str]:
    """Return set of election IDs that have been fully processed (all candidates done)."""
    if not PROGRESS_FILE.exists():
        return set()
    try:
        data = json.loads(PROGRESS_FILE.read_text())
        return set(data.get("completed_election_ids", []))
    except Exception as exc:
        log.warning("Could not read progress file %s: %s — starting fresh", PROGRESS_FILE, exc)
        return set()


def save_progress(completed_ids: set[str]) -> None:
    """Persist the set of completed election IDs to PROGRESS_FILE."""
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(
        json.dumps({"completed_election_ids": sorted(completed_ids)}, indent=2)
    )


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
    res = _sb_insert(supabase.table("elections"), payload)
    if not res:
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
    res = _sb_insert(supabase.table("candidates"), payload)
    if not res:
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
        if _sb_insert(supabase.table("candidate_positions"), payload):
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

    completed_election_ids = load_progress()
    if completed_election_ids:
        log.info(
            "Resuming from checkpoint — %d elections already fully processed",
            len(completed_election_ids),
        )

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
    races = fetch_bp_wa_races(session)

    e_inserted = c_inserted = p_inserted = 0

    for race in races:
        now = ts()
        office_name   = (race.get("office_name") or "").strip()
        election_date = race.get("election_date") or ELECTION_DATE_GENERAL
        description   = race.get("description")
        candidates    = race.get("candidates") or []
        source_url    = race.get("source_url")

        if not office_name:
            continue

        # Classify level and resolve location FKs
        level, county_name, city_name = classify_race(office_name)

        county_id: Optional[str] = None
        if county_name:
            county_id = county_map.get(county_name)
            if not county_id:
                bare = county_name.replace(" County", "").strip()
                county_id = county_map.get(bare)
            if not county_id:
                log.warning("County not found in DB: %r", county_name)

        municipality_id: Optional[str] = None
        if level == "city" and city_name:
            municipality_id = municipality_map.get(city_name.lower())
            if not municipality_id:
                log.warning("Municipality not found in DB: %r", city_name)

        # Track whether this is a new insert
        ekey = (office_name, election_date, county_id, municipality_id)
        is_new_election = ekey not in existing_elecs

        election_id = upsert_election(
            supabase, wa_id, office_name, level, election_date,
            county_id, municipality_id, source_url, description,
            existing_elecs, now,
        )
        if election_id and is_new_election:
            e_inserted += 1

        if not election_id:
            continue

        # Skip elections already fully processed in a prior run
        if election_id in completed_election_ids:
            log.debug("SKIP (checkpoint): %s", office_name)
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

            bp_url = cand.get("bp_url")
            if not bp_url or not cand_id:
                continue

            positions = fetch_bp_candidate_positions(bp_url, session)
            if positions:
                p_inserted += upsert_positions(supabase, cand_id, positions, existing_pos, now)

        # Mark this election as fully processed and persist the checkpoint
        completed_election_ids.add(election_id)
        save_progress(completed_election_ids)

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
            if eid not in completed_election_ids:
                completed_election_ids.add(eid)
                save_progress(completed_election_ids)
            e_inserted += 1

    log.info(
        "=== elections-sync complete — %d elections, %d candidates, %d positions ===",
        e_inserted, c_inserted, p_inserted,
    )


if __name__ == "__main__":
    main()
