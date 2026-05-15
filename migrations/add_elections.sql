-- Migration: elections, candidates, candidate_positions, candidate_endorsements
-- Run in the Supabase SQL editor before populating election data.

-- ── elections ─────────────────────────────────────────────────────────────────

create table elections (
  id               uuid        primary key default gen_random_uuid(),
  state_id         uuid        references states       (id) on delete restrict,
  county_id        uuid        references counties     (id) on delete restrict,
  municipality_id  uuid        references municipalities (id) on delete restrict,
  office_name      text        not null,
  level            text        not null check (level in ('federal', 'state', 'county', 'city')),
  election_date    text,
  primary_date     text,
  filing_deadline  text,
  description      text,
  source_url       text,
  created_at       timestamptz not null default now()
);

create index idx_elections_state_id      on elections (state_id);
create index idx_elections_level         on elections (level);
create index idx_elections_election_date on elections (election_date);

comment on table elections is
  'Upcoming and historical elections at all levels of government.';
comment on column elections.level is
  'Jurisdiction level of the race: federal, state, county, or city.';
comment on column elections.election_date is
  'General election date stored as text (e.g. "2025-11-04") for flexibility.';
comment on column elections.primary_date is
  'Primary or special-primary date, if applicable.';
comment on column elections.filing_deadline is
  'Candidate filing deadline for this race, if known.';


-- ── candidates ────────────────────────────────────────────────────────────────

create table candidates (
  id               uuid        primary key default gen_random_uuid(),
  election_id      uuid        not null references elections (id) on delete cascade,
  official_id      uuid        references officials (id) on delete set null,
  name             text        not null,
  party            text,
  photo_url        text,
  website          text,
  email            text,
  is_incumbent     boolean     not null default false,
  ballotpedia_url  text,
  created_at       timestamptz not null default now()
);

create index idx_candidates_election_id  on candidates (election_id);
create index idx_candidates_official_id  on candidates (official_id);

comment on table candidates is
  'Candidates running in a specific election.';
comment on column candidates.official_id is
  'Links to the officials table when the candidate is a known incumbent or has a profile.';
comment on column candidates.is_incumbent is
  'True when the candidate currently holds the seat being contested.';


-- ── candidate_positions ───────────────────────────────────────────────────────

create table candidate_positions (
  id                  uuid        primary key default gen_random_uuid(),
  candidate_id        uuid        not null references candidates (id) on delete cascade,
  issue_area          text,
  position_statement  text,
  source_url          text,
  created_at          timestamptz not null default now()
);

create index idx_candidate_positions_candidate_id on candidate_positions (candidate_id);

comment on table candidate_positions is
  'Policy positions and issue statements for a candidate.';
comment on column candidate_positions.issue_area is
  'Topic category (e.g. Housing, Public Safety, Transportation).';


-- ── candidate_endorsements ────────────────────────────────────────────────────

create table candidate_endorsements (
  id                uuid        primary key default gen_random_uuid(),
  candidate_id      uuid        not null references candidates (id) on delete cascade,
  endorser_name     text,
  endorser_type     text,
  endorsement_date  text,
  source_url        text,
  created_at        timestamptz not null default now()
);

create index idx_candidate_endorsements_candidate_id on candidate_endorsements (candidate_id);

comment on table candidate_endorsements is
  'Endorsements received by a candidate from individuals or organizations.';
comment on column candidate_endorsements.endorser_type is
  'Category of endorser (e.g. Organization, Elected Official, Union, Newspaper).';
