-- Migration: add municipalities table (idempotent)
--
-- Safe to run against a DB that was seeded from schema.sql (which already has
-- the table but lacks state_id, population, and mrsc_city_id) OR a fresh DB
-- that has never run schema.sql. All statements use IF NOT EXISTS / ADD COLUMN
-- IF NOT EXISTS so re-runs are harmless.
--
-- Run in the Supabase SQL editor before running agents/city-sync.py.

-- 1. Create the table if schema.sql has not been applied yet.
create table if not exists municipalities (
  id               uuid        primary key default gen_random_uuid(),
  state_id         uuid        references states   (id) on delete restrict,
  county_id        uuid        not null references counties (id) on delete restrict,
  name             text        not null,
  type             text,
  population       integer,
  government_form  text,
  official_website text,
  created_at       timestamptz not null default now(),

  unique (county_id, name)
);

-- 2. Add columns that are absent when the table was created from schema.sql
--    (which used population_2020 / population_2025_est / year_incorporated
--    instead of the canonical single population column, and lacked state_id
--    and the MRSC stable key).

alter table municipalities add column if not exists state_id      uuid    references states (id) on delete restrict;
alter table municipalities add column if not exists population    integer;
alter table municipalities add column if not exists mrsc_city_id integer unique;

-- 3. Indexes — all idempotent.
create index if not exists idx_municipalities_county_id  on municipalities (county_id);
create index if not exists idx_municipalities_state_id   on municipalities (state_id);
create index if not exists idx_municipalities_name       on municipalities (name);
create index if not exists idx_municipalities_mrsc_id    on municipalities (mrsc_city_id);

comment on table  municipalities                     is 'Cities, towns, and other incorporated places within a county.';
comment on column municipalities.mrsc_city_id        is 'Stable integer ID from the MRSC Officials Directory. Used as the upsert key by city-sync.py.';
comment on column municipalities.type                is 'Class as reported by MRSC: First, Code, or Town.';
comment on column municipalities.population          is 'Population estimate from MRSC (refreshed on each city-sync run).';
