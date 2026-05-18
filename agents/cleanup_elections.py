"""
One-time cleanup: remove contaminated elections from the DB.

The elections-sync.py scraper previously followed national Ballotpedia
category pages (e.g. United_States_Senate_elections,_2026) and stored
elections for all 50 states under Washington's state_id.  This script
deletes those rows so the next scraper run starts with clean data.

Elections deleted:
  - Other-state races: office_name matches 'election(s) in [not Washington]'
  - National category pages: overviews with no specific seat
  - WA bundle pages: 'WA State Legislature …', 'Multiple WA …', etc.
  - Party primary overview pages
  - Any remaining election whose state_id is not Washington's

Candidates belonging to deleted elections are removed first.

Run:
    python3 agents/cleanup_elections.py
"""

import os
import sys
import re
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv(Path(__file__).parent.parent / ".env")

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
if not supabase_url or not supabase_key:
    sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")

sb: Client = create_client(supabase_url, supabase_key)

# ── Resolve WA state_id ────────────────────────────────────────────────────────

res = sb.table("states").select("id").eq("abbreviation", "WA").single().execute()
if not res.data:
    sys.exit("ERROR: Washington state row not found.")
wa_id: str = res.data["id"]
print(f"Washington state_id: {wa_id}")

# ── Load all elections with state_id = WA ──────────────────────────────────────

res = sb.table("elections").select("id,office_name,level").eq("state_id", wa_id).execute()
all_elections = res.data or []
print(f"Total elections under WA state_id: {len(all_elections)}")

# ── Identify elections to delete ───────────────────────────────────────────────

# Mirrors the isWARelevantElection() logic from elections.ts
BAD_PATTERNS = [
    # Other states' named elections (contains "election(s) in" but not Washington)
    re.compile(r"\belections? in (?!washington\b)", re.IGNORECASE),
    # National category pages — no specific seat
    re.compile(r"^united states (senate|house|congress) (elections?|primaries?)$", re.IGNORECASE),
    re.compile(r"^united states house of representatives elections$", re.IGNORECASE),
    re.compile(r"party (battleground )?primaries$", re.IGNORECASE),
    re.compile(r"^special elections to the \d+", re.IGNORECASE),
    re.compile(r"^list of (congressional|u\.s\.)", re.IGNORECASE),
    # WA bundle / category pages (many seats, no specific candidates)
    re.compile(r"^wa state legislature\b", re.IGNORECASE),
    re.compile(r"^multiple wa\b", re.IGNORECASE),
    re.compile(r"^u\.s\. house.+\ball wa\b", re.IGNORECASE),
    re.compile(r"\bstate (senate|house) elections$", re.IGNORECASE),
    re.compile(r"^united states (senate|house).+elections in washington$", re.IGNORECASE),
    # Overview page stored as city-level
    re.compile(r"^washington elections$", re.IGNORECASE),
    # Non-WA state courts/legislatures that slipped through
    re.compile(r"^west virginia\b", re.IGNORECASE),
]

to_delete: list[str] = []
for e in all_elections:
    name = e.get("office_name") or ""
    if any(p.search(name) for p in BAD_PATTERNS):
        to_delete.append(e["id"])
        print(f"  DELETE  [{e['level']:7s}]  {name}")

print(f"\n{len(to_delete)} elections will be deleted ({len(all_elections) - len(to_delete)} kept)")
if not to_delete:
    print("Nothing to delete.")
    sys.exit(0)

# ── Confirm ────────────────────────────────────────────────────────────────────

answer = input("\nProceed? [y/N] ").strip().lower()
if answer != "y":
    print("Aborted.")
    sys.exit(0)

# ── Delete in batches of 50 ────────────────────────────────────────────────────

BATCH = 50

def chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]

cand_deleted = 0
elec_deleted = 0

for batch in chunks(to_delete, BATCH):
    # Delete candidates first (foreign key)
    res = sb.table("candidates").delete().in_("election_id", batch).execute()
    cand_deleted += len(res.data or [])
    # Delete elections
    res = sb.table("elections").delete().in_("id", batch).execute()
    elec_deleted += len(res.data or [])

print(f"\nDeleted {elec_deleted} elections and {cand_deleted} candidates.")
