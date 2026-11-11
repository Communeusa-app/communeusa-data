#!/usr/bin/env python3
"""
health-check.py

Read-only data quality checks for CommuneUSA. Writes nothing to the database.
Prints a report (and a GitHub run summary), and exits 1 when any ERROR is found so the
scheduled run fails and GitHub emails the owner. WARN lines are reported but do not fail the run.

Usage
-----
  python agents/health-check.py
  python agents/health-check.py --state WA
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from supabase import create_client

# Smallest believable counts of active officials. A drop below these means a sync wiped data.
FLOORS = {"federal": 12, "state": 147, "county": 150, "city": 300}
EXPECTED_WA_COUNTIES = 39
STALE_AFTER_DAYS = 3        # unofficial results should be refreshed nightly while counting
VERIFIED_WITHIN_DAYS = {"federal": 10, "state": 10, "county": 10, "city": 40}   # weekly syncs; city is monthly
COUNTING_WINDOW_DAYS = 45   # how long after an election results are still expected to move


def fetch_all(db, table: str, columns: str, **filters) -> list[dict]:
    rows, start, page = [], 0, 1000
    while True:
        q = db.table(table).select(columns)
        for k, v in filters.items():
            q = q.eq(k, v)
        chunk = q.range(start, start + page - 1).execute().data or []
        rows += chunk
        if len(chunk) < page:
            return rows
        start += page


class Report:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []  # (level, section, message)
        self.details: list[str] = []

    def error(self, section: str, msg: str) -> None:
        self.items.append(("ERROR", section, msg))

    def warn(self, section: str, msg: str) -> None:
        self.items.append(("WARN", section, msg))

    def info(self, section: str, msg: str) -> None:
        self.items.append(("INFO", section, msg))

    def render(self) -> str:
        errors = sum(1 for i in self.items if i[0] == "ERROR")
        warns = sum(1 for i in self.items if i[0] == "WARN")
        out = [f"# Data health check\n", f"**{errors} errors, {warns} warnings**\n"]
        for level in ("ERROR", "WARN", "INFO"):
            rows = [i for i in self.items if i[0] == level]
            if not rows:
                continue
            out.append(f"\n## {level}\n")
            for _, section, msg in rows:
                out.append(f"- [{section}] {msg}")
        if self.details:
            out.append("\n## Details\n")
            out += self.details
        return "\n".join(out) + "\n"


def sample(names: list[str], n: int = 8) -> str:
    shown = ", ".join(sorted(names)[:n])
    return shown + (f", and {len(names) - n} more" if len(names) > n else "")


def check_officials(db, wa: str, rep: Report) -> None:
    officials = fetch_all(
        db, "officials",
        "id,level,office_title,official_name,district,county_id,municipality_id,state_id,phone,email,official_website,is_active,created_at,updated_at,source,last_verified_at",
    )
    active = [o for o in officials if o.get("is_active")]
    by_level = Counter(o["level"] for o in active)
    for level, floor in FLOORS.items():
        n = by_level.get(level, 0)
        msg = f"{n} active {level} officials (expected at least {floor})"
        if n < floor:
            (rep.error if level in ("federal", "state") else rep.warn)("officials", msg)
        else:
            rep.info("officials", msg)

    blank = [o for o in active if not (o.get("official_name") or "").strip()]
    if blank:
        rep.error("officials", f"{len(blank)} active officials have no name")

    seen: dict[tuple, int] = defaultdict(int)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for o in active:
        key = (o["level"], (o["official_name"] or "").strip().lower(), (o["office_title"] or "").strip().lower(),
               o.get("county_id"), o.get("municipality_id"), o.get("state_id"))
        seen[key] += 1
        groups[key].append(o)
    dups = [k for k, v in seen.items() if v > 1]
    for k in dups[:12]:
        for o in groups[k]:
            rep.details.append(
                f"- duplicate official: {o['official_name']} | {o['office_title']} | district={o.get('district')} "
                f"| created={str(o.get('created_at'))[:19]} | updated={str(o.get('updated_at'))[:19]} | id={o['id']}"
            )
    if dups:
        rep.error("officials", f"{len(dups)} duplicate active officials, e.g. " + sample([f"{k[1]} ({k[2]})" for k in dups]))

    for level in ("federal", "state", "county", "city"):
        rows = [o for o in active if o["level"] == level]
        if not rows:
            continue
        contact = sum(1 for o in rows if o.get("phone") or o.get("email") or o.get("official_website"))
        rep.info("officials", f"{level}: {round(100 * contact / len(rows))}% have a phone, email or website")

    # Provenance: once the syncs have started stamping records, anything not confirmed lately is suspect.
    stamped = [o for o in active if o.get("last_verified_at")]
    if not stamped:
        rep.info("freshness", "no official has been verified yet (provenance not rolled out)")
    else:
        for level in ("federal", "state", "county", "city"):
            days = VERIFIED_WITHIN_DAYS[level]
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            rows = [o for o in active if o["level"] == level]
            old_rows = [o for o in rows
                        if not o.get("last_verified_at")
                        or datetime.fromisoformat(o["last_verified_at"].replace("Z", "+00:00")) < cutoff]
            if rows and old_rows:
                msg = (f"{len(old_rows)} of {len(rows)} active {level} officials were not confirmed by their source "
                       f"in the last {days} days")
                (rep.error if len(old_rows) > 0.2 * len(rows) else rep.warn)("freshness", msg)
                for o in old_rows[:5]:
                    rep.details.append(f"- not verified recently: {o['official_name']} ({o['office_title']}) "
                                       f"source={o.get('source')} last={o.get('last_verified_at')}")
            elif rows:
                rep.info("freshness", f"all {len(rows)} active {level} officials verified within {days} days")

    counties = fetch_all(db, "counties", "id,name", state_id=wa)
    if len(counties) != EXPECTED_WA_COUNTIES:
        rep.error("counties", f"{len(counties)} counties in the database, expected {EXPECTED_WA_COUNTIES}")
    with_officials = {o["county_id"] for o in active if o["level"] == "county" and o.get("county_id")}
    empty = [c["name"] for c in counties if c["id"] not in with_officials]
    if empty:
        rep.error("counties", f"{len(empty)} counties have no active officials: " + sample(empty))
    else:
        rep.info("counties", "every county has active officials")


def check_elections(db, wa: str, rep: Report) -> None:
    elections = fetch_all(
        db, "elections",
        "id,office_name,election_date,election_type,level,county_id,municipality_id,results_status,results_updated_at,seats",
        state_id=wa,
    )
    cands = fetch_all(db, "candidates", "id,election_id,name,votes,vote_pct,outcome,status")
    by_election: dict[str, list[dict]] = defaultdict(list)
    for c in cands:
        by_election[c["election_id"]].append(c)
    rep.info("elections", f"{len(elections)} races, {len(cands)} candidates")

    keys = Counter((e["election_date"], e["office_name"].strip().lower(), e.get("county_id"), e.get("municipality_id"))
                   for e in elections)
    dups = [k for k, v in keys.items() if v > 1]
    for k in dups[:12]:
        for e in elections:
            if (e["election_date"], e["office_name"].strip().lower(), e.get("county_id"), e.get("municipality_id")) == k:
                rep.details.append(
                    f"- duplicate race: {e['office_name']} {e['election_date']} | status={e.get('results_status')} "
                    f"| candidates={len(by_election.get(e['id'], []))} | id={e['id']}"
                )
    if dups:
        rep.error("elections", f"{len(dups)} duplicate races (same date, office and place), e.g. "
                  + sample([f"{k[1]} {k[0]}" for k in dups]))

    no_place = [e for e in elections if e["level"] == "county" and not e.get("county_id")]
    if no_place:
        rep.warn("elections", f"{len(no_place)} county-level races have no county, e.g. "
                 + sample([e["office_name"] for e in no_place]))

    certified_no_outcome, count_off, pct_off, certified_empty = [], [], [], []
    for e in elections:
        cs = [c for c in by_election.get(e["id"], []) if c.get("status") == "verified"]
        if e.get("results_status") == "certified":
            if not cs:
                certified_empty.append(e["office_name"])
                continue
            if not any(c.get("outcome") for c in cs):
                certified_no_outcome.append(f"{e['office_name']} {e['election_date']}")
                if len(certified_no_outcome) <= 20:
                    rep.details.append(f"- certified without won/advanced marks: {e['office_name']} {e['election_date']} "
                                       f"({e.get('election_type')}) id={e['id']} candidates=" + ", ".join(
                                           f"{c['name']} {c.get('votes')}" for c in cs))
                continue
            seats = e.get("seats") or 1
            if e.get("election_type") == "general":
                won = sum(1 for c in cs if c.get("outcome") == "won")
                if won != min(seats, len(cs)):
                    count_off.append(f"{e['office_name']} ({won} won, {seats} seats)")
            elif e.get("election_type") == "primary":
                adv = sum(1 for c in cs if c.get("outcome") == "advanced")
                if adv != min(2, len(cs)):
                    count_off.append(f"{e['office_name']} ({adv} advanced)")
        if cs and (e.get("seats") or 1) == 1:
            total = sum(float(c.get("vote_pct") or 0) for c in cs)
            # Write-ins are not stored, so small-town races can legitimately fall well under 100%.
            if total > 100.6 or (total and total < 50):
                pct_off.append(f"{e['office_name']} {e['election_date']} ({total:.1f}%)")
                if len(pct_off) <= 10:
                    rep.details.append(f"- vote shares {total:.1f}%: {e['office_name']} {e['election_date']} id={e['id']}: "
                                       + "; ".join(f"{c['name']} {c.get('votes')} ({c.get('vote_pct')}%)" for c in cs))
    if certified_empty:
        rep.warn("results", f"{len(certified_empty)} certified races have no verified candidates: " + sample(certified_empty))
    if certified_no_outcome:
        rep.error("results", f"{len(certified_no_outcome)} certified races have no won/advanced marks: "
                  + sample(certified_no_outcome))
    if count_off:
        rep.warn("results", f"{len(count_off)} certified races have an unexpected number of winners or advancers: "
                 + sample(count_off))
    if pct_off:
        rep.warn("results", f"{len(pct_off)} single-seat races have vote shares that do not add up: " + sample(pct_off))

    # Results should still be moving for recent elections; a stale timestamp means the nightly sync is failing.
    now = datetime.now(timezone.utc)
    recent = [e for e in elections
              if e.get("results_status") == "unofficial"
              and (now.date() - datetime.strptime(e["election_date"], "%Y-%m-%d").date()).days <= COUNTING_WINDOW_DAYS
              and e["election_date"] <= now.date().isoformat()]
    stale = []
    for e in recent:
        ts = e.get("results_updated_at")
        if not ts or now - datetime.fromisoformat(ts.replace("Z", "+00:00")) > timedelta(days=STALE_AFTER_DAYS):
            stale.append(e["office_name"])
    if stale:
        rep.error("results", f"{len(stale)} unofficial races in the counting window were not updated in "
                  f"{STALE_AFTER_DAYS} days (is the nightly results sync failing?): " + sample(stale))
    elif recent:
        rep.info("results", f"{len(recent)} unofficial races are fresh")


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only data quality checks.")
    ap.add_argument("--state", default="WA")
    args = ap.parse_args()

    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    db = create_client(url, key)
    wa = db.table("states").select("id").eq("abbreviation", args.state).single().execute().data["id"]

    rep = Report()
    check_officials(db, wa, rep)
    check_elections(db, wa, rep)
    text = rep.render()
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    if any(i[0] == "ERROR" for i in rep.items):
        sys.exit(1)


if __name__ == "__main__":
    main()
