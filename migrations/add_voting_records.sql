-- Migration: create voting_records table
-- Run in Supabase SQL editor or via psql before populating vote data.

create table voting_records (
  id                 uuid        primary key default gen_random_uuid(),
  official_id        uuid        references officials (id) on delete cascade,
  bill_name          text,
  bill_description   text,
  topic_category     text,
  vote_date          text,
  vote_cast          text,
  result             text,
  constituent_impact text,
  source_url         text,
  created_at         timestamptz not null default now()
);

create index idx_voting_records_official_id on voting_records (official_id);
create index idx_voting_records_created_at  on voting_records (created_at);

-- Dedup constraint: one vote per (official, bill, date).
-- Requires PostgreSQL 15+ (Supabase default). NULL values are treated as equal.
alter table voting_records
  add constraint voting_records_dedup
  unique nulls not distinct (official_id, bill_name, vote_date);

comment on table voting_records is
  'Individual votes cast by officials on bills and resolutions.';
comment on column voting_records.vote_cast is
  'How the official voted — e.g. Yea, Nay, Abstain, Not Voting.';
comment on column voting_records.result is
  'Outcome of the vote — e.g. Passed, Failed.';
comment on column voting_records.constituent_impact is
  'Plain-language summary of how the bill affects constituents.';
