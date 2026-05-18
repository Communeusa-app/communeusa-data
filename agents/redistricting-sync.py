"""
Redistricting monitor agent for Washington State.

Detects redistricting changes by:
  1. Checking for newer Census TIGER/Line shapefiles (primary signal)
  2. Monitoring All About Redistricting (redistricting.lls.edu/states)
  3. Monitoring Ballotpedia's WA redistricting page

When a TIGER file change is detected:
  - Downloads the new shapefile zip
  - Converts to GeoJSON using geopandas
  - Diffs geometry hashes feature-by-feature to find changed districts
  - Writes changed districts to Supabase (redistricted_districts table)
  - Copies updated GeoJSON to communeusa-web/public/districts/

State is tracked in output/redistricting-status.json.

Usage:
    python3 agents/redistricting-sync.py [--force] [--dry-run]
      --force      Re-check even if recently checked
      --dry-run    Detect changes but do not write to DB or copy files

Environment (.env):
    SUPABASE_URL, SUPABASE_SERVICE_KEY
    WEB_PUBLIC_DIR   Optional override for communeusa-web/public path
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    import geopandas as gpd
    HAS_GEOPANDAS = True
except ImportError:
    HAS_GEOPANDAS = False

load_dotenv(Path(__file__).parent.parent / ".env")

from supabase import create_client, Client

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────

DATA_ROOT  = Path(__file__).parent.parent
OUT_DIR    = DATA_ROOT / "output" / "districts"
STATUS_FILE = DATA_ROOT / "output" / "redistricting-status.json"

# Auto-detect communeusa-web/public; overridable via env
_web_env = os.environ.get("WEB_PUBLIC_DIR")
WEB_PUBLIC = Path(_web_env) if _web_env else DATA_ROOT.parent / "communeusa-web" / "public"

# ── TIGER config ───────────────────────────────────────────────────────────────

WA_FIPS = "53"

# Maps district_type → Census URL template, shapefile name, GeoJSON output name,
# the GeoJSON property that carries the district number, and simplification tolerance.
TIGER_CONFIG: dict[str, dict] = {
    "congressional": {
        "url_template": "https://www2.census.gov/geo/tiger/TIGER{year}/CD/tl_{year}_{fips}_cd{congress}.zip",
        "shapefile_template": "tl_{year}_{fips}_cd{congress}.shp",
        "output": "congressional.geojson",
        "num_key": "CD119FP",  # updated below when year changes
        "tolerance": 0.01,
    },
    "house": {
        "url_template": "https://www2.census.gov/geo/tiger/TIGER{year}/SLDL/tl_{year}_{fips}_sldl.zip",
        "shapefile_template": "tl_{year}_{fips}_sldl.shp",
        "output": "house_districts_wa.geojson",
        "num_key": "SLDLST",
        "tolerance": 0.005,
    },
    "senate": {
        "url_template": "https://www2.census.gov/geo/tiger/TIGER{year}/SLDU/tl_{year}_{fips}_sldu.zip",
        "shapefile_template": "tl_{year}_{fips}_sldu.shp",
        "output": "senate_districts_wa.geojson",
        "num_key": "SLDUST",
        "tolerance": 0.005,
    },
}

# 119th Congress sits 2025-2027; adjust here as new congresses begin.
CONGRESS_FOR_YEAR: dict[int, int] = {
    2024: 119, 2025: 119, 2026: 119, 2027: 120,
}

# ── Monitoring sources ─────────────────────────────────────────────────────────

ALL_ABOUT_REDISTRICTING_URL = "https://redistricting.lls.edu/states2"
BALLOTPEDIA_WA_URL = "https://ballotpedia.org/Redistricting_in_Washington"

# Re-check interval in hours when no change detected
CHECK_INTERVAL_HOURS = 24

# ── Supabase ───────────────────────────────────────────────────────────────────

def make_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
    return create_client(url, key)

# ── Status file ────────────────────────────────────────────────────────────────

def load_status() -> dict:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except Exception:
            pass
    return {
        "schema_version": 1,
        "tiger": {
            "congressional": {"url": None, "content_length": None, "geojson_sha256": None, "last_checked": None, "last_changed": None},
            "house":         {"url": None, "content_length": None, "geojson_sha256": None, "last_checked": None, "last_changed": None},
            "senate":        {"url": None, "content_length": None, "geojson_sha256": None, "last_checked": None, "last_changed": None},
        },
        "sources": {
            "all_about_redistricting": {"last_checked": None, "wa_hash": None, "last_changed": None},
            "ballotpedia_wa":           {"last_checked": None, "content_hash": None, "last_changed": None},
        },
        "changes": [],
    }

def save_status(status: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status, indent=2, default=str))

# ── HTTP helpers ───────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = (
        "CommuneUSA-RedistrictingMonitor/1.0 "
        "(civic data platform; contact: communeusa.app)"
    )
    return s

def safe_get(session: requests.Session, url: str, delay: float = 1.5, timeout: int = 60, **kwargs) -> Optional[requests.Response]:
    time.sleep(delay)
    try:
        r = session.get(url, timeout=timeout, **kwargs)
        r.raise_for_status()
        return r
    except Exception as e:
        log.warning(f"GET {url}: {e}")
        return None

def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"

# ── TIGER helpers ──────────────────────────────────────────────────────────────

def resolve_tiger_url(session: requests.Session, district_type: str) -> tuple:
    """
    Find the newest available Census TIGER URL for the given district type.
    Scans from current year down to 2024, returns (url, shapefile_name) for the
    first year that returns HTTP 200.
    """
    cfg = TIGER_CONFIG[district_type]
    current_year = datetime.datetime.now().year

    for year in range(current_year, 2023, -1):
        congress = CONGRESS_FOR_YEAR.get(year, 119)
        url = cfg["url_template"].format(year=year, fips=WA_FIPS, congress=congress)
        shp = cfg["shapefile_template"].format(year=year, fips=WA_FIPS, congress=congress)
        try:
            time.sleep(0.5)
            r = session.head(url, timeout=20)
            if r.status_code == 200:
                return url, shp
        except Exception:
            continue
    return None, None

def get_content_length(session: requests.Session, url: str) -> Optional[int]:
    try:
        time.sleep(0.5)
        r = session.head(url, timeout=20)
        length = r.headers.get("Content-Length")
        return int(length) if length else None
    except Exception:
        return None

def download_tiger_zip(session: requests.Session, url: str) -> Optional[bytes]:
    log.info(f"  Downloading {url.split('/')[-1]} …")
    r = safe_get(session, url, delay=1.0, timeout=180)
    if not r:
        return None
    mb = len(r.content) / 1_048_576
    log.info(f"  Downloaded {mb:.1f} MB")
    return r.content

def convert_to_geojson(zip_bytes: bytes, shapefile_name: str, tolerance: float, out_path: Path) -> bool:
    if not HAS_GEOPANDAS:
        log.error("geopandas not installed — cannot convert shapefile")
        return False
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                zf.extractall(tmp)
            shp_path = Path(tmp) / shapefile_name
            gdf = gpd.read_file(shp_path)
            gdf = gdf.to_crs(epsg=4326)
            gdf["geometry"] = gdf["geometry"].simplify(tolerance, preserve_topology=True)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            gdf.to_file(out_path, driver="GeoJSON")
        return True
    except Exception as e:
        log.error(f"Conversion error: {e}")
        return False

def geojson_feature_hashes(geojson_path: Path, num_key: str) -> dict[str, str]:
    """
    Returns {district_number: geometry_hash} for each feature in the GeoJSON.
    District numbers have leading zeros stripped.
    """
    if not geojson_path.exists():
        return {}
    try:
        gj = json.loads(geojson_path.read_text())
        result = {}
        for feat in gj.get("features", []):
            props = feat.get("properties") or {}
            raw_num = str(props.get(num_key, "")).strip()
            if not raw_num:
                continue
            num = str(int(raw_num))  # strip leading zeros
            geom_bytes = json.dumps(feat.get("geometry"), sort_keys=True).encode()
            result[num] = sha256_of_bytes(geom_bytes)
        return result
    except Exception as e:
        log.warning(f"Error reading {geojson_path}: {e}")
        return {}

# ── Source scrapers ────────────────────────────────────────────────────────────

def scrape_all_about_redistricting(session: requests.Session) -> Optional[str]:
    """
    Scrapes redistricting.lls.edu/states and returns the text of the
    Washington State row as a stable string for change detection.
    """
    r = safe_get(session, ALL_ABOUT_REDISTRICTING_URL, delay=2.0)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    # Look for a table row containing "Washington"
    for row in soup.find_all("tr"):
        text = row.get_text(" ", strip=True)
        if re.search(r"\bwashington\b", text, re.IGNORECASE) and len(text) > 20:
            # Exclude rows that are just headers or contain "West Virginia"
            if re.search(r"\bwest virginia\b", text, re.IGNORECASE):
                continue
            return text
    # Fallback: hash the full page
    return soup.get_text(" ", strip=True)[:4000]

def scrape_ballotpedia_wa(session: requests.Session) -> Optional[str]:
    """
    Fetches the Ballotpedia WA redistricting page and returns a stable
    excerpt of the main content for change detection.
    """
    r = safe_get(session, BALLOTPEDIA_WA_URL, delay=2.0)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    # Target the mw-parser-output div (main wiki content)
    content = soup.find("div", {"class": "mw-parser-output"})
    if not content:
        content = soup.find("div", {"id": "mw-content-text"}) or soup
    # Focus on sections likely to reflect redistricting status changes
    text_parts = []
    for tag in content.find_all(["h2", "h3", "p", "li"]):
        t = tag.get_text(" ", strip=True)
        if t and len(t) > 10:
            text_parts.append(t)
        if len(" ".join(text_parts)) > 8000:
            break
    return " ".join(text_parts)

# ── Supabase operations ────────────────────────────────────────────────────────

def record_changes(sb: Client, changes: list[dict], dry_run: bool) -> None:
    """
    Upserts redistricting change records into redistricted_districts.
    Each change dict: {state, district_type, district_number, changed_date, description, source_url}
    """
    if not changes:
        return
    if dry_run:
        log.info(f"  [dry-run] Would insert {len(changes)} record(s)")
        for c in changes:
            log.info(f"    {c}")
        return
    try:
        res = sb.table("redistricted_districts").upsert(
            changes,
            on_conflict="state,district_type,district_number,changed_date",
        ).execute()
        log.info(f"  Recorded {len(res.data or [])} change(s) in Supabase")
    except Exception as e:
        log.error(f"  Supabase upsert failed: {e}")

# ── TIGER change processing ────────────────────────────────────────────────────

def process_tiger_type(
    district_type: str,
    session: requests.Session,
    sb: Client,
    status: dict,
    dry_run: bool,
    force: bool,
) -> bool:
    """
    Checks and processes TIGER file changes for one district type.
    Returns True if a change was detected and processed.
    """
    cfg = TIGER_CONFIG[district_type]
    tiger_state = status["tiger"][district_type]

    # ── Resolve current TIGER URL ────────────────────────────────────────────
    log.info(f"  [{district_type}] Resolving latest TIGER URL …")
    url, shapefile_name = resolve_tiger_url(session, district_type)
    if not url:
        log.warning(f"  [{district_type}] No TIGER file found — skipping")
        return False

    stored_url = tiger_state.get("url")
    stored_length = tiger_state.get("content_length")

    # ── Check Content-Length for change signal ───────────────────────────────
    content_length = get_content_length(session, url)
    url_changed = (url != stored_url)
    size_changed = (content_length is not None and content_length != stored_length)

    if not force and not url_changed and not size_changed:
        log.info(f"  [{district_type}] No TIGER change detected (url={url.split('/')[-1]}, size={content_length})")
        tiger_state["last_checked"] = now_iso()
        return False

    change_reason = []
    if url_changed:    change_reason.append(f"new URL {url.split('/')[-1]}")
    if size_changed:   change_reason.append(f"size {stored_length} → {content_length}")
    if force:          change_reason.append("forced")
    log.info(f"  [{district_type}] Change detected: {', '.join(change_reason)}")

    # ── Download zip ─────────────────────────────────────────────────────────
    zip_bytes = download_tiger_zip(session, url)
    if not zip_bytes:
        log.error(f"  [{district_type}] Download failed — aborting")
        return False

    # ── Convert to GeoJSON in a temp location ────────────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        new_geojson = Path(tmp) / cfg["output"]
        ok = convert_to_geojson(zip_bytes, shapefile_name, cfg["tolerance"], new_geojson)
        if not ok:
            return False

        # ── Diff geometry hashes ──────────────────────────────────────────────
        existing_geojson = OUT_DIR / cfg["output"]
        # Derive the num_key from the shapefile name (may differ between TIGER years)
        num_key = _infer_num_key(shapefile_name) or cfg["num_key"]
        old_hashes = geojson_feature_hashes(existing_geojson, num_key)
        new_hashes = geojson_feature_hashes(new_geojson, num_key)

        changed_districts: list[str] = []
        for num, new_hash in new_hashes.items():
            if old_hashes.get(num) != new_hash:
                changed_districts.append(num)
        new_districts = sorted(set(new_hashes) - set(old_hashes))
        removed_districts = sorted(set(old_hashes) - set(new_hashes))

        if changed_districts or new_districts or removed_districts:
            log.info(
                f"  [{district_type}] Geometry changes — "
                f"modified: {sorted(changed_districts)}, "
                f"added: {new_districts}, removed: {removed_districts}"
            )
        else:
            log.info(f"  [{district_type}] TIGER size changed but geometries identical — no boundary changes")
            # Still update metadata
            tiger_state.update({"url": url, "content_length": content_length,
                                 "last_checked": now_iso()})
            return False

        # ── Copy files ────────────────────────────────────────────────────────
        new_sha256 = sha256_of_bytes(new_geojson.read_bytes())
        if not dry_run:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            dest = OUT_DIR / cfg["output"]
            dest.write_bytes(new_geojson.read_bytes())
            log.info(f"  [{district_type}] Updated {dest}")

            web_dest = WEB_PUBLIC / "districts" / cfg["output"]
            if web_dest.parent.exists():
                web_dest.write_bytes(new_geojson.read_bytes())
                log.info(f"  [{district_type}] Copied → {web_dest}")
            else:
                log.warning(f"  [{district_type}] Web public dir not found: {web_dest.parent}")
        else:
            log.info(f"  [{district_type}] [dry-run] Would update GeoJSON files")

    # ── Build Supabase records ────────────────────────────────────────────────
    changed_date = datetime.date.today().isoformat()
    records = []
    for num in sorted(set(changed_districts + new_districts)):
        records.append({
            "state":           "WA",
            "district_type":   district_type,
            "district_number": num,
            "changed_date":    changed_date,
            "description":     (
                f"Boundary updated — TIGER {url.split('/')[-1]} "
                f"({', '.join(change_reason)})"
            ),
            "source_url": "https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html",
        })
    record_changes(sb, records, dry_run)

    # ── Log change to status ─────────────────────────────────────────────────
    status["changes"].append({
        "detected_at":      now_iso(),
        "district_type":    district_type,
        "state":            "WA",
        "changed_districts": sorted(set(changed_districts + new_districts)),
        "tiger_url":        url,
        "description":      ", ".join(change_reason),
    })

    tiger_state.update({
        "url":            url,
        "content_length": content_length,
        "geojson_sha256": new_sha256,
        "last_checked":   now_iso(),
        "last_changed":   now_iso(),
    })

    return True

def _infer_num_key(shapefile_name: str) -> Optional[str]:
    """Derive the district number GeoJSON property key from a shapefile name."""
    n = shapefile_name.lower()
    if "cd" in n:   return None  # uses CD{congress}FP — let caller handle
    if "sldl" in n: return "SLDLST"
    if "sldu" in n: return "SLDUST"
    return None

# ── Source monitor helpers ─────────────────────────────────────────────────────

def _should_check(last_checked_iso: Optional[str], interval_hours: float) -> bool:
    if not last_checked_iso:
        return True
    try:
        last = datetime.datetime.fromisoformat(last_checked_iso.rstrip("Z"))
        return (datetime.datetime.utcnow() - last).total_seconds() > interval_hours * 3600
    except Exception:
        return True

def check_text_source(
    label: str,
    current_text: Optional[str],
    source_state: dict,
    hash_key: str,
) -> bool:
    """Compare current page text hash with stored; return True if changed."""
    if not current_text:
        log.warning(f"  [{label}] Could not fetch page")
        return False
    current_hash = sha256_of_text(current_text)
    stored_hash = source_state.get(hash_key)
    changed = (stored_hash is not None and current_hash != stored_hash)
    source_state[hash_key] = current_hash
    source_state["last_checked"] = now_iso()
    if changed:
        source_state["last_changed"] = now_iso()
        log.info(f"  [{label}] Content changed — may signal redistricting activity")
    else:
        log.info(f"  [{label}] No change detected")
    return changed

# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Redistricting monitor for WA State")
    parser.add_argument("--force",   action="store_true", help="Re-check all sources even if recently checked")
    parser.add_argument("--dry-run", action="store_true", help="Detect changes without writing to DB or files")
    args = parser.parse_args()

    if not HAS_GEOPANDAS:
        log.warning("geopandas not installed — TIGER conversion disabled. Run: pip install geopandas")

    log.info("=== redistricting-sync starting ===")
    if args.dry_run: log.info("DRY-RUN mode — no files or DB writes")

    sb = make_supabase()
    session = make_session()
    status = load_status()

    # ── Text-based source monitors (informational) ─────────────────────────────
    if args.force or _should_check(status["sources"]["all_about_redistricting"].get("last_checked"), CHECK_INTERVAL_HOURS):
        log.info("Checking All About Redistricting …")
        text = scrape_all_about_redistricting(session)
        check_text_source(
            "all_about_redistricting", text,
            status["sources"]["all_about_redistricting"], "wa_hash",
        )
    else:
        log.info("All About Redistricting — recently checked, skipping")

    if args.force or _should_check(status["sources"]["ballotpedia_wa"].get("last_checked"), CHECK_INTERVAL_HOURS):
        log.info("Checking Ballotpedia WA redistricting page …")
        text = scrape_ballotpedia_wa(session)
        check_text_source(
            "ballotpedia_wa", text,
            status["sources"]["ballotpedia_wa"], "content_hash",
        )
    else:
        log.info("Ballotpedia WA — recently checked, skipping")

    # ── TIGER file monitors (authoritative) ───────────────────────────────────
    log.info("Checking Census TIGER/Line files …")
    any_change = False
    for district_type in ("congressional", "house", "senate"):
        changed = process_tiger_type(
            district_type, session, sb, status,
            dry_run=args.dry_run, force=args.force,
        )
        if changed:
            any_change = True

    if any_change:
        log.info("Redistricting changes detected and processed.")
    else:
        log.info("No redistricting changes detected.")

    save_status(status)
    log.info("=== redistricting-sync complete ===")

if __name__ == "__main__":
    main()
