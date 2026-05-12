-- CommuneUSA database schema
-- PostgreSQL / Supabase compatible
-- Run in order: states → counties → municipalities → officials

-- ─────────────────────────────────────────────────────────────────────────────
-- Extensions
-- ─────────────────────────────────────────────────────────────────────────────

create extension if not exists "pgcrypto";   -- provides gen_random_uuid()
create extension if not exists "pg_trgm";    -- powers gin_trgm_ops on official_name


-- ─────────────────────────────────────────────────────────────────────────────
-- states
-- ─────────────────────────────────────────────────────────────────────────────

create table states (
  id            uuid primary key default gen_random_uuid(),
  name          text not null unique,
  abbreviation  char(2) not null unique,
  created_at    timestamptz not null default now()
);

comment on table states is 'US states. Seed row: Washington / WA.';


-- ─────────────────────────────────────────────────────────────────────────────
-- counties
-- ─────────────────────────────────────────────────────────────────────────────

create table counties (
  id          uuid primary key default gen_random_uuid(),
  state_id    uuid not null references states (id) on delete restrict,
  name        text not null,
  created_at  timestamptz not null default now(),

  unique (state_id, name)
);

comment on table counties is 'Counties within a state.';

create index idx_counties_state_id on counties (state_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- municipalities
-- ─────────────────────────────────────────────────────────────────────────────

create table municipalities (
  id                  uuid primary key default gen_random_uuid(),
  county_id           uuid not null references counties (id) on delete restrict,

  name                text not null,
  -- e.g. City, Town, Code City, First-Class City
  type                text,
  population_2020     integer,
  -- derived from source data; refreshed periodically
  population_2025_est integer,
  -- e.g. Mayor-Council, Council-Manager, Commission
  government_form     text,
  year_incorporated   smallint,
  official_website    text,

  created_at          timestamptz not null default now(),

  unique (county_id, name)
);

comment on table municipalities is 'Cities, towns, and other incorporated places.';

create index idx_municipalities_county_id on municipalities (county_id);
create index idx_municipalities_name      on municipalities (name);


-- ─────────────────────────────────────────────────────────────────────────────
-- officials
-- ─────────────────────────────────────────────────────────────────────────────

create table officials (
  id               uuid primary key default gen_random_uuid(),

  -- Jurisdiction FKs — at least one must be non-null (enforced by check below)
  municipality_id  uuid references municipalities (id) on delete set null,
  county_id        uuid references counties (id) on delete set null,
  state_id         uuid references states (id) on delete set null,

  -- Governs which FK is authoritative and drives UI filtering
  level            text not null
                     check (level in ('federal', 'state', 'county', 'city')),

  -- Office metadata
  office_title     text not null,
  -- Legislative chamber or executive category (e.g. Senate, House, Council, Executive)
  office_category  text,
  official_name    text not null,
  party            text,
  district         text,

  -- Tenure
  term_start       text,  -- stored as-is from source ("Jan 2026", "2019", etc.)
  term_end         text,

  -- Contact
  phone            text,
  email            text,
  official_website text,
  ballotpedia_url  text,

  -- Key committees or legislative positions (primarily state/federal)
  key_committees   text,

  appointed_or_elected  text,
  notes                 text,

  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),

  -- At least one jurisdiction must be set
  constraint officials_has_jurisdiction check (
    municipality_id is not null
    or county_id is not null
    or state_id is not null
  )
);

comment on table officials is
  'All elected and appointed officials across federal, state, county, and city levels.';

-- Foreign key indexes
create index idx_officials_municipality_id on officials (municipality_id);
create index idx_officials_county_id       on officials (county_id);
create index idx_officials_state_id        on officials (state_id);

-- Common query / filter indexes
create index idx_officials_level         on officials (level);
create index idx_officials_party         on officials (party);
create index idx_officials_official_name on officials (official_name);
-- Supports full-text and prefix searches on name
create index idx_officials_name_trgm     on officials using gin (official_name gin_trgm_ops);


-- ─────────────────────────────────────────────────────────────────────────────
-- updated_at trigger
-- ─────────────────────────────────────────────────────────────────────────────

create or replace function set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger officials_set_updated_at
  before update on officials
  for each row execute function set_updated_at();


-- ─────────────────────────────────────────────────────────────────────────────
-- Seed data — Washington state
-- ─────────────────────────────────────────────────────────────────────────────

insert into states (name, abbreviation) values ('Washington', 'WA');
