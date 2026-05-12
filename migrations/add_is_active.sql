-- Migration: add is_active to officials table
-- Run in Supabase SQL editor or via psql before running data-sync.py

alter table officials
  add column if not exists is_active boolean not null default true;

-- Backfill any pre-existing rows (DEFAULT covers new inserts; this is belt-and-suspenders)
update officials set is_active = true where is_active is null;

create index if not exists idx_officials_is_active on officials (is_active);

comment on column officials.is_active is
  'False when the official is no longer serving. Set by data-sync.py rather than deleting rows, preserving history.';
