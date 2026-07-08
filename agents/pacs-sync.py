"""
CommuneUSA PAC Sync Agent

Populates four PAC tables from FEC bulk data files (no live API calls):
  - pacs                         — committee master + financials
  - pac_independent_expenditures — Schedule E IEs per committee × candidate
  - pac_contributions            — committee-to-candidate direct contributions
  - pac_donors                   — top individual/org donors to each committee

Data source: FEC bulk downloads at https://www.fec.gov/files/bulk-downloads/{cycle}/

Files used:
  independent_expenditure_{cycle}.csv  — itemized IE records (has header row)
  cm{yy}.zip                           — committee master (pipe-delimited, no header)
  webk{yy}.zip                         — PAC financials summary (pipe-delimited, no header)
  pas2{yy}.zip                         — committee-to-candidate contributions
  indiv{yy}.zip                        — individual contributions to PACs (donors)

Required env vars (.env):
  SUPABASE_URL
  SUPABASE_SERVICE_KEY

Run:
  pip install supabase requests python-dotenv
  python3 agents/pacs-sync.py [--cycle 2024] [--dry-run]

Checkpoint: output/pacs-sync-progress.json — re-run to resume from last completed phase.
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
import zipfile
from datetime import datetime, timezone
from io import TextIOWrapper
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
log = logging.getLogger("pacs-sync")

# ── Constants ──────────────────────────────────────────────────────────────────

FEC_BULK_BASE          = "https://www.fec.gov/files/bulk-downloads"
BATCH_SIZE             = 200
RETRY_ATTEMPTS         = 3
RETRY_DELAY            = 2.0    # seconds between Supabase retry attempts
MAX_DONORS_PER_CMTE    = 200    # top-N donors to keep per committee

OUTPUT_DIR    = Path(__file__).parent.parent / "output"
BULK_DIR      = OUTPUT_DIR / "fec-bulk"
PROGRESS_FILE = OUTPUT_DIR / "pacs-sync-progress.json"

# FEC committee_type code → our CHECK constraint values
COMMITTEE_TYPE_MAP: dict[str, str] = {
    "O": "super_pac",
    "N": "pac",
    "Q": "pac",
    "V": "hybrid_pac",
    "W": "hybrid_pac",
    "X": "party",
    "Y": "party",
    "Z": "party",
}

# FEC entity_type → our donor_type CHECK constraint values
DONOR_TYPE_MAP: dict[str, str] = {
    "IND":  "individual",
    "ORG":  "corporation",
    "CORP": "corporation",
    "LAB":  "union",
    "COM":  "other_pac",
    "CCM":  "other_pac",
    "PAC":  "other_pac",
}

# ── FEC file column indices (pipe-delimited, no header row) ───────────────────

# cm{yy}.txt — committee master
CM_CMTE_ID   = 0
CM_CMTE_NM   = 1
CM_TRES_NM   = 2
CM_CMTE_ST   = 6
CM_CMTE_DSGN = 8
CM_CMTE_TP   = 9

# webk{yy}.txt — PAC summary financials
WK_CMTE_ID      = 0
WK_TTL_RECEIPTS = 5
WK_TTL_DISB     = 12

# itpas2{yy}.txt — committee-to-candidate contributions (Schedule B / 24K)
IT_CMTE_ID         = 0   # contributing PAC
IT_TRANSACTION_TP  = 5   # 24K = direct to candidate, 24Z = to party
IT_ENTITY_TP       = 6
IT_NAME            = 7   # recipient candidate name
IT_TRANSACTION_DT  = 13  # MMDDYYYY
IT_TRANSACTION_AMT = 14
IT_CAND_ID         = 16  # FEC candidate ID

# itcont{yy}.txt — individual contributions to committees (donors)
IC_CMTE_ID         = 0   # receiving PAC
IC_ENTITY_TP       = 6
IC_NAME            = 7   # donor name
IC_EMPLOYER        = 11
IC_TRANSACTION_DT  = 13  # MMDDYYYY
IC_TRANSACTION_AMT = 14


# ── Value helpers ──────────────────────────────────────────────────────────────

def clean(val: object) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def parse_amount(val: object) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_fec_date(raw: object) -> Optional[str]:
    """MMDDYYYY → YYYY-MM-DD. Used by pipe-delimited files."""
    s = clean(raw)
    if not s or len(s) < 8 or not s[:8].isdigit():
        return None
    return f"{s[4:8]}-{s[0:2]}-{s[2:4]}"


def parse_iso_date(raw: object) -> Optional[str]:
    """Accept YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS variants."""
    s = clean(raw)
    if not s:
        return None
    return s[:10] if len(s) >= 10 else s


def parse_fec_mon_date(raw: object) -> Optional[str]:
    """
    Parse the IE CSV date format DD-MON-YY (e.g. '27-SEP-24') → YYYY-MM-DD.
    Also accepts MMDDYYYY (pipe-delimited files) and ISO variants as fallback.
    """
    s = clean(raw)
    if not s:
        return None
    # DD-MON-YY or DD-MON-YYYY (FEC IE CSV)
    try:
        for fmt in ("%d-%b-%y", "%d-%b-%Y"):
            try:
                return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    except Exception:
        pass
    # MMDDYYYY fallback (pipe-delimited files)
    if len(s) == 8 and s.isdigit():
        return f"{s[4:8]}-{s[0:2]}-{s[2:4]}"
    # ISO fallback
    return s[:10] if len(s) >= 10 else None


def map_committee_type(code: Optional[str]) -> str:
    return COMMITTEE_TYPE_MAP.get((code or "").strip().upper(), "other")


def map_donor_type(code: Optional[str]) -> str:
    return DONOR_TYPE_MAP.get((code or "").strip().upper(), "other")


def normalize_name(raw: str) -> str:
    """FEC 'LAST, FIRST MIDDLE' → lowercase 'first last' for matching."""
    raw = raw.strip()
    if "," in raw:
        last, rest = raw.split(",", 1)
        first = rest.strip().split()[0] if rest.strip() else ""
        return f"{first} {last}".lower().strip()
    return raw.lower()


def col(row: list[str], idx: int) -> Optional[str]:
    """Safe positional access for pipe-delimited rows."""
    try:
        return clean(row[idx])
    except IndexError:
        return None


def ie_field(row: dict[str, str], *names: str) -> Optional[str]:
    """Try multiple column name variants (FEC IE CSV headers vary slightly)."""
    for name in names:
        val = row.get(name)
        if val is not None:
            return clean(val)
    return None


# ── Download helpers ───────────────────────────────────────────────────────────

def _yy(cycle: int) -> str:
    """2024 → '24'"""
    return str(cycle)[-2:]


def bulk_files(cycle: int) -> list[tuple[str, Path, bool]]:
    """
    Returns list of (url, local_path, is_zip).
    is_zip=False means the file is already usable as-is (no extraction needed).
    """
    yy = _yy(cycle)
    base = f"{FEC_BULK_BASE}/{cycle}"
    d = BULK_DIR / str(cycle)
    return [
        (f"{base}/independent_expenditure_{cycle}.csv",
         d / f"independent_expenditure_{cycle}.csv",
         False),
        (f"{base}/cm{yy}.zip",     d / f"cm{yy}.zip",     True),
        (f"{base}/webk{yy}.zip",   d / f"webk{yy}.zip",   True),
        (f"{base}/pas2{yy}.zip",   d / f"pas2{yy}.zip",   True),
        (f"{base}/indiv{yy}.zip",   d / f"indiv{yy}.zip",   True),
    ]


def download_file(url: str, dest: Path) -> bool:
    """
    Stream-download url to dest, showing progress every 50 MB.
    Returns True on success, False on HTTP error (logged clearly) or network failure.
    A partial download is removed so a re-run will retry cleanly.
    """
    log.info("Downloading %s → %s", url, dest.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=120) as resp:
            if not resp.ok:
                log.error(
                    "SKIP %s — HTTP %d %s (check FEC filename for cycle)",
                    dest.name, resp.status_code, resp.reason,
                )
                return False
            written = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):  # 1 MB
                    fh.write(chunk)
                    written += len(chunk)
                    if written % (50 << 20) == 0:
                        log.info("  … %.0f MB", written / (1 << 20))
        log.info("  → %.1f MB saved", dest.stat().st_size / (1 << 20))
        return True
    except Exception as exc:
        log.error("SKIP %s — download failed: %s", dest.name, exc)
        if dest.exists():
            dest.unlink()
        return False


def unzip_file(zip_path: Path) -> Path:
    """
    Extract the data file from a FEC bulk zip without assuming the inner filename.

    Strategy:
      1. List all non-directory members.
      2. If exactly one, use it.
      3. If multiple, prefer .txt/.csv files; among those take the largest.
    Logs which member was selected and returns the extracted path.
    """
    with zipfile.ZipFile(zip_path) as zf:
        members = [i for i in zf.infolist() if not i.filename.endswith("/")]
        if not members:
            raise ValueError(f"No files found inside {zip_path.name}")

        if len(members) == 1:
            info = members[0]
        else:
            data_members = [i for i in members
                            if i.filename.lower().endswith((".txt", ".csv"))]
            candidates = data_members if data_members else members
            info = max(candidates, key=lambda i: i.file_size)

        dest = zip_path.parent / Path(info.filename).name
        if dest.exists():
            log.info("Already extracted: %s (from %s)", dest.name, zip_path.name)
            return dest

        log.info("Extracting %s from %s (%.1f MB uncompressed)",
                 info.filename, zip_path.name, info.file_size / (1 << 20))
        zf.extract(info.filename, zip_path.parent)
        extracted = zip_path.parent / info.filename
        # Flatten any subdirectory the zip may have created
        if extracted != dest:
            extracted.rename(dest)
        return dest


def find_extracted(zip_path: Path) -> Optional[Path]:
    """
    Return the already-extracted data file for a zip, or None if not yet extracted.
    Looks for any .txt/.csv sibling whose stem matches the zip stem (case-insensitive),
    or falls back to any .txt/.csv in the same directory that isn't a zip.
    """
    stem = zip_path.stem.lower()
    parent = zip_path.parent
    # Exact-stem match first (e.g. cm24.zip → cm24.txt or CM24.TXT)
    for ext in (".txt", ".csv"):
        candidate = parent / (zip_path.stem + ext)
        if candidate.exists():
            return candidate
        # case-insensitive scan
        for p in parent.iterdir():
            if p.suffix.lower() == ext and p.stem.lower() == stem:
                return p
    return None


def phase_download(cycle: int) -> dict[str, Optional[Path]]:
    """
    Download all needed FEC bulk files, unzip where required.
    Returns mapping of logical name → extracted file path, or None if the
    file could not be downloaded (404 or other error). Callers that need a
    missing file will skip that phase and log a warning.
    """
    files = bulk_files(cycle)
    result: dict[str, Optional[Path]] = {}
    names = ["ie", "cm", "webk", "itpas2", "itcont"]

    for (url, local, is_zip), name in zip(files, names):
        if not local.exists():
            ok = download_file(url, local)
            if not ok:
                result[name] = None
                continue
        else:
            log.info("Already present: %s", local.name)

        if is_zip:
            already = find_extracted(local)
            if already:
                log.info("Already extracted: %s (from %s)", already.name, local.name)
                result[name] = already
            else:
                result[name] = unzip_file(local)
        else:
            result[name] = local

    return result


# ── Supabase retry wrapper ─────────────────────────────────────────────────────

def sb_upsert(supabase: Client, table: str, rows: list[dict],
              on_conflict: str, dry_run: bool) -> int:
    """Upsert rows in BATCH_SIZE chunks with retry. Returns rows upserted."""
    if dry_run:
        log.info("[DRY-RUN] would upsert %d rows into %s", len(rows), table)
        return len(rows)
    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i:i + BATCH_SIZE]
        for attempt in range(RETRY_ATTEMPTS):
            try:
                supabase.table(table).upsert(chunk, on_conflict=on_conflict).execute()
                total += len(chunk)
                break
            except Exception as exc:
                if attempt < RETRY_ATTEMPTS - 1:
                    log.warning("upsert %s attempt %d/%d failed: %s — retrying",
                                table, attempt + 1, RETRY_ATTEMPTS, exc)
                    time.sleep(RETRY_DELAY)
                else:
                    log.error("upsert %s failed after %d attempts: %s",
                              table, RETRY_ATTEMPTS, exc)
    return total


def sb_delete_then_insert(supabase: Client, table: str, rows: list[dict],
                          match_col: str, match_val: str,
                          cycle: int, dry_run: bool) -> int:
    """Delete existing rows for (match_col, cycle) then batch-insert. Returns count."""
    if dry_run:
        log.info("[DRY-RUN] would delete+insert %d rows into %s where %s=%s",
                 len(rows), table, match_col, match_val)
        return len(rows)
    try:
        supabase.table(table).delete().eq(match_col, match_val).eq("cycle", cycle).execute()
    except Exception as exc:
        log.warning("delete from %s where %s=%s failed: %s", table, match_col, match_val, exc)

    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i:i + BATCH_SIZE]
        for attempt in range(RETRY_ATTEMPTS):
            try:
                supabase.table(table).insert(chunk).execute()
                total += len(chunk)
                break
            except Exception as exc:
                if attempt < RETRY_ATTEMPTS - 1:
                    log.warning("insert %s attempt %d/%d: %s — retrying",
                                table, attempt + 1, RETRY_ATTEMPTS, exc)
                    time.sleep(RETRY_DELAY)
                else:
                    log.error("insert %s failed after %d attempts: %s",
                              table, RETRY_ATTEMPTS, exc)
    return total


# ── Official name lookup ───────────────────────────────────────────────────────

def build_official_lookup(supabase: Client) -> dict[str, str]:
    """Return {normalized_name: official_id} for all active federal officials."""
    res = (supabase.table("officials")
           .select("id,official_name")
           .eq("level", "federal")
           .eq("is_active", True)
           .execute())
    lookup: dict[str, str] = {}
    for row in res.data or []:
        key = normalize_name(row.get("official_name") or "")
        if key:
            lookup[key] = row["id"]
    log.info("Official lookup: %d federal officials", len(lookup))
    return lookup


# ── Phase 1: Parse independent expenditures ───────────────────────────────────

def phase_ies(ie_path: Path, cycle: int, official_lookup: dict[str, str],
              supabase: Client, dry_run: bool) -> tuple[dict[str, list[dict]], set[str]]:
    """
    Parse the IE CSV (has header row; columns confirmed from FEC 2024 file):
      spe_id   — committee/spender ID (the PAC making the expenditure)
      spe_nam  — committee/spender name
      cand_id  — FEC candidate ID
      cand_name — candidate name ("LAST, FIRST" format)
      sup_opp  — S (support) or O (oppose)
      exp_amo  — expenditure amount
      exp_date — expenditure date (DD-MON-YY, e.g. "27-SEP-24"; may be empty)
      dissem_dt — dissemination date, used as fallback when exp_date is empty

    Returns:
      - ie_rows_by_committee: {spe_id: [pac_ie row dicts]}  (pac_id filled later)
      - committee_ids: set of all spe_id values seen
    """
    log.info("[IEs] parsing %s", ie_path.name)
    ie_rows_by_committee: dict[str, list[dict]] = {}
    skipped = 0
    total = 0
    debug_printed = 0

    with open(ie_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        log.info("[IEs] header columns: %s", list(reader.fieldnames or []))

        for row in reader:
            total += 1

            # spe_id is the committee making the independent expenditure
            cmte_id = ie_field(row, "spe_id", "committee_id", "CMTE_ID")
            if not cmte_id:
                skipped += 1
                continue

            so_raw = ie_field(row, "sup_opp", "support_oppose_indicator", "SUPOPPOSE")
            if so_raw not in ("S", "O"):
                skipped += 1
                continue

            cand_id   = ie_field(row, "cand_id", "candidate_id", "CAND_ID")
            cand_name = ie_field(row, "cand_name", "candidate_name", "CAND_NAME") or "Unknown"
            amount    = parse_amount(ie_field(row, "exp_amo", "expenditure_amount",
                                              "total", "TRANSACTION_AMT"))
            # exp_date is DD-MON-YY (e.g. "27-SEP-24"); fall back to dissem_dt
            raw_date  = ie_field(row, "exp_date", "dissem_dt", "expenditure_date",
                                 "dissemination_date")
            exp_date  = parse_fec_mon_date(raw_date)

            official_id = official_lookup.get(normalize_name(cand_name))

            record = {
                "official_id":      official_id,
                "candidate_name":   cand_name,
                "fec_candidate_id": cand_id,
                "amount":           amount or 0,
                "support_oppose":   "support" if so_raw == "S" else "oppose",
                "expenditure_date": exp_date,
                "cycle":            cycle,
                # pac_id filled in phase_pacs after upsert
            }
            ie_rows_by_committee.setdefault(cmte_id, []).append(record)

            if debug_printed < 3:
                log.info("[IEs] sample record %d: cmte=%s cand=%r amt=%s so=%s date=%s",
                         debug_printed + 1, cmte_id, cand_name, amount, so_raw, exp_date)
                debug_printed += 1

    committee_ids = set(ie_rows_by_committee.keys())
    log.info("[IEs] %d raw rows → %d valid, %d committees, %d skipped",
             total, total - skipped, len(committee_ids), skipped)
    return ie_rows_by_committee, committee_ids


# ── Phase 2: Upsert pacs + insert IEs ─────────────────────────────────────────

def load_cm(cm_path: Path) -> dict[str, dict]:
    """Parse committee master into {cmte_id: {name, type, designation, treasurer, state}}."""
    log.info("[CM] loading %s", cm_path.name)
    cm: dict[str, dict] = {}
    with open(cm_path, newline="", encoding="latin-1") as fh:
        for row in csv.reader(fh, delimiter="|"):
            cmte_id = col(row, CM_CMTE_ID)
            if not cmte_id:
                continue
            cm[cmte_id] = {
                "name":          col(row, CM_CMTE_NM) or cmte_id,
                "committee_type": map_committee_type(col(row, CM_CMTE_TP)),
                "designation":   col(row, CM_CMTE_DSGN),
                "treasurer":     col(row, CM_TRES_NM),
                "state":         col(row, CM_CMTE_ST),
            }
    log.info("[CM] loaded %d committees", len(cm))
    return cm


def load_webk(webk_path: Path) -> dict[str, dict]:
    """Parse PAC financials summary into {cmte_id: {total_raised, total_spent}}."""
    log.info("[WEBK] loading %s", webk_path.name)
    webk: dict[str, dict] = {}
    with open(webk_path, newline="", encoding="latin-1") as fh:
        for row in csv.reader(fh, delimiter="|"):
            cmte_id = col(row, WK_CMTE_ID)
            if not cmte_id:
                continue
            webk[cmte_id] = {
                "total_raised": parse_amount(col(row, WK_TTL_RECEIPTS)),
                "total_spent":  parse_amount(col(row, WK_TTL_DISB)),
            }
    log.info("[WEBK] loaded financials for %d committees", len(webk))
    return webk


def phase_pacs(
    committee_ids: set[str],
    cm: dict[str, dict],
    webk: dict[str, dict],
    ie_rows_by_committee: dict[str, list[dict]],
    cycle: int,
    supabase: Client,
    dry_run: bool,
) -> dict[str, str]:
    """
    Upsert one pacs row per committee, then delete+insert its IE rows.
    Returns {fec_committee_id: pacs.id (uuid)}.
    """
    log.info("[PACS] upserting %d committees", len(committee_ids))
    pac_id_map: dict[str, str] = {}
    ie_inserted = 0
    now = datetime.now(timezone.utc).isoformat()

    for cmte_id in sorted(committee_ids):
        info  = cm.get(cmte_id, {})
        fins  = webk.get(cmte_id, {})

        pac_row = {
            "fec_committee_id": cmte_id,
            "name":             info.get("name") or cmte_id,
            "committee_type":   info.get("committee_type") or "other",
            "designation":      info.get("designation"),
            "total_raised":     fins.get("total_raised"),
            "total_spent":      fins.get("total_spent"),
            "cycle":            cycle,
            "treasurer":        info.get("treasurer"),
            "state":            info.get("state"),
            "website":          None,
            "updated_at":       now,
        }

        if dry_run:
            pac_id_map[cmte_id] = f"dry-run-{cmte_id}"
            log.info("[DRY-RUN] would upsert pacs for %s (%s)", cmte_id, pac_row["name"])
            continue

        for attempt in range(RETRY_ATTEMPTS):
            try:
                res = (supabase.table("pacs")
                       .upsert(pac_row, on_conflict="fec_committee_id")
                       .select("id")
                       .single()
                       .execute())
                pac_id = res.data["id"]
                pac_id_map[cmte_id] = pac_id
                break
            except Exception as exc:
                if attempt < RETRY_ATTEMPTS - 1:
                    log.warning("[PACS] upsert %s attempt %d failed: %s — retrying",
                                cmte_id, attempt + 1, exc)
                    time.sleep(RETRY_DELAY)
                else:
                    log.error("[PACS] upsert %s failed: %s", cmte_id, exc)

        if cmte_id not in pac_id_map:
            continue

        pac_id = pac_id_map[cmte_id]
        ie_rows = ie_rows_by_committee.get(cmte_id, [])
        if ie_rows:
            stamped = [{**r, "pac_id": pac_id} for r in ie_rows]
            ie_inserted += sb_delete_then_insert(
                supabase, "pac_independent_expenditures",
                stamped, "pac_id", pac_id, cycle, dry_run,
            )

    log.info("[PACS] upserted %d rows; inserted %d IE rows", len(pac_id_map), ie_inserted)
    return pac_id_map


# ── Phase 3: Committee-to-candidate contributions ─────────────────────────────

def phase_contributions(
    itpas2_path: Path,
    pac_id_map: dict[str, str],
    official_lookup: dict[str, str],
    cycle: int,
    supabase: Client,
    dry_run: bool,
) -> int:
    """
    Parse pas2 file (pipe-delimited, no header) for 24K contributions.
    Column layout (0-based, from FEC data dictionary):
      [0]  CMTE_ID        contributing PAC committee
      [5]  TRANSACTION_TP 24K = direct contribution to candidate committee
      [6]  ENTITY_TP
      [7]  NAME           recipient name (campaign committee or candidate)
      [13] TRANSACTION_DT MMDDYYYY
      [14] TRANSACTION_AMT
      [16] CAND_ID        FEC candidate ID of recipient
    Returns total inserted.
    """
    log.info("[CONTRIBS] parsing %s", itpas2_path.name)
    rows_by_pac: dict[str, list[dict]] = {}
    skipped = total = 0
    debug_row_logged = False
    debug_printed = 0

    with open(itpas2_path, newline="", encoding="latin-1") as fh:
        for row in csv.reader(fh, delimiter="|"):
            total += 1

            if not debug_row_logged:
                log.info("[CONTRIBS] first raw row (%d fields): %s", len(row), row)
                debug_row_logged = True

            cmte_id = col(row, IT_CMTE_ID)
            if cmte_id not in pac_id_map:
                skipped += 1
                continue
            txn_tp = col(row, IT_TRANSACTION_TP)
            if txn_tp != "24K":
                skipped += 1
                continue

            # col[7] is the recipient name (usually a campaign committee name)
            recipient_name = col(row, IT_NAME) or "Unknown"
            cand_id        = col(row, IT_CAND_ID)
            amount         = parse_amount(col(row, IT_TRANSACTION_AMT))
            if not amount:
                skipped += 1
                continue

            pac_id      = pac_id_map[cmte_id]
            # Name-match against officials using the recipient committee name as a
            # best-effort proxy; cand_id lookup would be more accurate but requires
            # an extra FEC candidate file.
            official_id = official_lookup.get(normalize_name(recipient_name))

            record = {
                "pac_id":            pac_id,
                "official_id":       official_id,
                "candidate_name":    recipient_name,
                "fec_candidate_id":  cand_id,
                "amount":            amount,
                "contribution_date": parse_fec_date(col(row, IT_TRANSACTION_DT)),
                "cycle":             cycle,
            }
            rows_by_pac.setdefault(pac_id, []).append(record)

            if debug_printed < 3:
                log.info("[CONTRIBS] sample record %d: cmte=%s recipient=%r cand_id=%s amt=%s",
                         debug_printed + 1, cmte_id, recipient_name, cand_id, amount)
                debug_printed += 1

    log.info("[CONTRIBS] %d raw rows → %d committees with 24K contribs (%d skipped)",
             total, len(rows_by_pac), skipped)

    inserted = 0
    for pac_id, rows in rows_by_pac.items():
        # find the fec_committee_id for logging
        cmte_id = next((k for k, v in pac_id_map.items() if v == pac_id), pac_id)
        inserted += sb_delete_then_insert(
            supabase, "pac_contributions", rows,
            "pac_id", pac_id, cycle, dry_run,
        )
    log.info("[CONTRIBS] inserted %d rows", inserted)
    return inserted


# ── Phase 4: Donors (individual contributions to our PACs) ────────────────────

def phase_donors(
    itcont_path: Path,
    pac_id_map: dict[str, str],
    cycle: int,
    supabase: Client,
    dry_run: bool,
) -> int:
    """
    Stream itcont; collect contributions to our committees; keep top MAX_DONORS_PER_CMTE
    by amount per committee. Returns total inserted.
    """
    log.info("[DONORS] streaming %s (filtering to %d committees)",
             itcont_path.name, len(pac_id_map))

    # Collect all matching contributions in memory, bucketed by pac_id
    buckets: dict[str, list[dict]] = {}
    skipped = total = 0

    with open(itcont_path, newline="", encoding="latin-1") as fh:
        for row in csv.reader(fh, delimiter="|"):
            total += 1
            cmte_id = col(row, IC_CMTE_ID)
            if cmte_id not in pac_id_map:
                skipped += 1
                continue

            amount = parse_amount(col(row, IC_TRANSACTION_AMT))
            if not amount or amount <= 0:
                skipped += 1
                continue

            pac_id = pac_id_map[cmte_id]
            buckets.setdefault(pac_id, []).append({
                "pac_id":            pac_id,
                "donor_name":        col(row, IC_NAME) or "Unknown",
                "donor_type":        map_donor_type(col(row, IC_ENTITY_TP)),
                "donor_employer":    col(row, IC_EMPLOYER),
                "amount":            amount,
                "contribution_date": parse_fec_date(col(row, IC_TRANSACTION_DT)),
                "cycle":             cycle,
            })

            if total % 1_000_000 == 0:
                log.info("[DONORS]  … %dM rows scanned, %d committees matched",
                         total // 1_000_000, len(buckets))

    log.info("[DONORS] scanned %d rows; %d committees have donor data", total, len(buckets))

    inserted = 0
    for pac_id, rows in buckets.items():
        # Keep top MAX_DONORS_PER_CMTE by amount
        top = sorted(rows, key=lambda r: r["amount"] or 0, reverse=True)[:MAX_DONORS_PER_CMTE]
        inserted += sb_delete_then_insert(
            supabase, "pac_donors", top,
            "pac_id", pac_id, cycle, dry_run,
        )
    log.info("[DONORS] inserted %d rows (%d committees)", inserted, len(buckets))
    return inserted


# ── Checkpoint helpers ─────────────────────────────────────────────────────────

def load_progress(cycle: int) -> dict:
    if PROGRESS_FILE.exists():
        try:
            data = json.loads(PROGRESS_FILE.read_text())
            if data.get("cycle") == cycle:
                log.info("Resuming from checkpoint: phases complete = %s",
                         data.get("phases_complete", []))
                return data
            log.info("Checkpoint is for cycle %s, not %d — starting fresh",
                     data.get("cycle"), cycle)
        except Exception as exc:
            log.warning("Could not read progress file: %s — starting fresh", exc)
    return {"cycle": cycle, "phases_complete": [], "committee_ids": []}


def save_progress(progress: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2))


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync FEC PAC bulk data into Supabase")
    parser.add_argument("--cycle", type=int, default=2024,
                        help="FEC two-year cycle (e.g. 2024 or 2026)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and validate without writing to Supabase")
    args = parser.parse_args()

    cycle   = args.cycle
    dry_run = args.dry_run

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")

    log.info("=== pacs-sync starting  cycle=%d  dry_run=%s ===", cycle, dry_run)

    supabase: Client = create_client(supabase_url, supabase_key)
    official_lookup  = build_official_lookup(supabase)
    progress         = load_progress(cycle)
    done             = set(progress.get("phases_complete", []))

    # ── Download ──────────────────────────────────────────────────────────────
    # phase_download is idempotent: skips files already on disk and
    # zips already extracted, so it is safe to call on every run.
    paths = phase_download(cycle)
    if "download" not in done:
        progress["phases_complete"].append("download")
        save_progress(progress)

    # ── Load reference files (always needed regardless of checkpoint) ─────────
    cm   = load_cm(paths["cm"])   if paths.get("cm")   else {}
    webk = load_webk(paths["webk"]) if paths.get("webk") else {}
    if not paths.get("cm"):
        log.warning("Committee master (cm) unavailable — pacs rows will have minimal metadata")
    if not paths.get("webk"):
        log.warning("PAC financials (webk) unavailable — total_raised/total_spent will be null")

    # ── IEs ───────────────────────────────────────────────────────────────────
    if "ies" not in done:
        if not paths.get("ie"):
            log.error("IE file unavailable — cannot determine committee set; aborting")
            sys.exit(1)
        ie_rows_by_committee, committee_ids = phase_ies(
            paths["ie"], cycle, official_lookup, supabase, dry_run,
        )
        progress["committee_ids"]     = sorted(committee_ids)
        progress["phases_complete"].append("ies")
        save_progress(progress)
    else:
        log.info("[IEs] already complete — reloading committee set from checkpoint")
        committee_ids        = set(progress.get("committee_ids", []))
        ie_rows_by_committee = {}  # IEs already inserted; skip re-insert in phase_pacs
        log.info("[IEs] %d committees from checkpoint", len(committee_ids))

    # ── PACs + IE insert ──────────────────────────────────────────────────────
    if "pacs" not in done:
        pac_id_map = phase_pacs(
            committee_ids, cm, webk, ie_rows_by_committee,
            cycle, supabase, dry_run,
        )
        progress["phases_complete"].append("pacs")
        save_progress(progress)
    else:
        log.info("[PACS] already complete — fetching pac_id_map from DB")
        pac_id_map = {}
        if not dry_run:
            res = (supabase.table("pacs")
                   .select("id,fec_committee_id")
                   .eq("cycle", cycle)
                   .in_("fec_committee_id", sorted(committee_ids))
                   .execute())
            for row in res.data or []:
                pac_id_map[row["fec_committee_id"]] = row["id"]
        log.info("[PACS] loaded %d pac_id entries", len(pac_id_map))

    # ── Contributions ─────────────────────────────────────────────────────────
    if "contributions" not in done:
        if not paths.get("itpas2"):
            log.warning("SKIP contributions phase — pas2 file unavailable (download failed)")
        else:
            phase_contributions(
                paths["itpas2"], pac_id_map, official_lookup,
                cycle, supabase, dry_run,
            )
            progress["phases_complete"].append("contributions")
            save_progress(progress)
    else:
        log.info("[CONTRIBS] already complete — skipping")

    # ── Donors ────────────────────────────────────────────────────────────────
    if "donors" not in done:
        if not paths.get("itcont"):
            log.warning("SKIP donors phase — itcont file unavailable (download failed)")
        else:
            phase_donors(paths["itcont"], pac_id_map, cycle, supabase, dry_run)
            progress["phases_complete"].append("donors")
            save_progress(progress)
    else:
        log.info("[DONORS] already complete — skipping")

    log.info("=== pacs-sync complete  cycle=%d  phases=%s ===",
             cycle, progress["phases_complete"])


if __name__ == "__main__":
    main()
