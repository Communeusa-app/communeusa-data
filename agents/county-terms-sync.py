#!/usr/bin/env python3
"""
county-terms-sync.py

The county directories (MRSC, WSAC) list who holds each office but not their party or term.
Certified Secretary of State results do: the winner of a county race has a party on the ballot,
and county terms run four years from the election. This agent matches each active county
official to the races they won, then:

  * fills in party when the official has none (never overwrites a party that is already set),
  * sets term start and end from the latest general-election win when that is newer than what is
    stored (so stale values like "Dec 2025" are corrected),
  * writes one official_terms row per general-election win, so the profile's office history shows
    each elected term.

Nothing is guessed: an official with no winning race in our election data is left alone and listed.
Special elections are ignored here because their terms start on certification, not in January.

Usage
-----
  python agents/county-terms-sync.py --dry-run   # report what would change
  python agents/county-terms-sync.py             # apply
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from supabase import create_client

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

SOURCE = "WA Secretary of State certified election results"
SOURCE_URL = "https://results.vote.wa.gov/"
TERM_YEARS = 4
# Ballot party label (as stored on candidates) -> label used on official profiles. Anything else is skipped.
PARTY_MAP = {
    "democratic": "Democrat", "democrat": "Democrat",
    "republican": "Republican",
    "independent": "Independent", "independent party": "Independent",
    "libertarian": "Libertarian", "green": "Green", "green party": "Green",
}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def fetch_all(query, page: int = 1000) -> list[dict]:
    """Supabase returns at most 1000 rows per request, so read every page."""
    rows, offset = [], 0
    while True:
        batch = query().range(offset, offset + page - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < page:
            return rows
        offset += page


def tokens(name: str) -> list[str]:
    name = re.sub(r"\(.*?\)|\".*?\"", " ", name or "")
    name = re.sub(r"[.,]", " ", name)
    return [t.lower() for t in name.split() if t.lower() not in SUFFIXES]


def name_key(name: str) -> tuple[str, str] | None:
    t = tokens(name)
    return (t[0], t[-1]) if len(t) >= 2 else None


def year_of(text: str | None) -> int | None:
    m = re.search(r"(19|20)\d{2}", text or "")
    return int(m.group(0)) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    this_year = datetime.now(timezone.utc).year

    officials = fetch_all(lambda: sb.table("officials")
        .select("id,official_name,office_title,party,term_start,term_end,county_id,counties(name)")
        .eq("level", "county").eq("is_active", True).order("id"))
    elections = fetch_all(lambda: sb.table("elections")
        .select("id,office_name,election_date,election_type,county_id")
        .eq("level", "county").not_.is_("county_id", "null").order("id"))
    by_election = {e["id"]: e for e in elections}
    winners = fetch_all(lambda: sb.table("candidates")
        .select("election_id,name,party,official_id,outcome").eq("outcome", "won").order("id"))
    log.info("%d county officials, %d county races, %d winning candidates", len(officials), len(elections), len(winners))

    # county_id -> list of winning entries
    wins_by_county: dict[str, list[dict]] = defaultdict(list)
    for w in winners:
        e = by_election.get(w["election_id"])
        if not e or not e.get("election_date"):
            continue
        year = year_of(e["election_date"])
        if not year or (e.get("election_type") or "general") != "general":
            continue
        wins_by_county[e["county_id"]].append({
            "key": name_key(w["name"]), "official_id": w.get("official_id"), "party": (w.get("party") or "").strip(),
            "year": year, "office_name": e["office_name"], "name": w["name"],
        })

    updates, term_rows, no_win, mismatches = [], [], [], []
    filled_party = fixed_terms = 0
    for o in officials:
        key = name_key(o["official_name"])
        county = (o.get("counties") or {}).get("name") or "?"
        mine = [w for w in wins_by_county.get(o["county_id"], [])
                if w["official_id"] == o["id"] or (key and w["key"] == key)]
        if not mine:
            # nicknames (Bob / Robert): same last name and first initial, only when it is unambiguous
            cand = [w for w in wins_by_county.get(o["county_id"], [])
                    if key and w["key"] and w["key"][1] == key[1] and w["key"][0][:1] == key[0][:1]]
            if len({w["name"] for w in cand}) == 1:
                mine = cand
        if not mine:
            no_win.append(f'{county}: {o["official_name"]} ({o["office_title"]})')
            continue
        mine.sort(key=lambda w: w["year"])
        latest = mine[-1]
        change: dict = {}

        ballot_party = PARTY_MAP.get(latest["party"].lower())
        if ballot_party and not (o.get("party") or "").strip():
            change["party"] = ballot_party
            filled_party += 1
        elif ballot_party and o.get("party") and o["party"].strip() != ballot_party:
            mismatches.append(f'{county}: {o["official_name"]} stored {o["party"]!r}, ballot says {ballot_party!r}')

        end_year = latest["year"] + TERM_YEARS
        stored_end = year_of(o.get("term_end"))
        if stored_end is None or end_year > stored_end:
            change["term_start"] = f"Jan {latest['year'] + 1}"
            change["term_end"] = f"Dec {end_year}"
            fixed_terms += 1
        if change:
            updates.append((o["id"], o["official_name"], county, change))

        title = f'{o["office_title"]}, {county} County' if o.get("office_title") else f"{county} County"
        for w in mine:
            start, end = w["year"] + 1, w["year"] + TERM_YEARS
            term_rows.append({
                "official_id": o["id"], "office": title, "start_year": start,
                "end_year": None if end >= this_year else end, "how_assumed": "elected",
                "note": f'Won the {w["year"]} general election',
                "source": SOURCE, "source_url": SOURCE_URL,
            })

    for _id, name, county, change in updates[:80]:
        log.info("%s (%s): %s", name, county, change)
    if len(updates) > 80:
        log.info("... and %d more", len(updates) - 80)
    log.info("%d officials matched to a win; party filled for %d; term dates set or corrected for %d; %d history rows",
             len(officials) - len(no_win), filled_party, fixed_terms, len(term_rows))
    for m in mismatches:
        log.warning("Party differs: %s", m)
    log.info("%d officials with no winning race in our data (left unchanged), for example:", len(no_win))
    for n in no_win[:25]:
        log.info("    %s", n)

    if args.dry_run:
        log.info("=== dry run, nothing written ===")
        return 0
    for oid, _n, _c, change in updates:
        sb.table("officials").update(change).eq("id", oid).execute()
    for i in range(0, len(term_rows), 100):
        sb.table("official_terms").upsert(term_rows[i:i + 100], on_conflict="official_id,office,start_year").execute()
    log.info("=== applied ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
