#!/usr/bin/env python3
"""
bio-sync.py

Adds a short background paragraph to federal and state officials, taken from the opening
of their Wikipedia article. Wikipedia text is licensed CC BY-SA 4.0, so every stored bio
keeps the article link and license, and the website prints them as a visible credit.

Matching is deliberately strict. An article is used only when its title shares the
official's first and last name, it is a normal article (not a disambiguation page), and
its text says the person is a Washington or U.S. politician. Anything less is skipped and
listed, so a wrong person never ends up on a profile.

Usage
-----
  python agents/bio-sync.py --dry-run          # show matches, write nothing
  python agents/bio-sync.py                    # fill officials that have no bio yet
  python agents/bio-sync.py --refresh          # re-fetch bios older than 90 days
  python agents/bio-sync.py --only "Maria Cantwell"
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from supabase import create_client

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

API = "https://en.wikipedia.org/w/api.php"
SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
HEADERS = {"User-Agent": "CommuneUSA-bot/1.0 (https://www.communeusa.com; civic data)"}
LICENSE = "CC BY-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
MAX_CHARS = 650
REFRESH_AFTER_DAYS = 90

ROLE_WORDS = ("politician", "senator", "representative", "congress", "legislat", "governor",
              "lieutenant governor", "attorney general", "secretary of state", "treasurer",
              "auditor", "commissioner", "superintendent", "lawyer", "businessman", "businesswoman")
PLACE_WORDS = ("washington", "united states", "american")
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def name_tokens(name: str) -> list[str]:
    name = re.sub(r"\(.*?\)|\".*?\"", " ", name)
    toks = [t.lower().strip(".,") for t in re.split(r"[\s]+", name) if t.strip(".,")]
    return [t for t in toks if t not in SUFFIXES]


def title_matches(official: str, title: str) -> bool:
    """First and last name of the official must both appear in the article title."""
    o, t = name_tokens(official), set(name_tokens(re.sub(r"\(.*?\)", " ", title)))
    if len(o) < 2:
        return False
    return o[0] in t and o[-1] in t


def trim(text: str) -> str:
    """First sentences up to MAX_CHARS, ending on a sentence boundary."""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*\((?:[^()]*?(?:born|pronounced|/)[^()]*?)\)", "", text, count=1)
    if len(text) <= MAX_CHARS:
        return text
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    out = ""
    for s in sentences:
        if out and len(out) + len(s) + 1 > MAX_CHARS:
            break
        out = f"{out} {s}".strip()
    return out[:MAX_CHARS].rsplit(" ", 1)[0] + "." if len(out) > MAX_CHARS else out


def find_bio(name: str) -> tuple[dict | None, str]:
    """Return (match, reason). match has title, url, text."""
    try:
        r = requests.get(API, headers=HEADERS, timeout=30, params={
            "action": "query", "list": "search", "format": "json", "srlimit": 5,
            "srsearch": f'"{name}" Washington politician'})
        r.raise_for_status()
        hits = [h["title"] for h in r.json().get("query", {}).get("search", [])]
    except (requests.RequestException, ValueError) as exc:
        return None, f"search failed: {exc}"
    for title in hits:
        if not title_matches(name, title):
            continue
        try:
            s = requests.get(SUMMARY + urllib.parse.quote(title.replace(" ", "_"), safe=""),
                             headers=HEADERS, timeout=30)
            if s.status_code != 200:
                continue
            j = s.json()
        except (requests.RequestException, ValueError):
            continue
        if j.get("type") != "standard":
            continue
        extract = j.get("extract") or ""
        low = extract.lower()
        if not (any(w in low for w in ROLE_WORDS) and any(w in low for w in PLACE_WORDS)):
            continue
        url = j.get("content_urls", {}).get("desktop", {}).get("page") or \
            "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
        return {"title": j.get("title", title), "url": url, "text": trim(extract)}, "ok"
    return None, "no confident match"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--only")
    args = ap.parse_args()

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    rows, start = [], 0
    while True:
        q = (sb.table("officials")
             .select("id, official_name, level, bio_summary, bio_source, bio_updated_at")
             .eq("is_active", True).in_("level", ["federal", "state"])
             .range(start, start + 999))
        batch = q.execute().data or []
        rows += batch
        if len(batch) < 1000:
            break
        start += 1000

    cutoff = datetime.now(timezone.utc) - timedelta(days=REFRESH_AFTER_DAYS)
    todo = []
    for r in rows:
        if args.only and r["official_name"] != args.only:
            continue
        if r.get("bio_summary") and r.get("bio_source") != "Wikipedia":
            continue  # a hand-written or other-source bio is never overwritten
        if r.get("bio_summary") and not args.refresh:
            continue
        if r.get("bio_summary") and r.get("bio_updated_at"):
            if datetime.fromisoformat(r["bio_updated_at"].replace("Z", "+00:00")) > cutoff:
                continue
        todo.append(r)

    log.info("%d officials to look up (of %d active federal and state)", len(todo), len(rows))
    matched = skipped = 0
    skipped_names = []
    for r in todo:
        m, why = find_bio(r["official_name"])
        time.sleep(0.3)
        if not m:
            skipped += 1
            skipped_names.append(f'{r["official_name"]} ({why})')
            continue
        matched += 1
        log.info("%s -> %s", r["official_name"], m["title"])
        log.info("    %s", m["text"][:200])
        if not args.dry_run:
            sb.table("officials").update({
                "bio_summary": m["text"], "bio_source": "Wikipedia", "bio_source_url": m["url"],
                "bio_license": LICENSE, "bio_updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", r["id"]).execute()

    log.info("Matched %d, skipped %d%s", matched, skipped, " (dry run, nothing written)" if args.dry_run else "")
    for n in skipped_names:
        log.info("  skipped: %s", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
