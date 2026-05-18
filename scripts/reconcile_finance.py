"""
CommuneUSA Campaign Finance Reconciliation Script

Finds officials with zero campaign_finance records and attempts to match them
against PDC filings using fuzzy name matching.

Logic:
  - ≥ 80% match confidence → auto-insert PDC rows for that official
  - 50–79% match confidence → log as "manual review needed", no insert
  - < 50% → skip silently

Name variants tried per official:
  - Full name as stored
  - First + Last only (drops middle names/initials)
  - Hyphenated last name variants (e.g. "Smith-Jones" → "Smith", "Jones")

Required env vars (.env in project root):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY

Optional:
    PDC_BASE  (default: https://data.wa.gov/resource/kv7h-kjye.json)

Run:
    pip install supabase requests python-dotenv rapidfuzz
    python3 scripts/reconcile_finance.py [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from supabase import Client, create_client

try:
    from rapidfuzz import fuzz as _fuzz
    def fuzzy_score(a: str, b: str) -> float:
        return _fuzz.token_sort_ratio(a, b)
except ImportError:
    import difflib
    def fuzzy_score(a: str, b: str) -> float:  # type: ignore[misc]
        return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("reconcile-finance")

# ── Constants ──────────────────────────────────────────────────────────────────

PDC_BASE      = os.environ.get("PDC_BASE", "https://data.wa.gov/resource/kv7h-kjye.json")
PDC_PAGE_SIZE = 1000
PDC_DELAY     = 0.25
PDC_YEARS     = ("2023", "2024", "2025", "2026")

AUTO_INSERT_THRESHOLD  = 80.0
MANUAL_REVIEW_THRESHOLD = 50.0

BATCH_SIZE = 200

# ── Name helpers ───────────────────────────────────────────────────────────────

_PDC_PAREN_RE = re.compile(r"\(([^)]+)\)")


def pdc_preferred_name(raw: str) -> str:
    m = _PDC_PAREN_RE.search(raw)
    if m:
        return m.group(1).strip()
    return raw.strip().title()


def name_variants(name: str) -> list[str]:
    """Generate name variants to try against PDC filer names."""
    name = name.strip()
    variants: list[str] = [name]
    parts = name.split()

    if len(parts) >= 3:
        # First + Last only (drops middle name/initial)
        first_last = f"{parts[0]} {parts[-1]}"
        if first_last not in variants:
            variants.append(first_last)

    # Handle hyphenated last name: "Smith-Jones" → try "Smith" and "Jones"
    for part in parts:
        if "-" in part:
            sub_parts = part.split("-")
            for sub in sub_parts:
                candidate = f"{parts[0]} {sub}" if part == parts[-1] else f"{sub} {parts[-1]}"
                if candidate not in variants:
                    variants.append(candidate)

    return variants


def clean(val) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def parse_date(raw) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).strip()
    try:
        from datetime import datetime
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


# ── PDC search ─────────────────────────────────────────────────────────────────

def search_pdc_by_name(query: str) -> list[dict]:
    """Return all PDC contribution records where filer_name contains `query`."""
    results: list[dict] = []
    for year in PDC_YEARS:
        offset = 0
        while True:
            params = {
                "$limit":      PDC_PAGE_SIZE,
                "$offset":     offset,
                "type":        "Candidate",
                "election_year": year,
                "$q":          query,
                "$order":      "id ASC",
            }
            try:
                resp = requests.get(PDC_BASE, params=params, timeout=30)
                resp.raise_for_status()
                page = resp.json()
            except Exception as exc:
                log.warning("PDC search failed for %r year=%s: %s", query, year, exc)
                break

            results.extend(page)
            if len(page) < PDC_PAGE_SIZE:
                break
            offset += PDC_PAGE_SIZE
            time.sleep(PDC_DELAY)

    return results


# ── Reconciliation logic ───────────────────────────────────────────────────────

def reconcile_official(
    official: dict,
    existing: set[tuple],
    dry_run: bool,
) -> tuple[list[dict], list[dict]]:
    """
    Try to find PDC contributions for an unmatched official.

    Returns:
        (rows_to_insert, manual_review_entries)
    """
    official_id   = official["id"]
    official_name = (official["official_name"] or "").strip()
    variants = name_variants(official_name)

    # Collect all PDC records that mention this official's name variants
    candidate_records: list[tuple[str, dict, float]] = []  # (pdc_name, rec, score)

    seen_ids: set[str] = set()
    for variant in variants:
        pdc_records = search_pdc_by_name(variant)
        for rec in pdc_records:
            rec_id = rec.get("id") or rec.get("record_id") or str(rec)
            if rec_id in seen_ids:
                continue
            seen_ids.add(rec_id)

            raw_filer = rec.get("filer_name") or ""
            pdc_name  = pdc_preferred_name(raw_filer)
            score     = fuzzy_score(official_name, pdc_name)
            candidate_records.append((pdc_name, rec, score))

    if not candidate_records:
        return [], []

    # Best score across all candidates
    best_score = max(score for _, _, score in candidate_records)

    if best_score < MANUAL_REVIEW_THRESHOLD:
        return [], []

    if best_score < AUTO_INSERT_THRESHOLD:
        # Log the best match for manual review
        best_match = max(candidate_records, key=lambda x: x[2])
        return [], [{
            "official_name":  official_name,
            "official_id":    official_id,
            "matched_name":   best_match[0],
            "confidence":     round(best_match[2], 1),
        }]

    # ≥ 80% — collect all records from filer names that meet threshold
    rows_to_insert: list[dict] = []
    for pdc_name, rec, score in candidate_records:
        if score < AUTO_INSERT_THRESHOLD:
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

        row = {
            "official_id":     official_id,
            "donor_name":      donor_name,
            "donor_type":      clean(rec.get("contributor_category")),
            "amount":          amount,
            "election_cycle":  clean(rec.get("election_year")),
            "donation_date":   donation_date,
            "industry_sector": clean(rec.get("code")),
            "source_url":      source_url,
            "filing_source":   "PDC",
        }
        rows_to_insert.append(row)
        existing.add(dedup_key)

        log.info(
            "MATCH  official=%r  pdc_name=%r  score=%.1f  donor=%r  amount=%s",
            official_name, pdc_name, score, donor_name, amount,
        )

    return rows_to_insert, []


# ── Supabase helpers ───────────────────────────────────────────────────────────

def get_unmatched_officials(supabase: Client) -> list[dict]:
    """Return active officials that have zero rows in campaign_finance."""
    res = (
        supabase.table("officials")
        .select("id,official_name,level,office_title")
        .eq("is_active", True)
        .execute()
    )
    all_officials = res.data or []

    # Load all official_ids that already have finance data
    page, size = 0, 1000
    funded_ids: set[str] = set()
    while True:
        res2 = (
            supabase.table("campaign_finance")
            .select("official_id")
            .range(page * size, (page + 1) * size - 1)
            .execute()
        )
        for r in res2.data or []:
            funded_ids.add(r["official_id"])
        if len(res2.data or []) < size:
            break
        page += 1

    unmatched = [o for o in all_officials if o["id"] not in funded_ids]
    log.info(
        "%d total officials, %d have finance data, %d unmatched",
        len(all_officials), len(funded_ids), len(unmatched),
    )
    return unmatched


def load_existing_keys(supabase: Client) -> set[tuple]:
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


def batch_insert(supabase: Client, rows: list[dict]) -> int:
    inserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i : i + BATCH_SIZE]
        supabase.table("campaign_finance").upsert(
            chunk, on_conflict="official_id,donor_name,amount,donation_date",
            ignore_duplicates=True,
        ).execute()
        inserted += len(chunk)
    return inserted


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile missing campaign finance records")
    parser.add_argument("--dry-run", action="store_true", help="Find matches but don't insert")
    args = parser.parse_args()

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")

    supabase: Client = create_client(supabase_url, supabase_key)
    log.info("Connected to Supabase%s", " (DRY RUN)" if args.dry_run else "")

    unmatched = get_unmatched_officials(supabase)
    if not unmatched:
        log.info("All officials already have finance data — nothing to do")
        return

    existing = load_existing_keys(supabase)

    all_rows: list[dict] = []
    manual_review: list[dict] = []
    skipped = 0

    for official in unmatched:
        rows, review = reconcile_official(official, existing, dry_run=args.dry_run)
        if rows:
            all_rows.extend(rows)
            log.info(
                "WILL INSERT  official=%r  rows=%d%s",
                official["official_name"], len(rows),
                "  [dry-run]" if args.dry_run else "",
            )
        elif review:
            manual_review.extend(review)
        else:
            skipped += 1

    log.info(
        "Reconciliation complete — auto-insert: %d rows  manual-review: %d  skipped: %d",
        len(all_rows), len(manual_review), skipped,
    )

    if manual_review:
        log.info("── MANUAL REVIEW NEEDED ──────────────────────────")
        for entry in manual_review:
            log.info(
                "  official=%r  best_match=%r  confidence=%.1f%%",
                entry["official_name"], entry["matched_name"], entry["confidence"],
            )

    if all_rows and not args.dry_run:
        inserted = batch_insert(supabase, all_rows)
        log.info("Inserted %d rows into campaign_finance", inserted)
    elif all_rows and args.dry_run:
        log.info("[dry-run] would insert %d rows", len(all_rows))


if __name__ == "__main__":
    main()
