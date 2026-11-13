#!/usr/bin/env python3
"""
statewide-sync.py

Keeps the nine Washington statewide elected officials current. No directory or API covers
them, so the details live in data/statewide-officials.json (checked into the repo and edited
by hand, with the source for each fact being the office's own website).

Each run fetches every office's page and confirms the officeholder's last name is still on
it. Only then is the record updated and stamped as verified. If a page cannot be fetched
or no longer shows the name, the record is left alone, a warning is printed, and the
health check will flag it once the last verification gets old.

Usage
-----
  python agents/statewide-sync.py            # update and stamp
  python agents/statewide-sync.py --dry-run  # show what would change
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import requests
from supabase import create_client

import provenance

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent.parent / "data" / "statewide-officials.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CommuneUSA-bot/1.0; +https://www.communeusa.com)"}
FIELDS = ("party", "phone", "email", "official_website", "term_start", "term_end")


def page_names_official(url: str, official_name: str) -> bool:
    try:
        r = requests.get(url, headers=HEADERS, timeout=45)
    except requests.RequestException as exc:
        log.warning("Could not fetch %s: %s", url, exc)
        return False
    if r.status_code != 200:
        log.warning("Could not fetch %s: HTTP %s", url, r.status_code)
        return False
    return official_name.split()[-1].lower() in r.text.lower()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    db = create_client(url, key)

    entries = json.loads(DATA.read_text(encoding="utf-8"))
    verified = problems = 0
    for e in entries:
        title, name = e["office_title"], e["official_name"]
        rows = (db.table("officials").select("id,is_active," + ",".join(FIELDS))
                .eq("level", "state").eq("office_title", title).eq("official_name", name).execute().data or [])
        if not rows:
            log.error("%s: no row for %s in the database (the officeholder may have changed)", title, name)
            problems += 1
            continue
        candidates = e["verify_url"] if isinstance(e["verify_url"], list) else [e["verify_url"]]
        confirmed_at = next((u for u in candidates if page_names_official(u, name)), None)
        if not confirmed_at:
            log.warning("%s: %s not confirmed on %s, leaving the record unverified", title, name, ", ".join(candidates))
            problems += 1
            continue
        row = rows[0]
        changes = {f: e[f] for f in FIELDS if e.get(f) is not None and e[f] != row.get(f)}
        if not row.get("is_active"):
            changes["is_active"] = True
        log.info("%s: %s confirmed%s", title, name, f", updating {sorted(changes)}" if changes else "")
        if args.dry_run:
            continue
        if changes:
            db.table("officials").update(changes).eq("id", row["id"]).execute()
        for h in e.get("history") or []:
            db.table("official_terms").upsert({
                "official_id": row["id"], "office": h["office"], "start_year": h["start_year"],
                "end_year": h.get("end_year"), "how_assumed": h.get("how_assumed"), "note": h.get("note"),
                "source": "Official office website", "source_url": confirmed_at,
            }, on_conflict="official_id,office,start_year").execute()
        db.table("officials").update(
            {**provenance.fields_for("Official office website", confirmed_at)}
        ).eq("id", row["id"]).execute()
        verified += 1
    log.info("%d verified, %d need attention", verified, problems)
    if problems == len(entries):
        sys.exit("ERROR: could not confirm any statewide officials")


if __name__ == "__main__":
    main()
