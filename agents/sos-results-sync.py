#!/usr/bin/env python3
"""
sos-results-sync.py
===================
Loads official candidates and vote counts from the Washington Secretary of State's results
export into the elections tables.

Sources (tried in this order for --date):
  1. https://results.votewa.gov/cdn/results/washington/export-<YYYYMMDD>.json   (2026 and later,
     the state's new "Enhanced Voting" results site; "Media Export" on the Reports page)
  2. https://results.vote.wa.gov/results/<YYYYMMDD>/export/<YYYYMMDD>_AllState.csv   (older elections)
Either can also be passed directly with --file (a local file or a web address).

What it does
  - Reads the export CSV (one row per candidate per contest).
  - Matches each contest to an existing race (by chamber, district and position) or creates it.
  - For each candidate: updates the matching row (same last name and first initial) or inserts one,
    and marks it status='verified' with votes and percent.
  - Never deletes. Candidates from earlier imports that are not on the official list stay
    status='unverified' (the website only shows verified candidates).

Usage
  python agents/sos-results-sync.py --date 20261103 --dry-run
  python agents/sos-results-sync.py --date 20261103 --certified
  python agents/sos-results-sync.py --file ~/Downloads/20251104_AllState.csv --parse-only

Requires migrations/add_candidate_verification.sql to have been run.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("sos-results-sync")

JSON_URL = "https://results.votewa.gov/cdn/results/washington/export-{date}.json"
CSV_URL = "https://results.vote.wa.gov/results/{date}/export/{date}_AllState.csv"
MEASURE_WORDS = {"approved", "rejected", "yes", "no", "for", "against", "maintained", "repealed"}


class NotFound(Exception):
    pass


# ── Parsing ───────────────────────────────────────────────────────────────────

def clean_party(raw: str) -> Optional[str]:
    """'(Prefers Democratic Party)' -> 'Democratic'. Judicial and blank values -> None."""
    raw = (raw or "").strip()
    m = re.match(r"^\(\s*(?:prefers|states)\s+(.*?)\s*(?:party)?\s*\)$", raw, re.IGNORECASE)
    value = (m.group(1) if m else raw).strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered in ("democratic", "democrat"):
        return "Democratic"
    if lowered in ("republican", "gop"):
        return "Republican"
    if "no party" in lowered:
        return "No Party Preference"
    if lowered == "nonpartisan":
        return "Nonpartisan"
    return value.title() if value.isupper() else value


def title_case(text: str) -> str:
    small = {"of", "and", "the", "for", "in"}
    words = re.split(r"(\s+)", text.lower())
    out = []
    for i, w in enumerate(words):
        if w.strip() == "":
            out.append(w)
        elif i > 0 and w in small:
            out.append(w)
        else:
            out.append(re.sub(r"^([a-z])", lambda m: m.group(1).upper(), w))
    return "".join(out)


COUNTY_OFFICES = [
    (r"director of elections", "Director of Elections"),
    (r"assessor", "Assessor"),
    (r"auditor", "Auditor"),
    (r"\bclerk\b", "Clerk"),
    (r"coroner|medical examiner", "Coroner"),
    (r"prosecut|pros\.? atty", "Prosecuting Attorney"),
    (r"sheriff", "Sheriff"),
    (r"treasurer", "Treasurer"),
    (r"county executive|\bexecutive\b", "Executive"),
]
COUNTY_BOARD = r"(commissioner|councilor|council)"
# Contests that are not county offices (courts, special districts, cities). Not handled yet.
COUNTY_EXCLUDE = re.compile(
    r"court|judge|judicial|utility|\bpud\b|fire|port\b|school|hospital|cemetery|water|sewer|"
    r"library|park|city of|municipal|town of|levy|bond|measure|proposition|charter|\bcity\b",
    re.I,
)


def county_office(text: str) -> Optional[tuple]:
    """Return (office, number) for a county office such as ('Sheriff', None) or ('Commissioner', 3)."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    if COUNTY_EXCLUDE.search(low):
        return None
    m = re.search(COUNTY_BOARD, low)
    if m:
        n = re.search(r"(?:district|dist\.?|no\.?|position|pos\.?)\s*#?\s*(\d+)", low)
        office = "Council" if m.group(1) in ("council", "councilor") else "Commissioner"
        return (office, int(n.group(1)) if n else None)
    for pattern, label in COUNTY_OFFICES:
        if re.search(pattern, low):
            return (label, None)
    return None


def county_name_of(jurisdiction: str) -> Optional[str]:
    j = re.sub(r"\s+", " ", (jurisdiction or "").strip())
    m = re.match(r"(.+?) county$", j, re.I)
    return m.group(1).strip() if m else None


def parse_contest(race: str, jurisdiction: str) -> Optional[dict]:
    """
    Turn a Secretary of State contest label into {kind, district, position, office_name, level}.
    Returns None for contests this agent does not handle yet (county, city, measures).
    """
    r = re.sub(r"\s+", " ", race.strip())
    low = r.lower()

    m = re.match(r"legislative district (\d+) - state senator", low)
    if m:
        d = int(m.group(1))
        return dict(kind="senate", district=d, position=None, level="state",
                    office_name=f"Washington State Senate District {d}")

    m = re.match(r"legislative district (\d+) - state representative pos\.? ?(\d)", low)
    if m:
        d, p = int(m.group(1)), int(m.group(2))
        return dict(kind="house", district=d, position=p, level="state",
                    office_name=f"Washington State House District {d} Position {p}")

    m = re.match(r"congressional district (\d+) - u\.?s\.? representative", low)
    if m:
        d = int(m.group(1))
        return dict(kind="congress", district=d, position=None, level="federal",
                    office_name=f"U.S. House Washington District {d}")

    if re.match(r"(united states|u\.?s\.?) senator", low):
        return dict(kind="ussenate", district=None, position=None, level="federal",
                    office_name="U.S. Senate Washington")

    county = county_name_of(jurisdiction)
    if county:
        office = county_office(r)
        if office is None:
            return None
        label, num = office
        title = f"{label} District {num}" if num else label
        return dict(kind="county", district=num, position=None, level="county", county=county,
                    office=label, office_name=f"{county} County {title}")

    if jurisdiction.strip().lower() == "judicial" or re.match(
        r"(supreme court|court of appeals|superior court)", low
    ):
        return dict(kind="judicial", district=None, position=None, level="state",
                    office_name=title_case(r))

    return None


def parse_existing_name(name: str) -> tuple:
    """Best-effort key (kind, district, position) for an election already in the database."""
    low = (name or "").lower()
    d = re.search(r"district\s+(\d+)", low)
    district = int(d.group(1)) if d else None
    p = re.search(r"pos(?:ition)?\.?\s*(\d)", low)
    position = int(p.group(1)) if p else None
    if "senate" in low and "u.s." not in low and "united states" not in low and district:
        return ("senate", district, None)
    if ("house" in low or "representative" in low) and "u.s." not in low \
            and "congress" not in low and "united states" not in low and district:
        return ("house", district, position)
    if ("u.s. house" in low or "congress" in low) and district:
        return ("congress", district, None)
    return ("name", re.sub(r"[^a-z0-9]+", " ", low).strip(), None)


def parse_rows(rows: list[dict]) -> list[dict]:
    """Group CSV rows into contests with candidates. Skips write-ins, measures and unsupported contests."""
    contests: dict[str, dict] = {}
    skipped: dict[str, int] = {}
    for row in rows:
        race = (row.get("Race") or "").strip()
        if not race:
            continue
        info = parse_contest(race, row.get("JurisdictionName") or "")
        if info is None:
            skipped[race] = skipped.get(race, 0) + 1
            continue
        cand = (row.get("Candidate") or "").strip()
        if not cand or cand.upper() == "WRITE-IN":
            continue
        if cand.lower() in MEASURE_WORDS:
            continue
        key = info["office_name"]
        try:
            seats = int(float(row.get("VoteFor") or 1))
        except ValueError:
            seats = 1
        c = contests.setdefault(key, {**info, "seats": seats, "candidates": []})
        votes = int(float(row.get("Votes") or 0))
        pct = float(row.get("PercentageOfTotalVotes") or 0)
        c["candidates"].append({
            "name": re.sub(r"\s+", " ", cand),
            "party": clean_party(row.get("Party") or ""),
            "votes": votes,
            "vote_pct": pct,
        })
    for race, n in skipped.items():
        log.info("Skipped (not handled yet): %s", race)
    return list(contests.values())


def name_key(name: str) -> tuple:
    """(last name, first initial), so 'Deb Krishnadasan' matches 'Deborah Krishnadasan'."""
    cleaned = re.sub(r"[^a-z\s'-]", "", name.lower())
    parts = [p for p in cleaned.split() if p not in ("jr", "sr", "ii", "iii", "iv")]
    if not parts:
        return ("", "")
    return (parts[-1], parts[0][:1])


# ── Input ─────────────────────────────────────────────────────────────────────

def rows_from_json(data: dict) -> list[dict]:
    """Flatten the state's JSON export into the same row shape as the CSV files (plus VoteFor)."""
    rows: list[dict] = []
    for item in (data.get("results") or {}).get("ballotItems") or []:
        if item.get("contestType") == "BallotMeasure":
            continue   # measures are not handled yet
        options = item.get("ballotOptions") or []
        total = sum(int(o.get("voteCount") or 0) for o in options)
        for o in options:
            votes = int(o.get("voteCount") or 0)
            rows.append({
                "Race": item.get("name") or "",
                "Candidate": o.get("name") or "",
                "Party": o.get("politicalParty") or "",
                "Votes": votes,
                "PercentageOfTotalVotes": round(100.0 * votes / total, 2) if total else 0,
                "JurisdictionName": "",
                "VoteFor": item.get("voteFor") or 1,
            })
    for jur in data.get("localResults") or []:
        county = jur.get("name") or ""
        for item in jur.get("ballotItems") or []:
            if item.get("type") != "Local" or item.get("contestType") != "Candidate":
                continue
            options = item.get("ballotOptions") or []
            total = sum(int(o.get("voteCount") or 0) for o in options)
            for o in options:
                votes = int(o.get("voteCount") or 0)
                rows.append({
                    "Race": item.get("name") or "",
                    "Candidate": o.get("name") or "",
                    "Party": o.get("politicalParty") or "",
                    "Votes": votes,
                    "PercentageOfTotalVotes": round(100.0 * votes / total, 2) if total else 0,
                    "JurisdictionName": county,
                    "VoteFor": item.get("voteFor") or 1,
                })
    return rows


def read_source(src: str) -> list[dict]:
    """Read a results file (CSV or the state's JSON export) from a web address or local path."""
    if re.match(r"https?://", src):
        resp = requests.get(src, timeout=180, headers={"User-Agent": "CommuneUSA data agent"})
        if resp.status_code == 404:
            raise NotFound(src)
        resp.raise_for_status()
        raw = resp.content
    else:
        with open(src, "rb") as fh:
            raw = fh.read()
    text = raw.decode("utf-8-sig", errors="replace")
    if text.lstrip().startswith("{"):
        import json
        return rows_from_json(json.loads(text))
    return list(csv.DictReader(io.StringIO(text)))


# ── Database ──────────────────────────────────────────────────────────────────

def apply(contests: list[dict], election_date: str, certified: bool, dry_run: bool) -> None:
    from supabase import create_client

    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    db = create_client(url, key)

    wa = db.table("states").select("id").eq("abbreviation", "WA").single().execute().data["id"]
    elections = (
        db.table("elections")
        .select("id,office_name,election_date,level,county_id,municipality_id")
        .eq("election_date", election_date)
        .execute()
        .data
        or []
    )
    counties = db.table("counties").select("id,name").eq("state_id", wa).execute().data or []
    county_ids = {re.sub(r"\s+county$", "", c["name"], flags=re.I).strip().lower(): c["id"] for c in counties}
    by_key: dict[tuple, list[dict]] = {}
    by_county: dict[tuple, list[dict]] = {}
    for e in elections:
        if e.get("municipality_id"):
            continue
        if e.get("county_id"):
            off = county_office(e["office_name"])
            if off:
                by_county.setdefault((e["county_id"], off), []).append(e)
            continue
        by_key.setdefault(parse_existing_name(e["office_name"]), []).append(e)

    now = datetime.now(timezone.utc).isoformat()
    status = "certified" if certified else "unofficial"
    stats = dict(elections_matched=0, elections_created=0, cand_updated=0, cand_inserted=0, ambiguous=0)

    for c in contests:
        county_id = None
        if c["kind"] == "county":
            county_id = county_ids.get(c["county"].lower())
            if not county_id:
                log.warning("County not found in database, skipping: %s", c["office_name"])
                stats["ambiguous"] += 1
                continue
            matches = by_county.get((county_id, (c["office"], c["district"])), [])
        else:
            matches = None
        k = (c["kind"], c["district"], c["position"]) if c["kind"] != "judicial" else (
            "name", re.sub(r"[^a-z0-9]+", " ", c["office_name"].lower()).strip(), None)
        if matches is None:
            matches = by_key.get(k, [])
        if len(matches) > 1:
            log.warning("AMBIGUOUS, skipping: %s matches %d existing races", c["office_name"], len(matches))
            stats["ambiguous"] += 1
            continue
        if matches:
            election_id = matches[0]["id"]
            stats["elections_matched"] += 1
            log.info("MATCH   %-52s -> %s", c["office_name"], matches[0]["office_name"])
            if not dry_run:
                db.table("elections").update(
                    {"results_status": status, "results_updated_at": now, "seats": c["seats"]}
                ).eq("id", election_id).execute()
        else:
            stats["elections_created"] += 1
            log.info("CREATE  %s", c["office_name"])
            election_id = None
            if not dry_run:
              res = db.table("elections").insert({
                "state_id": wa, "county_id": county_id, "office_name": c["office_name"], "level": c["level"],
                "election_date": election_date, "source_url": "https://results.vote.wa.gov",
                "results_status": status, "results_updated_at": now, "seats": c["seats"],
              }).execute()
              election_id = res.data[0]["id"]

        existing = []
        if election_id:
            existing = db.table("candidates").select("id,name").eq("election_id", election_id).execute().data or []
        by_name = {name_key(x["name"]): x for x in existing}

        for cand in c["candidates"]:
            fields = {
                "party": cand["party"], "votes": cand["votes"], "vote_pct": cand["vote_pct"],
                "source": "wa_sos", "status": "verified", "verified_at": now,
            }
            hit = by_name.get(name_key(cand["name"]))
            if hit:
                stats["cand_updated"] += 1
                log.info("   update  %s (%s votes)", cand["name"], f"{cand['votes']:,}")
                if not dry_run:
                    db.table("candidates").update(fields).eq("id", hit["id"]).execute()
            else:
                stats["cand_inserted"] += 1
                log.info("   insert  %s (%s votes)", cand["name"], f"{cand['votes']:,}")
                if not dry_run:
                    db.table("candidates").insert(
                        {"election_id": election_id, "name": cand["name"], "is_incumbent": False, **fields}
                    ).execute()

    print("\n" + "=" * 60)
    print("  DRY RUN, nothing written" if dry_run else "  Done")
    for k, v in stats.items():
        print(f"  {k:<18} {v}")
    print("=" * 60)


# ── Entry point ───────────────────────────────────────────────────────────────

def discover(date: str) -> None:
    """Print which results pages and export files the state site actually offers. Writes nothing."""
    hdr = {"User-Agent": "CommuneUSA data agent"}
    pages = [
        JSON_URL.format(date=date),
        CSV_URL.format(date=date),
        f"https://results.votewa.gov/results/public/washington/elections/{date}/reports",
        "https://results.vote.wa.gov/",
        f"https://results.vote.wa.gov/results/{date}/export.html",
        f"https://results.vote.wa.gov/results/{date}/",
        f"https://results.vote.wa.gov/results/{date}/default.html",
    ]
    for page in pages:
        is_data = page.endswith((".json", ".csv"))
        try:
            r = requests.get(page, timeout=30, headers=hdr, stream=is_data)
        except requests.RequestException as exc:
            print(f"{page}\n   could not connect: {exc}")
            continue
        size = r.headers.get("Content-Length")
        print(f"{page}\n   HTTP {r.status_code}" + (f", {int(size):,} bytes" if size and size.isdigit() else ""))
        if is_data:
            r.close()
            continue
        if r.status_code != 200:
            continue
        links = sorted(set(re.findall(r'href=["\']([^"\']+)["\']', r.text)))
        keep = [l for l in links if re.search(r"results/\d{8}|\.csv|\.xml|export", l, re.I)]
        for l in keep[:60]:
            print(f"   {l}")
        if not keep:
            print("   (no results or export links on this page)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Load Washington Secretary of State results into CommuneUSA.")
    ap.add_argument("--date", help="Election date as YYYYMMDD, e.g. 20261103 (downloads the export files)")
    ap.add_argument("--file", action="append", default=[], help="Local CSV or URL (repeatable) instead of --date downloads")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing")
    ap.add_argument("--parse-only", action="store_true", help="Parse the files and print contests; no database access")
    ap.add_argument("--discover", action="store_true", help="List the results pages and export files the state site offers for --date, then exit")
    ap.add_argument("--certified", action="store_true", help="Mark results as certified (default: unofficial)")
    args = ap.parse_args()

    if not args.date and not args.file:
        ap.error("give --date or --file")

    if args.discover:
        if not args.date:
            ap.error("--discover needs --date")
        discover(args.date)
        return

    sources = list(args.file)
    if args.date and not re.fullmatch(r"\d{8}", args.date):
        ap.error("--date must look like 20261103")

    rows: list[dict] = []
    if sources:
        for src in sources:
            log.info("Reading %s", src)
            try:
                rows += read_source(src)
            except NotFound:
                sys.exit(f"ERROR: not found: {src}")
    else:
        for src in (JSON_URL.format(date=args.date), CSV_URL.format(date=args.date)):
            log.info("Reading %s", src)
            try:
                rows = read_source(src)
                break
            except NotFound:
                log.warning("Not found: %s", src)
        else:
            sys.exit(
                "ERROR: no results file found for this date. The state may not have posted it yet. "
                "Run again with the 'discover' option to see what the site offers."
            )
    contests = parse_rows(rows)
    log.info("Parsed %d contests, %d candidates", len(contests), sum(len(c["candidates"]) for c in contests))

    if args.parse_only:
        for c in contests:
            print(f"{c['office_name']}  [{c['level']}]")
            for cand in c["candidates"]:
                print(f"    {cand['name']:<28} {cand['party'] or '-':<12} {cand['votes']:>9,}  {cand['vote_pct']:>6.2f}%")
        return

    if args.date:
        d = args.date
        election_date = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    else:
        sys.exit("ERROR: database updates need --date so races get the right election date")
    apply(contests, election_date, args.certified, args.dry_run)


if __name__ == "__main__":
    main()
