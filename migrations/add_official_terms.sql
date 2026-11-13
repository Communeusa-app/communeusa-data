-- Public office history: one row per stretch of service in an office.
-- Run in the Supabase SQL editor before the website and syncs that use it are deployed.

create table if not exists official_terms (
  id           uuid        primary key default gen_random_uuid(),
  official_id  uuid        not null references officials (id) on delete cascade,
  office       text        not null,
  start_year   int         not null,
  end_year     int,                      -- null means still serving
  how_assumed  text        check (how_assumed in ('elected', 'appointed')),
  note         text,
  source       text,
  source_url   text,
  created_at   timestamptz not null default now(),
  unique (official_id, office, start_year)
);

create index if not exists official_terms_official_idx on official_terms (official_id, start_year desc);

alter table official_terms enable row level security;
create policy public_read on official_terms for select using (true);
