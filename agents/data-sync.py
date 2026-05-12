"""
CommuneUSA Data Sync Agent

Syncs Washington State legislators (Open States API) and WA federal legislators
(Congress.gov API) into Supabase. Inserts new officials, updates changed fields,
and flags removed officials as inactive rather than deleting them.

Required env vars:
    SUPABASE_URL         — e.g. https://xyzxyz.supabase.co
    SUPABASE_SERVICE_KEY — service-role key (not anon) for write access
    OPENSTATES_API_KEY   — Open States v3 API key
    CONGRESS_API_KEY     — Congress.gov API key

Run:
    pip install supabase requests python-dotenv
    python agents/data-sync.py
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from supabase import Client, create_client

# Load .env from the repo root (one level above this script's directory).
load_dotenv(Path(__file__).parent.parent / ".env")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("data-sync")

# ── API constants ─────────────────────────────────────────────────────────────

OPENSTATES_BASE  = "https://v3.openstates.org"
CONGRESS_BASE    = "https://api.congress.gov/v3"

# Fields synced from the APIs; compared against DB to detect changes.
MUTABLE_FIELDS = {"party", "term_end", "phone", "email", "office_title", "district",
                  "official_website", "is_active"}

# Congress.gov maps chamber strings to our office_title values.
CHAMBER_TITLE = {
    "Senate":                    "U.S. Senator",
    "House of Representatives":  "U.S. Representative",
}
CHAMBER_CATEGORY = {
    "Senate":                    "Senate",
    "House of Representatives":  "House",
}


# ── Generic helpers ───────────────────────────────────────────────────────────

def clean(val) -> Optional[str]:
    """Strip strings; coerce empty / whitespace to None."""
    if val is None:
        return None
    if isinstance(val, str):
        val = val.strip()
        return val or None
    return str(val)


def last_first_to_first_last(name: str) -> str:
    """Convert 'Smith, Jane A.' → 'Jane A. Smith'."""
    parts = name.split(",", 1)
    if len(parts) == 2:
        return f"{parts[1].strip()} {parts[0].strip()}"
    return name.strip()


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Open States ───────────────────────────────────────────────────────────────

def fetch_openstates_legislators(api_key: str) -> list[dict]:
    """
    Return all current WA state legislators from Open States v3.
    Paginates automatically. Each result is normalised to our schema shape.
    """
    people: list[dict] = []
    page = 1
    per_page = 50

    while True:
        resp = requests.get(
            f"{OPENSTATES_BASE}/people",
            params={
                "jurisdiction": "wa",
                "include":      "offices",
                "per_page":     per_page,
                "page":         page,
            },
            headers={"X-API-KEY": api_key},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        if not results:
            break

        for person in results:
            role = person.get("current_role") or {}
            org_class = role.get("org_classification", "")
            if org_class == "upper":
                title, category = "State Senate", "Senate"
            elif org_class == "lower":
                title, category = "State House of Representatives", "House"
            else:
                continue  # skip governors, inactive members, etc.

            offices = person.get("offices") or []
            phone = clean(offices[0].get("voice")) if offices else None

            people.append({
                "official_name": clean(person.get("name")),
                "level":         "state",
                "office_title":  title,
                "office_category": category,
                "party":         clean(person.get("party")),
                "district":      clean(str(role["district"])) if role.get("district") is not None else None,
                "email":         clean(person.get("email")),
                "phone":         phone,
            })

        pagination = data.get("pagination", {})
        if page >= pagination.get("max_page", 1):
            break
        page += 1
        time.sleep(0.25)   # be polite to Open States

    log.info("Open States — fetched %d WA state legislators", len(people))
    return people


# ── Congress.gov ──────────────────────────────────────────────────────────────

def fetch_congress_members(api_key: str) -> list[dict]:
    """
    Return current WA members of Congress from Congress.gov.
    Fetches the member list, then individual detail pages for term/contact info.
    """
    raw_members = _fetch_congress_member_list(api_key)
    members: list[dict] = []

    for raw in raw_members:
        detail = _fetch_congress_member_detail(raw["url"], api_key)
        if not detail:
            detail = {}

        # Prefer directOrderName ("First Last") from detail; fall back to list name.
        name = clean(
            detail.get("directOrderName")
            or last_first_to_first_last(raw.get("name", ""))
        )
        if not name:
            continue

        # Most recent term determines current chamber and dates.
        # Congress.gov returns terms as a plain list in detail and raw.
        def _extract_terms(obj: dict) -> list:
            t = obj.get("terms", [])
            if isinstance(t, dict):
                return t.get("item", [])
            return t if isinstance(t, list) else []

        terms = _extract_terms(detail) or _extract_terms(raw)
        current_term = terms[-1] if terms else {}
        chamber = current_term.get("chamber", "")

        title    = CHAMBER_TITLE.get(chamber)
        category = CHAMBER_CATEGORY.get(chamber)
        if not title:
            continue   # skip if we can't determine the chamber

        district = clean(str(raw["district"])) if raw.get("district") else None
        term_end_year = current_term.get("endYear")
        term_end = f"Jan {term_end_year}" if term_end_year else None

        members.append({
            "official_name":   name,
            "level":           "federal",
            "office_title":    title,
            "office_category": category,
            "party":           clean(raw.get("partyName") or detail.get("partyName")),
            "district":        district,
            "term_start":      clean(str(current_term["startYear"])) if current_term.get("startYear") else None,
            "term_end":        term_end,
            "official_website": clean(detail.get("officialUrl")),
        })

        time.sleep(0.5)  # Congress.gov rate limit headroom

    log.info("Congress.gov — fetched %d WA federal legislators", len(members))
    return members


def _fetch_congress_member_list(api_key: str) -> list[dict]:
    """Paginate through /member?stateCode=WA and return raw member dicts."""
    members: list[dict] = []
    offset = 0
    limit  = 250

    while True:
        resp = requests.get(
            f"{CONGRESS_BASE}/member",
            params={
                "stateCode":     "WA",
                "currentMember": "true",
                "limit":         limit,
                "offset":        offset,
                "format":        "json",
                "api_key":       api_key,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("members", [])
        members.extend(batch)

        pagination = data.get("pagination", {})
        total = pagination.get("count", len(members))
        if offset + limit >= total or not batch:
            break
        offset += limit

    # Congress.gov ignores stateCode in the list endpoint — filter client-side.
    return [m for m in members if m.get("state") == "Washington"]


def _fetch_congress_member_detail(member_url: str, api_key: str) -> dict:
    """Fetch the individual member detail page; return the 'member' dict."""
    try:
        resp = requests.get(
            member_url,
            params={"format": "json", "api_key": api_key},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("member", {})
    except Exception as exc:
        log.warning("Could not fetch member detail from %s: %s", member_url, exc)
        return {}


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
        sys.exit("ERROR: Washington state row not found in Supabase. Run seed.py first.")
    return res.data["id"]


def get_existing_officials(supabase: Client, level: str) -> dict[str, dict]:
    """
    Return {official_name: row_dict} for all officials at the given level.
    Includes inactive officials so we can reactivate if they return.
    """
    res = (
        supabase.table("officials")
        .select("id,official_name,party,term_end,phone,email,office_title,district,official_website,is_active")
        .eq("level", level)
        .execute()
    )
    return {r["official_name"]: r for r in (res.data or [])}


# ── Core sync logic ───────────────────────────────────────────────────────────

def sync_officials(
    supabase: Client,
    wa_id: str,
    incoming: list[dict],
    level: str,
) -> None:
    """
    Reconcile `incoming` (from APIs) against the DB for `level`.

    - INSERT   rows not in DB
    - UPDATE   rows where any MUTABLE_FIELD changed
    - INACTIVE rows in DB but not in any incoming record
    """
    existing = get_existing_officials(supabase, level)
    incoming_by_name = {r["official_name"]: r for r in incoming if r.get("official_name")}

    inserted = updated = flagged = 0
    now = ts()

    # ── INSERT or UPDATE ──────────────────────────────────────────────────────
    for name, api_row in incoming_by_name.items():
        db_row = existing.get(name)

        if db_row is None:
            # New official — insert
            payload = {
                "state_id":      wa_id,
                "level":         level,
                "is_active":     True,
                **{k: api_row.get(k) for k in (
                    "official_name", "office_title", "office_category",
                    "party", "district", "term_start", "term_end",
                    "phone", "email", "official_website",
                )},
            }
            supabase.table("officials").insert(payload).execute()
            log.info("[%s] INSERT  %-6s  %s  —  %s", now, level, name, api_row.get("office_title"))
            inserted += 1
            continue

        # Existing official — check for changes
        changes: dict = {}
        for field in MUTABLE_FIELDS - {"is_active"}:
            new_val = clean(api_row.get(field))
            old_val = clean(db_row.get(field))
            if new_val != old_val and new_val is not None:
                changes[field] = new_val

        # Reactivate if previously flagged inactive
        if not db_row.get("is_active", True):
            changes["is_active"] = True

        if changes:
            supabase.table("officials").update(changes).eq("id", db_row["id"]).execute()
            log.info("[%s] UPDATE  %-6s  %s  —  %s", now, level, name, changes)
            updated += 1

    # ── FLAG INACTIVE ─────────────────────────────────────────────────────────
    for name, db_row in existing.items():
        if name not in incoming_by_name and db_row.get("is_active", True):
            supabase.table("officials").update({"is_active": False}).eq("id", db_row["id"]).execute()
            log.info("[%s] INACTIVE %-6s  %s", now, level, name)
            flagged += 1

    log.info(
        "%s sync complete — %d inserted, %d updated, %d flagged inactive",
        level.upper(), inserted, updated, flagged,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    supabase_url  = os.environ.get("SUPABASE_URL")
    supabase_key  = os.environ.get("SUPABASE_SERVICE_KEY")
    openstates_key = os.environ.get("OPENSTATES_API_KEY")
    congress_key  = os.environ.get("CONGRESS_API_KEY")

    missing = [
        name for name, val in {
            "SUPABASE_URL":         supabase_url,
            "SUPABASE_SERVICE_KEY": supabase_key,
            "OPENSTATES_API_KEY":   openstates_key,
            "CONGRESS_API_KEY":     congress_key,
        }.items() if not val
    ]
    if missing:
        sys.exit(f"ERROR: missing environment variables: {', '.join(missing)}")

    log.info("=== CommuneUSA data-sync starting ===")

    supabase: Client = create_client(supabase_url, supabase_key)
    wa_id = get_wa_state_id(supabase)
    log.info("Connected to Supabase — WA state id: %s", wa_id)

    # ── State legislators (Open States) ──────────────────────────────────────
    state_legislators = fetch_openstates_legislators(openstates_key)
    sync_officials(supabase, wa_id, state_legislators, "state")

    # ── Federal legislators (Congress.gov) ───────────────────────────────────
    federal_members = fetch_congress_members(congress_key)
    sync_officials(supabase, wa_id, federal_members, "federal")

    log.info("=== data-sync complete ===")


if __name__ == "__main__":
    main()
