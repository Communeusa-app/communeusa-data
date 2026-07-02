-- ── CommuneUSA — add_colleges.sql ────────────────────────────────────────────
-- Adds the colleges table and the courts table.
-- Safe to re-run (all statements use IF NOT EXISTS).

-- ── 1. colleges ──────────────────────────────────────────────────────────────

create table if not exists colleges (
  id             uuid        primary key default gen_random_uuid(),
  state_id       uuid        not null references states(id),
  county_id      uuid        references counties(id),
  name           text        not null,
  type           text,        -- university / liberal_arts / community_college / technical / for_profit
  enrollment     integer,
  president      text,
  phone          text,
  website        text,
  city           text,
  ownership_type text,
  created_at     timestamptz not null default now()
);

create index if not exists idx_colleges_state  on colleges (state_id);
create index if not exists idx_colleges_county on colleges (county_id);
create index if not exists idx_colleges_name   on colleges (name);

-- ── 2. courts ─────────────────────────────────────────────────────────────────
-- Court entities (the institution itself, not individual judges).
-- Individual judges live in the judiciary table.

create table if not exists courts (
  id           uuid        primary key default gen_random_uuid(),
  state_id     uuid        not null references states(id),
  county_id    uuid        references counties(id),
  court_level  text,        -- superior / district / municipal / appellate / supreme
  name         text        not null,
  address      text,
  phone        text,
  website      text,
  jurisdiction text,
  judge_count  integer,
  created_at   timestamptz not null default now()
);

create index if not exists idx_courts_state  on courts (state_id);
create index if not exists idx_courts_county on courts (county_id);
create index if not exists idx_courts_level  on courts (court_level);
create index if not exists idx_courts_name   on courts (name);
