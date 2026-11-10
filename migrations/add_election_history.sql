-- Migration: past elections. Each primary and general election is its own set of race rows,
-- told apart by election_date and election_type. Safe to run more than once. No new tables,
-- so RLS is unchanged.

alter table elections
  add column if not exists election_type text;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'elections_election_type_check') then
    alter table elections
      add constraint elections_election_type_check
      check (election_type is null or election_type in ('primary', 'general', 'special'));
  end if;
end $$;

-- Existing 2026 general election rows
update elections set election_type = 'general'
where election_type is null and election_date = '2026-11-03';

alter table candidates
  add column if not exists outcome text;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'candidates_outcome_check') then
    alter table candidates
      add constraint candidates_outcome_check
      check (outcome is null or outcome in ('advanced', 'won'));
  end if;
end $$;

create index if not exists elections_date_idx on elections (election_date);
