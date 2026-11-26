#!/usr/bin/env python3
"""
role-type-sync.py

Fills `officials.appointed_or_elected` so profiles can say whether someone won their seat at the ballot box
or was appointed to it. Nothing is guessed from a job title:

  * Federal members of Congress are elected (every current Washington member won a general election).
  * State legislators and statewide officials are marked Elected only when they match a winning candidate in
    our certified election results, in the same kind of office.
  * Anyone listed in data/known-appointed.json is marked Appointed, with the source on file there.
  * A legislator with no winning race is NOT labelled. They are listed in the log as "possible vacancy
    appointment" so a person can confirm it and add them to known-appointed.json. In Washington a county
    council picks a replacement when a legislator leaves mid-term, so a missing win is a real signal, but
    it can also be a gap in our results.

County officials are handled by county-terms-sync (a certified county win marks them Elected). Existing
values are never overwritten, so a hand-entered Appointed or Elected stays as it is.

Usage
-----
  python agents/role-type-sync.py --dry-run
  python agents/role-type-sync.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from supabase import create_client

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

APPOINTED_FILE = Path(__file__).resolve().parent.parent / "data" / "known-appointed.json"
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

# Kinds of state office, matched on both the official's title and the race name.
STATE_CLASSES = [
    # Official titles read "State Senate" / "State House of Representatives"; race names say "Senator" / "Representative".
    ("senator", r"\bsenat(?:or|e)\b"),
    ("representative", r"\b(?:representatives?|rep)\b"),
    ("governor", r"\bgovernor\b"),
    ("secretary of state", r"secretary of state"),
    ("attorney general", r"attorney general"),
    ("treasurer", r"treasurer"),
    ("auditor", r"\bauditor\b"),
    ("public lands", r"public lands"),
    ("superintendent", r"superintendent"),
    ("insurance", r"insurance"),
]


def state_class(text: str | None) -> str | None:
    t = (text or "").lower()
    if "lieutenant governor" in t:
        return "lieutenant governor"
    for name, pattern in STATE_CLASSES:
        if re.search(pattern, t):
            return name
    return None


def fetch_all(query, page: int = 1000) -> list[dict]:
    rows, offset = [], 0
    while True:
        batch = query().range(offset, offset + page - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < page:
            return rows
        offset += page


def name_key(name: str) -> tuple[str, str] | None:
    name = re.sub(r"\(.*?\)|\".*?\"", " ", name or "")
    t = [x.lower() for x in re.sub(r"[.,]", " ", name).split() if x.lower() not in SUFFIXES]
    return (t[0], t[-1]) if len(t) >= 2 else None


def known_appointed() -> list[dict]:
    try:
        return json.loads(APPOINTED_FILE.read_text()).get("appointed", [])
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s: %s", APPOINTED_FILE.name, exc)
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

    officials = fetch_all(lambda: sb.table("officials")
        .select("id,official_name,office_title,level,appointed_or_elected")
        .eq("is_active", True).in_("level", ["federal", "state", "county"]).order("id"))
    log.info("%d active federal, state and county officials", len(officials))

    elections = fetch_all(lambda: sb.table("elections")
        .select("id,office_name,election_type,level").in_("level", ["state", "federal"]).order("id"))
    by_election = {e["id"]: e for e in elections}
    winners = fetch_all(lambda: sb.table("candidates")
        .select("election_id,name,official_id").eq("outcome", "won").order("id"))
    wins: dict[str, list[dict]] = defaultdict(list)  # class -> wins
    unclassified: dict[str, int] = defaultdict(int)
    for w in winners:
        e = by_election.get(w["election_id"])
        if not e or e["level"] != "state" or (e.get("election_type") or "general") != "general":
            continue
        cls = state_class(e["office_name"])
        if cls:
            wins[cls].append({"key": name_key(w["name"]), "official_id": w.get("official_id")})
        else:
            unclassified[e["office_name"]] += 1
    # Diagnostics: shows whether House races are in the data and what they are called.
    log.info("Certified state wins by office kind: %s", {k: len(v) for k, v in sorted(wins.items())})
    log.info("State race names not recognised (%d), for example:", len(unclassified))
    for n, c in sorted(unclassified.items(), key=lambda x: -x[1])[:25]:
        log.info("    %s (%d wins)", n, c)

    appointed = known_appointed()
    updates: list[tuple[str, str, str, str]] = []  # id, name, value, reason
    review: list[str] = []
    for o in officials:
        if (o.get("appointed_or_elected") or "").strip():
            continue  # never overwrite
        name, title = o["official_name"], (o.get("office_title") or "")
        hit = next((a for a in appointed if a["name"] == name and a.get("level") == o["level"]
                    and a.get("office_contains", "").lower() in title.lower()), None)
        if hit:
            updates.append((o["id"], name, "Appointed", hit.get("how", "listed in known-appointed.json")))
            continue
        if o["level"] == "federal":
            updates.append((o["id"], name, "Elected", "member of Congress"))
        elif o["level"] == "state":
            cls = state_class(title)
            key = name_key(name)
            matched = cls and any(w["official_id"] == o["id"] or (key and w["key"] == key) for w in wins.get(cls, []))
            if matched:
                updates.append((o["id"], name, "Elected", "matches a certified winning race"))
            else:
                review.append(f"{name} ({title})")
        # county: handled by county-terms-sync

    log.info("%d officials to label", len(updates))
    counts: dict[str, int] = defaultdict(int)
    for _id, name, value, reason in updates:
        counts[value] += 1
        if value == "Appointed":
            log.info("APPOINTED  %s: %s", name, reason)
    log.info("Elected: %d, Appointed: %d", counts["Elected"], counts["Appointed"])
    log.info("%d state officials have no winning race in our data (left unlabelled). If one of them was "
             "appointed to fill a vacancy, confirm it and add them to data/known-appointed.json:", len(review))
    for r in review:
        log.info("    possible vacancy appointment or missing result: %s", r)

    if args.dry_run:
        log.info("=== dry run, nothing written ===")
        return 0
    for oid, _n, value, _r in updates:
        sb.table("officials").update({"appointed_or_elected": value}).eq("id", oid).execute()
    log.info("=== applied ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
