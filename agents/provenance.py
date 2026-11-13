"""
provenance.py

Shared helper so every official record says where it came from and when a sync last
confirmed it. Used by data-sync, county-sync and city-sync.
"""

from __future__ import annotations

from datetime import datetime, timezone

CHUNK = 150

# One place for the human-readable source names the website shows.
SOURCES = {
    "congress":  ("Congress.gov", "https://www.congress.gov/members"),
    "openstates": ("Open States", "https://openstates.org/wa/legislators/"),
    "waco":      ("Washington Association of County Officials", "https://www.countyofficials.org/m/directory/department?did=193"),
    "wsac":      ("Washington State Association of Counties", "https://wsac.org/member-directory/"),
    "mrsc":      ("MRSC city officials directory", "https://mrsc.org/research-tools/washington-city-and-town-profiles/city-officials"),
}


def fields(key: str) -> dict:
    """Provenance columns for a new or updated row."""
    name, url = SOURCES[key]
    return {"source": name, "source_url": url, "last_verified_at": datetime.now(timezone.utc).isoformat()}


def fields_for(source: str, source_url: str) -> dict:
    """Provenance columns for a source that is not in the SOURCES table."""
    return {"source": source, "source_url": source_url, "last_verified_at": datetime.now(timezone.utc).isoformat()}


def stamp(supabase, ids: list[str], key: str) -> int:
    """Mark existing rows as confirmed by their source right now. Returns the number stamped."""
    ids = [i for i in dict.fromkeys(ids) if i]
    for start in range(0, len(ids), CHUNK):
        supabase.table("officials").update(fields(key)).in_("id", ids[start:start + CHUNK]).execute()
    return len(ids)
