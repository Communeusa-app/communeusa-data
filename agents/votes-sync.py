"""
CommuneUSA Votes Sync Agent

Data sources:
  WA State  — Open States API /bills?include=votes (current session)
  US House  — House Clerk roll call XML feeds (clerk.house.gov/evs)
  US Senate — Senate.gov vote XML feeds (senate.gov/legislative/LIS)

Congress.gov (CONGRESS_API_KEY) is used to enrich federal bill titles where the
XML feeds do not provide a human-readable description.

Congress.gov does not expose a per-member vote endpoint; the Clerk and Senate
XML feeds are the authoritative sources for individual member vote records.

Required env vars (.env):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
    OPENSTATES_API_KEY
    CONGRESS_API_KEY

Run:
    pip install supabase requests python-dotenv
    python3 agents/votes-sync.py
"""

import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("votes-sync")

# ── Constants ──────────────────────────────────────────────────────────────────

OPENSTATES_BASE  = "https://v3.openstates.org"
CONGRESS_BASE    = "https://api.congress.gov/v3"
HOUSE_CLERK_BASE = "https://clerk.house.gov/evs"
SENATE_VOTE_BASE = "https://www.senate.gov/legislative/LIS/roll_call_votes"

CURRENT_SESSION    = "2025-2026"   # WA state legislative session
CURRENT_CONGRESS   = 119           # 119th U.S. Congress (2025–2027)

RECENT_VOTES_COUNT = 50            # Roll calls to fetch per House / Senate block
OPENSTATES_PAGES   = 5             # Pages of WA state bills to scan (50 bills/page)
OPENSTATES_LIMIT   = 50

# Motion text patterns that indicate a final-passage roll call
FINAL_PASSAGE_RE = re.compile(
    r"(final passage|3rd reading|passage|concurrence|sine die)",
    re.IGNORECASE,
)

# Procedural House legis-num values with no real bill
HOUSE_PROCEDURAL = {"QUORUM", "JOURNAL", "ADJOURN", "RECESS", "H RES", "MOTION"}


# ── Generic helpers ────────────────────────────────────────────────────────────

def clean(val) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def normalize_vote_cast(raw: str) -> str:
    v = raw.strip().lower()
    if v in ("yes", "yea", "aye"):
        return "Yea"
    if v in ("no", "nay"):
        return "Nay"
    if v in ("not voting", "absent", "not present"):
        return "Not Voting"
    if v == "present":
        return "Present"
    if v == "excused":
        return "Excused"
    return raw.strip().title()


def normalize_result(raw: str) -> str:
    r = raw.strip().lower()
    if any(w in r for w in ("pass", "agreed", "confirmed", "adopted", "enacted")):
        return "Passed"
    if any(w in r for w in ("fail", "rejected", "defeated", "not agreed")):
        return "Failed"
    return raw.strip()


def parse_date(raw: str) -> Optional[str]:
    """Return ISO date (YYYY-MM-DD) from various date string formats."""
    if not raw:
        return None
    for fmt in ("%d-%b-%Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Extract 'Month D, YYYY' from longer strings (Senate XML)
    m = re.search(r"([A-Za-z]+ \d{1,2}, \d{4})", raw)
    if m:
        try:
            return datetime.strptime(m.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw  # fall back to raw


# ── Congress.gov bill title enrichment ────────────────────────────────────────

_bill_title_cache: dict[str, Optional[str]] = {}

BILL_DESIGNATOR_RE = [
    (re.compile(r"\bH\s*\.?\s*R\s*\.?\s*(\d+)", re.I),         "hr"),
    (re.compile(r"\bS\s*\.?\s*(\d+)\b", re.I),                  "s"),
    (re.compile(r"\bH\s*\.?\s*J\s*\.?\s*RES\s*\.?\s*(\d+)", re.I), "hjres"),
    (re.compile(r"\bS\s*\.?\s*J\s*\.?\s*RES\s*\.?\s*(\d+)", re.I), "sjres"),
    (re.compile(r"\bH\s*\.?\s*CON\s*\.?\s*RES\s*\.?\s*(\d+)", re.I), "hconres"),
    (re.compile(r"\bS\s*\.?\s*CON\s*\.?\s*RES\s*\.?\s*(\d+)", re.I), "sconres"),
    (re.compile(r"\bH\s*\.?\s*RES\s*\.?\s*(\d+)", re.I),        "hres"),
    (re.compile(r"\bS\s*\.?\s*RES\s*\.?\s*(\d+)", re.I),        "sres"),
]


def parse_bill_designator(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return (bill_type, bill_number) for Congress.gov API, or (None, None)."""
    for pattern, btype in BILL_DESIGNATOR_RE:
        m = pattern.search(text)
        if m:
            return btype, m.group(1)
    return None, None


def enrich_bill_title(bill_ref: str, api_key: str) -> Optional[str]:
    """Look up the official bill title from Congress.gov. Returns None on miss."""
    bill_type, bill_num = parse_bill_designator(bill_ref)
    if not bill_type or not bill_num:
        return None

    cache_key = f"{bill_type}/{bill_num}"
    if cache_key in _bill_title_cache:
        return _bill_title_cache[cache_key]

    try:
        resp = requests.get(
            f"{CONGRESS_BASE}/bill/{CURRENT_CONGRESS}/{bill_type}/{bill_num}",
            params={"format": "json", "api_key": api_key},
            timeout=15,
        )
        if resp.status_code == 200:
            title = clean(resp.json().get("bill", {}).get("title"))
            _bill_title_cache[cache_key] = title
            time.sleep(0.4)  # Congress.gov rate limit headroom
            return title
    except Exception as exc:
        log.debug("Congress.gov enrichment failed (%s): %s", cache_key, exc)

    _bill_title_cache[cache_key] = None
    return None


# ── Supabase helpers ───────────────────────────────────────────────────────────

def build_name_lookup(supabase: Client) -> dict[str, Optional[str]]:
    """
    Build a full-name → official_id lookup for all active officials.
    When the same name maps to multiple rows with identical title+level (true
    duplicates), keep the first ID seen. Only mark None when rows differ in
    level/title and cannot be disambiguated.
    """
    res = supabase.table("officials").select("id,official_name,level,office_title").eq("is_active", True).execute()
    # name → list of (id, level, office_title)
    candidates: dict[str, list[tuple[str, str, str]]] = {}
    for row in res.data or []:
        key = (row["official_name"] or "").strip().lower()
        if not key:
            continue
        entry = (row["id"], row.get("level", ""), row.get("office_title", "") or "")
        candidates.setdefault(key, []).append(entry)

    lookup: dict[str, Optional[str]] = {}
    for key, entries in candidates.items():
        if len(entries) == 1:
            lookup[key] = entries[0][0]
        else:
            # Check if all duplicates share the same level+title (identical rows)
            unique_profiles = {(lvl, title) for _, lvl, title in entries}
            if len(unique_profiles) == 1:
                # True duplicate rows — any ID will do, use the first
                lookup[key] = entries[0][0]
            else:
                # Genuinely different officials with the same name — skip
                log.warning("Ambiguous name (different profiles): %r — skipping", key)
                lookup[key] = None
    return lookup


def build_house_last_lookup(supabase: Client) -> dict[str, Optional[str]]:
    """
    Build a last-name → official_id lookup for active federal House members.
    Used because the House Clerk XML only provides legislator last names.

    When duplicate rows exist for the same last name (same full name + title),
    treat them as identical duplicates and use the first ID. Only skip when the
    last name genuinely maps to different people (different full names).
    """
    res = (
        supabase.table("officials")
        .select("id,official_name,office_title")
        .eq("level", "federal")
        .eq("is_active", True)
        .execute()
    )
    # Collect all (id, full_name) per last name
    candidates: dict[str, list[tuple[str, str]]] = {}
    for row in res.data or []:
        title = (row.get("office_title") or "").lower()
        if "representative" not in title and "house" not in title:
            continue
        name = row["official_name"] or ""
        last = name.rsplit(" ", 1)[-1].lower()
        candidates.setdefault(last, []).append((row["id"], name))

    lookup: dict[str, Optional[str]] = {}
    for last, entries in candidates.items():
        if len(entries) == 1:
            lookup[last] = entries[0][0]
            continue
        # Check if all entries are for the same full name (identical duplicate rows)
        unique_names = {name for _, name in entries}
        if len(unique_names) == 1:
            lookup[last] = entries[0][0]  # same person, use first ID
        else:
            lookup[last] = None  # genuinely different people with same last name
            log.debug("Ambiguous House last name %r (%d entries) — skipping", last, len(entries))
    return lookup


def load_existing_keys(supabase: Client) -> set[tuple]:
    """Return (official_id, bill_name, vote_date) for all existing voting records.
    Paginates through the full table to avoid Supabase's 1000-row default limit.
    """
    keys: set[tuple] = set()
    page_size = 1000
    offset = 0
    while True:
        res = (
            supabase.table("voting_records")
            .select("official_id,bill_name,vote_date")
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = res.data or []
        for r in batch:
            keys.add((r["official_id"], r["bill_name"], r["vote_date"]))
        if len(batch) < page_size:
            break
        offset += page_size
    return keys


def batch_insert(supabase: Client, rows: list[dict]) -> int:
    if not rows:
        return 0
    chunk = 100
    inserted = 0
    for i in range(0, len(rows), chunk):
        # ignore_duplicates uses ON CONFLICT DO NOTHING (requires the dedup unique constraint)
        supabase.table("voting_records").upsert(rows[i : i + chunk], ignore_duplicates=True).execute()
        inserted += len(rows[i : i + chunk])
    return inserted


# ── Open States — WA State votes ──────────────────────────────────────────────

def fetch_wa_state_votes(api_key: str) -> list[dict]:
    """
    Fetch WA state bill final-passage votes from Open States.
    Returns raw vote dicts keyed with 'official_name'.
    """
    records = []
    headers = {"X-API-KEY": api_key}

    for page in range(1, OPENSTATES_PAGES + 1):
        try:
            resp = requests.get(
                f"{OPENSTATES_BASE}/bills",
                params={
                    "jurisdiction": "wa",
                    "session":      CURRENT_SESSION,
                    "include":      "votes",
                    "limit":        OPENSTATES_LIMIT,
                    "page":         page,
                },
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Open States page %d error: %s", page, exc)
            break

        data  = resp.json()
        bills = data.get("results", [])
        if not bills:
            break

        for bill in bills:
            # Skip non-bill items (resolutions, memorials, etc.)
            classification = bill.get("classification") or []
            if "bill" not in classification:
                continue

            identifier    = bill.get("identifier", "")
            title         = clean(bill.get("title") or "")
            subjects      = bill.get("subject") or []
            category      = subjects[0] if subjects else None
            openstates_url = bill.get("openstates_url")

            for vote_event in bill.get("votes") or []:
                motion = vote_event.get("motion_text") or ""
                # Only keep final-passage roll calls
                if not FINAL_PASSAGE_RE.search(motion):
                    continue

                vote_date = clean(vote_event.get("start_date"))
                result    = normalize_result(vote_event.get("result", ""))

                for v in vote_event.get("votes") or []:
                    voter = v.get("voter") or {}
                    name  = clean(voter.get("name") or v.get("voter_name"))
                    if not name:
                        continue
                    records.append({
                        "official_name":    name,
                        "bill_name":        identifier,
                        "bill_description": title,
                        "topic_category":   category,
                        "vote_date":        vote_date,
                        "vote_cast":        normalize_vote_cast(v.get("option", "")),
                        "result":           result,
                        "constituent_impact": None,
                        "source_url":       openstates_url,
                    })

        pagination = data.get("pagination") or {}
        if page >= (pagination.get("max_page") or page):
            break
        time.sleep(0.25)

    log.info("Open States — %d raw WA state vote records across %d pages", len(records), page)
    return records


# ── House Clerk roll call XML ──────────────────────────────────────────────────

def find_max_house_roll(year: int) -> int:
    """Binary search for the highest existing roll call number for a given year."""
    lo, hi = 1, 500
    while lo < hi:
        mid = (lo + hi + 1) // 2
        url = f"{HOUSE_CLERK_BASE}/{year}/roll{mid:03d}.xml"
        try:
            r = requests.head(url, timeout=10)
            if r.status_code == 200:
                lo = mid
            else:
                hi = mid - 1
        except Exception:
            hi = mid - 1
    return lo


def fetch_house_votes(year: int, congress_key: str) -> list[dict]:
    """
    Fetch the most recent RECENT_VOTES_COUNT House roll calls for `year`,
    filtered to WA member votes on actual legislation.
    """
    max_roll = find_max_house_roll(year)
    if max_roll < 1:
        log.info("House %d: no roll calls found", year)
        return []

    log.info("House %d: latest roll = %d, fetching last %d", year, max_roll, RECENT_VOTES_COUNT)
    records = []
    start = max(1, max_roll - RECENT_VOTES_COUNT + 1)

    for roll_num in range(max_roll, start - 1, -1):
        url = f"{HOUSE_CLERK_BASE}/{year}/roll{roll_num:03d}.xml"
        try:
            resp = requests.get(url, timeout=20)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
        except Exception as exc:
            log.debug("House roll %d/%03d parse error: %s", year, roll_num, exc)
            continue

        meta = root.find("vote-metadata")
        if meta is None:
            continue

        legis_num     = (meta.findtext("legis-num") or "").strip().upper()
        vote_question = clean(meta.findtext("vote-question")) or ""
        vote_desc     = clean(meta.findtext("vote-desc"))
        action_date   = clean(meta.findtext("action-date"))
        vote_result   = clean(meta.findtext("vote-result")) or ""

        # Skip procedural roll calls with no real bill
        if not legis_num or legis_num in HOUSE_PROCEDURAL:
            continue
        if not any(c.isdigit() for c in legis_num):
            continue  # no bill number present

        bill_description = vote_desc or enrich_bill_title(legis_num, congress_key)
        result     = normalize_result(vote_result)
        vote_date  = parse_date(action_date)
        source_url = f"https://clerk.house.gov/evs/{year}/roll{roll_num:03d}.xml"

        for rv in root.iter("recorded-vote"):
            leg  = rv.find("legislator")
            vote = rv.find("vote")
            if leg is None or vote is None:
                continue
            if leg.get("state") != "WA":
                continue

            # Strip state disambiguator like "(WA)" from display name
            raw_name  = (leg.text or "").strip()
            last_name = re.sub(r"\s*\([^)]+\)\s*$", "", raw_name).strip().lower()

            records.append({
                "_house_last": last_name,
                "bill_name":         legis_num,
                "bill_description":  bill_description,
                "topic_category":    vote_question or None,
                "vote_date":         vote_date,
                "vote_cast":         normalize_vote_cast(vote.text or ""),
                "result":            result,
                "constituent_impact": None,
                "source_url":        source_url,
            })

        time.sleep(0.1)

    log.info("House %d: %d WA member votes from last %d rolls",
             year, len(records), RECENT_VOTES_COUNT)
    return records


# ── Senate vote XML ────────────────────────────────────────────────────────────

def find_max_senate_vote(congress: int, session: int) -> int:
    """Binary search for the highest existing Senate vote number for a congress/session."""
    lo, hi = 1, 500
    while lo < hi:
        mid = (lo + hi + 1) // 2
        url = (f"{SENATE_VOTE_BASE}/vote{congress}{session}/"
               f"vote_{congress}_{session}_{mid:05d}.xml")
        try:
            r = requests.head(url, timeout=10, allow_redirects=False)
            if r.status_code == 200:
                lo = mid
            else:
                hi = mid - 1
        except Exception:
            hi = mid - 1
    return lo


def fetch_senate_votes(congress: int, session: int, congress_key: str) -> list[dict]:
    """
    Fetch the most recent RECENT_VOTES_COUNT Senate votes for a congress/session,
    filtered to WA member votes.
    """
    max_vote = find_max_senate_vote(congress, session)
    if max_vote < 1:
        log.info("Senate %d/%d: no votes found", congress, session)
        return []

    log.info("Senate %d/%d: latest vote = %d, fetching last %d",
             congress, session, max_vote, RECENT_VOTES_COUNT)
    records = []
    start = max(1, max_vote - RECENT_VOTES_COUNT + 1)

    for vote_num in range(max_vote, start - 1, -1):
        url = (f"{SENATE_VOTE_BASE}/vote{congress}{session}/"
               f"vote_{congress}_{session}_{vote_num:05d}.xml")
        try:
            resp = requests.get(url, timeout=20, allow_redirects=False)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
        except Exception as exc:
            log.debug("Senate %d/%d vote %d parse error: %s", congress, session, vote_num, exc)
            continue

        vote_question = clean(root.findtext("vote_question_text")) or ""
        doc_text      = clean(root.findtext("vote_document_text")) or ""
        vote_result   = clean(root.findtext("vote_result_text")) or ""
        vote_date_raw = clean(root.findtext("vote_date"))
        vote_title    = clean(root.findtext("vote_title"))

        bill_name        = doc_text or vote_question[:80]
        bill_description = vote_title or enrich_bill_title(doc_text, congress_key)
        result           = normalize_result(vote_result)
        vote_date        = parse_date(vote_date_raw)
        source_url       = url

        members_el = root.find("members")
        if members_el is None:
            continue

        for member in members_el.findall("member"):
            if clean(member.findtext("state")) != "WA":
                continue

            first = clean(member.findtext("first_name")) or ""
            last  = clean(member.findtext("last_name")) or ""
            cast  = clean(member.findtext("vote_cast")) or ""

            records.append({
                "official_name":    f"{first} {last}".strip(),
                "bill_name":        bill_name,
                "bill_description": bill_description,
                "topic_category":   vote_question or None,
                "vote_date":        vote_date,
                "vote_cast":        normalize_vote_cast(cast),
                "result":           result,
                "constituent_impact": None,
                "source_url":       source_url,
            })

        time.sleep(0.1)

    log.info("Senate %d/%d: %d WA member votes from last %d votes",
             congress, session, len(records), RECENT_VOTES_COUNT)
    return records


# ── Resolution ────────────────────────────────────────────────────────────────

def resolve_and_stage(
    records: list[dict],
    source: str,
    name_lookup: dict,
    house_last_lookup: dict,
    existing_keys: set,
) -> tuple[list[dict], int, int]:
    """
    Resolve official_name / _house_last to official_id, deduplicate against
    existing_keys, and return (rows_to_insert, skipped_unmatched, skipped_dup).
    """
    to_insert: list[dict] = []
    unmatched = 0
    duplicates = 0
    now = ts()

    for rec in records:
        # Determine official_id
        if "_house_last" in rec:
            last = rec.pop("_house_last")
            official_id = house_last_lookup.get(last)
            if official_id is None:
                log.warning("[%s] House: no match for last name %r", source, last)
                unmatched += 1
                continue
        else:
            name_key = (rec.pop("official_name", "") or "").lower()
            official_id = name_lookup.get(name_key)
            if official_id is None:
                log.warning("[%s] No match for %r", source, name_key)
                unmatched += 1
                continue

        bill_name = clean(rec.get("bill_name"))
        vote_date = clean(rec.get("vote_date"))
        dup_key   = (official_id, bill_name, vote_date)

        if dup_key in existing_keys:
            duplicates += 1
            continue

        row = {
            "official_id":        official_id,
            "bill_name":          bill_name,
            "bill_description":   clean(rec.get("bill_description")),
            "topic_category":     clean(rec.get("topic_category")),
            "vote_date":          vote_date,
            "vote_cast":          clean(rec.get("vote_cast")),
            "result":             clean(rec.get("result")),
            "constituent_impact": None,
            "source_url":         clean(rec.get("source_url")),
        }
        existing_keys.add(dup_key)
        to_insert.append(row)
        log.info("[%s] [%s] QUEUE  %s  %s  |  %s  |  %s",
                 now, source,
                 vote_date or "?",
                 bill_name or "?",
                 row.get("vote_cast", "?"),
                 row.get("result", "?"))

    return to_insert, unmatched, duplicates


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    url       = os.environ.get("SUPABASE_URL")
    key       = os.environ.get("SUPABASE_SERVICE_KEY")
    os_key    = os.environ.get("OPENSTATES_API_KEY")
    cg_key    = os.environ.get("CONGRESS_API_KEY")

    missing = [k for k, v in {
        "SUPABASE_URL":         url,
        "SUPABASE_SERVICE_KEY": key,
        "OPENSTATES_API_KEY":   os_key,
        "CONGRESS_API_KEY":     cg_key,
    }.items() if not v]
    if missing:
        sys.exit(f"ERROR: missing env vars: {', '.join(missing)}")

    log.info("=== CommuneUSA votes-sync starting ===")
    supabase: Client = create_client(url, key)

    name_lookup       = build_name_lookup(supabase)
    house_last_lookup = build_house_last_lookup(supabase)
    existing_keys     = load_existing_keys(supabase)
    log.info("Loaded %d officials and %d existing vote records",
             len(name_lookup), len(existing_keys))

    all_rows:    list[dict] = []
    total_unmatched  = 0
    total_duplicates = 0

    def run(records: list[dict], source: str) -> None:
        nonlocal total_unmatched, total_duplicates
        rows, um, dups = resolve_and_stage(
            records, source, name_lookup, house_last_lookup, existing_keys
        )
        all_rows.extend(rows)
        total_unmatched  += um
        total_duplicates += dups

    # WA State Legislature (Open States)
    run(fetch_wa_state_votes(os_key), "state")

    # U.S. House (current year + previous year for cross-year sessions)
    current_year = datetime.now().year
    for year in sorted({current_year - 1, current_year}):
        run(fetch_house_votes(year, cg_key), f"house-{year}")

    # U.S. Senate (session 1 and session 2 of current Congress)
    for session in (1, 2):
        run(fetch_senate_votes(CURRENT_CONGRESS, session, cg_key), f"senate-s{session}")

    inserted = batch_insert(supabase, all_rows)
    log.info(
        "=== votes-sync complete: %d inserted, %d duplicate, %d unmatched ===",
        inserted, total_duplicates, total_unmatched,
    )


if __name__ == "__main__":
    main()
