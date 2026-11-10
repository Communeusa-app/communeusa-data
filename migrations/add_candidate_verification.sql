-- Migration: track where each candidate came from, and store official results.
-- Run in the Supabase SQL editor. Safe to run more than once.
-- No new tables, so existing RLS and the public_read policies still apply.

alter table candidates
  add column if not exists source      text,
  add column if not exists status      text not null default 'unverified',
  add column if not exists votes       integer,
  add column if not exists vote_pct    numeric(5,2),
  add column if not exists verified_at timestamptz;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'candidates_status_check') then
    alter table candidates
      add constraint candidates_status_check check (status in ('verified', 'unverified'));
  end if;
end $$;

alter table elections
  add column if not exists results_status     text,
  add column if not exists results_updated_at timestamptz;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'elections_results_status_check') then
    alter table elections
      add constraint elections_results_status_check
      check (results_status is null or results_status in ('unofficial', 'certified'));
  end if;
end $$;

comment on column candidates.source is
  'Where the candidate row came from: wa_sos (official results) or null for earlier imports.';
comment on column candidates.status is
  'verified = confirmed against official results; unverified = not yet confirmed. The site shows only verified candidates.';
comment on column candidates.votes is 'Votes counted so far (unofficial until the election is certified).';
comment on column elections.results_status is 'unofficial while counting, certified once the state certifies results.';
