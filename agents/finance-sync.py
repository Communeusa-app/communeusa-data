"""
CommuneUSA Campaign Finance Sync Agent

Data sources:
  PDC  — WA Public Disclosure Commission (data.wa.gov)
         Itemized contributions to WA candidates across all jurisdiction types:
           Legislative — state House and Senate races
           Local       — city council, mayor, and other local races
           Statewide   — governor, AG, secretary of state, and other statewide offices
           Judicial    — judges and court races
         Free API, no key required. 1 000 records per page.

  FEC  — Federal Election Commission (api.fec.gov)
         Schedule A itemized contributions to WA federal candidates.
         Requires FEC_API_KEY (free at api.data.gov/signup).

Flow:
  1. Build name lookup (officials table → lowercase name → id).
  2. For each PDC jurisdiction type × election year (2023–2026), page through
     contributions and match filer names to officials.
  3. Fetch WA federal candidates from FEC; for each, pull Schedule A.
  4. Load existing (official_id, donor_name, amount, donation_date) keys.
  5. Insert new rows into campaign_finance, skipping duplicates.

Required env vars (.env):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
    FEC_API_KEY          (optional — FEC sync skipped if absent)

Run:
    pip install supabase requests python-dotenv
    python3 agents/finance-sync.py
"""

import logging
import os
import re
import sys
import time
from datetime import date, datetime, timezone
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
log = logging.getLogger("finance-sync")

# ── Constants ──────────────────────────────────────────────────────────────────

PDC_BASE          = "https://data.wa.gov/resource/kv7h-kjye.json"
FEC_BASE          = "https://api.fec.gov/v1"

PDC_PAGE_SIZE     = 1000
FEC_PAGE_SIZE     = 100
PDC_DELAY         = 0.25   # seconds between PDC pages
FEC_DELAY         = 0.5    # seconds between FEC requests

# Election cycles to pull. PDC uses individual years; FEC uses 2-year periods.
# 2023 is included because many WA local races (city council, mayor) are odd-year.
PDC_YEARS             = ("2023", "2024", "2025", "2026")
PDC_JURISDICTION_TYPES = ("Legislative", "Local", "Statewide", "Judicial")
FEC_CYCLES            = (2024, 2026)

BATCH_SIZE        = 200    # rows per Supabase insert


# ── Name helpers ───────────────────────────────────────────────────────────────

# PDC filer_name format: "LAST FIRST M (Preferred Name)"
# Extract the parenthetical preferred name when present.
_PDC_PAREN_RE = re.compile(r"\(([^)]+)\)")

def pdc_preferred_name(raw: str) -> str:
    """Return the parenthetical preferred name, or title-case the raw name."""
    m = _PDC_PAREN_RE.search(raw)
    if m:
        return m.group(1).strip()
    return raw.strip().title()


def fec_normalize_name(raw: str) -> str:
    """Convert 'LAST, FIRST MIDDLE' → 'First Last'."""
    raw = raw.strip()
    if "," in raw:
        last, rest = raw.split(",", 1)
        parts = rest.strip().split()
        # drop single-letter middle initials
        first_parts = [p for p in parts if len(p) > 1 or not p.isalpha()]
        first = " ".join(first_parts[:1])  # keep only first name
        return f"{first.title()} {last.title()}".strip()
    return raw.title()


def clean(val) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def parse_date(raw) -> Optional[str]:
    """Return ISO date string (YYYY-MM-DD) from various formats."""
    if not raw:
        return None
    s = str(raw).strip()
    # PDC: "2025-09-15T00:00:00.000"
    try:
        return datetime.fromisoformat(s.split("T")[0]).date().isoformat()
    except (ValueError, AttributeError):
        return s[:10] if len(s) >= 10 else s


def parse_amount(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# ── Supabase helpers ───────────────────────────────────────────────────────────

def build_name_lookup(supabase: Client) -> dict[str, Optional[str]]:
    """Return lowercase name → official_id for all active officials.
    Identical duplicates (same level + title) resolve to the first row.
    Genuinely different people with the same name resolve to None.
    """
    res = (
        supabase.table("officials")
        .select("id,official_name,level,office_title")
        .eq("is_active", True)
        .execute()
    )
    candidates: dict[str, list[tuple[str, str, str]]] = {}
    for row in res.data or []:
        key = (row["official_name"] or "").strip().lower()
        if not key:
            continue
        entry = (row["id"], row.get("level", ""), row.get("office_title") or "")
        candidates.setdefault(key, []).append(entry)

    lookup: dict[str, Optional[str]] = {}
    for key, entries in candidates.items():
        if len(entries) == 1:
            lookup[key] = entries[0][0]
        else:
            unique_profiles = {(lvl, title) for _, lvl, title in entries}
            if len(unique_profiles) == 1:
                lookup[key] = entries[0][0]
            else:
                log.warning("Ambiguous name (different profiles): %r — skipping", key)
                lookup[key] = None
    return lookup


def load_existing_keys(supabase: Client) -> set[tuple]:
    """Return all (official_id, donor_name, amount, donation_date) tuples in DB."""
    page, size = 0, 1000
    keys: set[tuple] = set()
    while True:
        res = (
            supabase.table("campaign_finance")
            .select("official_id,donor_name,amount,donation_date")
            .range(page * size, (page + 1) * size - 1)
            .execute()
        )
        for r in res.data or []:
            keys.add((r["official_id"], r["donor_name"], r["amount"], r["donation_date"]))
        if len(res.data or []) < size:
            break
        page += 1
    log.info("Loaded %d existing campaign_finance keys", len(keys))
    return keys


def batch_insert(supabase: Client, rows: list[dict], source: str) -> int:
    """Insert rows in chunks; return count inserted."""
    inserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i : i + BATCH_SIZE]
        supabase.table("campaign_finance").upsert(
            chunk, on_conflict="official_id,donor_name,amount,donation_date",
            ignore_duplicates=True,
        ).execute()
        inserted += len(chunk)
        log.info("[%s] inserted chunk %d–%d (%d rows)",
                 source, i + 1, i + len(chunk), len(chunk))
    return inserted


# ── PDC sync ───────────────────────────────────────────────────────────────────

def fetch_pdc_contributions(
    name_lookup: dict[str, Optional[str]],
    existing: set[tuple],
    jurisdiction_type: str,
) -> list[dict]:
    """
    Page through PDC contributions for a single jurisdiction_type across all
    PDC_YEARS. Returns new rows not already in `existing`; mutates `existing`
    so subsequent calls for other jurisdiction types don't re-insert the same
    records.
    """
    rows: list[dict] = []
    unmatched: set[str] = set()
    tag = f"PDC/{jurisdiction_type}"

    for year in PDC_YEARS:
        offset = 0
        page_num = 0
        log.info("[%s] fetching election_year=%s …", tag, year)

        while True:
            params = {
                "$limit":             PDC_PAGE_SIZE,
                "$offset":            offset,
                "type":               "Candidate",
                "jurisdiction_type":  jurisdiction_type,
                "election_year":      year,
                "$order":             "id ASC",
            }
            try:
                resp = requests.get(PDC_BASE, params=params, timeout=30)
                resp.raise_for_status()
                page = resp.json()
            except Exception as exc:
                log.error("[%s] request failed (year=%s offset=%d): %s",
                          tag, year, offset, exc)
                break

            if not page:
                break

            page_num += 1
            matched_this_page = 0

            for rec in page:
                raw_name = rec.get("filer_name") or ""
                preferred = pdc_preferred_name(raw_name)
                key = preferred.lower()

                if key not in name_lookup:
                    unmatched.add(preferred)
                    continue

                official_id = name_lookup[key]
                if official_id is None:
                    continue

                donor_name    = clean(rec.get("contributor_name"))
                amount        = parse_amount(rec.get("amount"))
                donation_date = parse_date(rec.get("receipt_date"))

                dedup_key = (official_id, donor_name, amount, donation_date)
                if dedup_key in existing:
                    continue

                source_url = None
                url_field = rec.get("url")
                if isinstance(url_field, dict):
                    source_url = url_field.get("url")
                elif isinstance(url_field, str):
                    source_url = url_field

                rows.append({
                    "official_id":     official_id,
                    "donor_name":      donor_name,
                    "donor_type":      clean(rec.get("contributor_category")),
                    "amount":          amount,
                    "election_cycle":  clean(rec.get("election_year")),
                    "donation_date":   donation_date,
                    "industry_sector": clean(rec.get("code")),
                    "source_url":      source_url,
                    "filing_source":   "PDC",
                })
                existing.add(dedup_key)
                matched_this_page += 1

            log.info("[%s] year=%s page=%d  fetched=%d  new=%d  running_total=%d",
                     tag, year, page_num, len(page), matched_this_page, len(rows))

            if len(page) < PDC_PAGE_SIZE:
                break
            offset += PDC_PAGE_SIZE
            time.sleep(PDC_DELAY)

    if unmatched:
        log.info("[%s] %d unmatched filer names (not in officials table)",
                 tag, len(unmatched))
        for name in sorted(unmatched)[:20]:
            log.debug("[%s] unmatched: %r", tag, name)

    log.info("[%s] collected %d new rows", tag, len(rows))
    return rows


# ── FEC sync ───────────────────────────────────────────────────────────────────

def fetch_fec_candidates(api_key: str) -> list[dict]:
    """Return all active WA federal candidates (House + Senate) from FEC."""
    candidates = []
    for office in ("H", "S"):
        for cycle in FEC_CYCLES:
            url = f"{FEC_BASE}/candidates/"
            params = {
                "state":        "WA",
                "office":       office,
                "election_year": cycle,
                "per_page":     100,
                "page":         1,
                "api_key":      api_key,
            }
            while True:
                try:
                    resp = requests.get(url, params=params, timeout=30)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    log.error("[FEC] candidates request failed: %s", exc)
                    break

                for cand in data.get("results", []):
                    committees = cand.get("principal_committees") or []
                    if not committees:
                        continue
                    candidates.append({
                        "name":         cand.get("name", ""),
                        "committee_id": committees[0].get("committee_id"),
                        "cycle":        cycle,
                    })

                pagination = data.get("pagination", {})
                if params["page"] >= pagination.get("pages", 1):
                    break
                params["page"] += 1
                time.sleep(FEC_DELAY)

    # deduplicate by committee_id
    seen_cmte: set[str] = set()
    unique = []
    for c in candidates:
        if c["committee_id"] and c["committee_id"] not in seen_cmte:
            seen_cmte.add(c["committee_id"])
            unique.append(c)
    log.info("[FEC] found %d unique WA federal candidate committees", len(unique))
    return unique


def fetch_fec_schedule_a(
    candidate: dict,
    api_key: str,
    name_lookup: dict[str, Optional[str]],
    existing: set[tuple],
) -> list[dict]:
    """Fetch Schedule A (itemized contributions) for one candidate committee."""
    official_name = fec_normalize_name(candidate["name"])
    official_id = name_lookup.get(official_name.lower())

    if official_id is None and official_name.lower() not in name_lookup:
        log.debug("[FEC] no official for %r — skipping", official_name)
        return []
    if official_id is None:
        log.debug("[FEC] ambiguous name %r — skipping", official_name)
        return []

    rows: list[dict] = []
    url = f"{FEC_BASE}/schedules/schedule_a/"
    # Use sort + last_index pagination (FEC deep-pagination pattern)
    last_index = None
    last_date = None
    page_num = 0

    for cycle in FEC_CYCLES:
        last_index = None
        last_date = None
        page_num = 0

        while True:
            params: dict = {
                "committee_id":              candidate["committee_id"],
                "two_year_transaction_period": cycle,
                "per_page":                  FEC_PAGE_SIZE,
                "sort":                      "contribution_receipt_date",
                "sort_hide_null":            True,
                "api_key":                   api_key,
            }
            if last_index:
                params["last_index"] = last_index
                params["last_contribution_receipt_date"] = last_date

            try:
                resp = requests.get(url, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.error("[FEC] schedule_a failed for %s cycle %d: %s",
                          candidate["committee_id"], cycle, exc)
                break

            results = data.get("results", [])
            page_num += 1
            matched_this_page = 0

            for contrib in results:
                donor_name   = clean(contrib.get("contributor_name"))
                amount       = parse_amount(contrib.get("contribution_receipt_amount"))
                donation_date = parse_date(contrib.get("contribution_receipt_date"))

                dedup_key = (official_id, donor_name, amount, donation_date)
                if dedup_key in existing:
                    continue

                rows.append({
                    "official_id":     official_id,
                    "donor_name":      donor_name,
                    "donor_type":      clean(contrib.get("entity_type_desc")),
                    "amount":          amount,
                    "election_cycle":  str(cycle),
                    "donation_date":   donation_date,
                    "industry_sector": None,   # not available at Schedule A level
                    "source_url":      f"https://www.fec.gov/data/receipts/?committee_id={candidate['committee_id']}",
                    "filing_source":   "FEC",
                })
                existing.add(dedup_key)
                matched_this_page += 1

            log.info("[FEC] %s cycle=%d page=%d  fetched=%d  new=%d",
                     official_name, cycle, page_num, len(results), matched_this_page)

            pagination = data.get("pagination", {})
            last_index = pagination.get("last_indexes", {}).get("last_index")
            last_date = pagination.get("last_indexes", {}).get("last_contribution_receipt_date")
            if not last_index or len(results) < FEC_PAGE_SIZE:
                break
            time.sleep(FEC_DELAY)

    return rows


def fetch_fec_contributions(
    api_key: str,
    name_lookup: dict[str, Optional[str]],
    existing: set[tuple],
) -> list[dict]:
    candidates = fetch_fec_candidates(api_key)
    all_rows: list[dict] = []
    for cand in candidates:
        rows = fetch_fec_schedule_a(cand, api_key, name_lookup, existing)
        all_rows.extend(rows)
        time.sleep(FEC_DELAY)
    log.info("[FEC] collected %d new rows total", len(all_rows))
    return all_rows


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    fec_api_key  = os.environ.get("FEC_API_KEY")

    if not supabase_url or not supabase_key:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")

    supabase: Client = create_client(supabase_url, supabase_key)
    log.info("Connected to Supabase")

    name_lookup = build_name_lookup(supabase)
    log.info("Name lookup built: %d officials", len(name_lookup))

    existing = load_existing_keys(supabase)

    # ── PDC — all jurisdiction types ──
    pdc_counts: dict[str, int] = {}
    pdc_total = 0
    for jtype in PDC_JURISDICTION_TYPES:
        rows = fetch_pdc_contributions(name_lookup, existing, jtype)
        inserted = batch_insert(supabase, rows, f"PDC/{jtype}") if rows else 0
        pdc_counts[jtype] = inserted
        pdc_total += inserted
        log.info("[PDC/%s] done — %d rows inserted", jtype, inserted)

    log.info("[PDC] all types complete — %s — total: %d",
             "  ".join(f"{k}: {v}" for k, v in pdc_counts.items()), pdc_total)

    # ── FEC ──
    fec_inserted = 0
    if fec_api_key:
        fec_rows = fetch_fec_contributions(fec_api_key, name_lookup, existing)
        if fec_rows:
            fec_inserted = batch_insert(supabase, fec_rows, "FEC")
        log.info("[FEC] done — %d rows inserted", fec_inserted)
    else:
        log.warning("[FEC] FEC_API_KEY not set — skipping federal sync")
        log.warning("[FEC] get a free key at https://api.data.gov/signup")

    log.info("Sync complete — PDC: %d  FEC: %d  total: %d",
             pdc_total, fec_inserted, pdc_total + fec_inserted)


if __name__ == "__main__":
    main()
