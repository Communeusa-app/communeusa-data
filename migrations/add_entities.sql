-- ─────────────────────────────────────────────────────────────────────────────
-- CommuneUSA — add_entities.sql
-- Adds civic entity tables: school districts & board members, law enforcement,
-- fire/EMS, hospitals, utilities/transit, state agencies, and judiciary.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 1. school_districts ──────────────────────────────────────────────────────

create table school_districts (
  id               uuid        primary key default gen_random_uuid(),
  state_id         uuid        not null references states(id),
  county_id        uuid        references counties(id),
  name             text        not null,
  enrollment       integer,
  official_website text,
  phone            text,
  superintendent   text,
  created_at       timestamptz not null default now()
);

create index idx_school_districts_state  on school_districts (state_id);
create index idx_school_districts_county on school_districts (county_id);

-- ── 2. school_board_members ──────────────────────────────────────────────────

create table school_board_members (
  id               uuid        primary key default gen_random_uuid(),
  district_id      uuid        not null references school_districts(id) on delete cascade,
  name             text        not null,
  position         text,
  party            text,
  term_start       text,
  term_end         text,
  phone            text,
  email            text,
  official_website text,
  created_at       timestamptz not null default now()
);

create index idx_school_board_members_district on school_board_members (district_id);

-- ── 3. law_enforcement_agencies ──────────────────────────────────────────────

create table law_enforcement_agencies (
  id               uuid        primary key default gen_random_uuid(),
  state_id         uuid        not null references states(id),
  county_id        uuid        references counties(id),
  municipality_id  uuid        references municipalities(id),
  agency_type      text,
  name             text        not null,
  jurisdiction     text,
  chief_name       text,
  sworn_officers   integer,
  headquarters     text,
  phone            text,
  website          text,
  created_at       timestamptz not null default now()
);

create index idx_law_enforcement_state  on law_enforcement_agencies (state_id);
create index idx_law_enforcement_county on law_enforcement_agencies (county_id);

-- ── 4. fire_ems_agencies ─────────────────────────────────────────────────────

create table fire_ems_agencies (
  id               uuid        primary key default gen_random_uuid(),
  state_id         uuid        not null references states(id),
  county_id        uuid        references counties(id),
  municipality_id  uuid        references municipalities(id),
  agency_type      text,
  name             text        not null,
  jurisdiction     text,
  fire_chief       text,
  stations         integer,
  personnel        integer,
  headquarters     text,
  phone            text,
  website          text,
  service_type     text,
  created_at       timestamptz not null default now()
);

create index idx_fire_ems_state  on fire_ems_agencies (state_id);
create index idx_fire_ems_county on fire_ems_agencies (county_id);

-- ── 5. hospitals ─────────────────────────────────────────────────────────────

create table hospitals (
  id               uuid        primary key default gen_random_uuid(),
  state_id         uuid        not null references states(id),
  county_id        uuid        references counties(id),
  municipality_id  uuid        references municipalities(id),
  ownership_type   text,
  name             text        not null,
  beds             integer,
  trauma_level     text,
  health_system    text,
  ceo              text,
  phone            text,
  website          text,
  created_at       timestamptz not null default now()
);

create index idx_hospitals_state  on hospitals (state_id);
create index idx_hospitals_county on hospitals (county_id);

-- ── 6. utilities_transit ─────────────────────────────────────────────────────

create table utilities_transit (
  id               uuid        primary key default gen_random_uuid(),
  state_id         uuid        not null references states(id),
  county_id        uuid        references counties(id),
  category         text,
  name             text        not null,
  service_type     text,
  customers_riders text,
  ceo              text,
  phone            text,
  website          text,
  governing_board  text,
  created_at       timestamptz not null default now()
);

create index idx_utilities_transit_state  on utilities_transit (state_id);
create index idx_utilities_transit_county on utilities_transit (county_id);

-- ── 7. state_agencies ────────────────────────────────────────────────────────

create table state_agencies (
  id               uuid        primary key default gen_random_uuid(),
  state_id         uuid        not null references states(id),
  category         text,
  name             text        not null,
  abbreviation     text,
  director         text,
  selection_method text,
  budget           text,
  employees        text,
  headquarters     text,
  phone            text,
  website          text,
  mission          text,
  created_at       timestamptz not null default now()
);

create index idx_state_agencies_state on state_agencies (state_id);

-- ── 8. judiciary ─────────────────────────────────────────────────────────────

create table judiciary (
  id               uuid        primary key default gen_random_uuid(),
  state_id         uuid        not null references states(id),
  county_id        uuid        references counties(id),
  court_level      text,
  court_name       text,
  position         text,
  judge_name       text,
  selection_method text,
  appointed_by     text,
  term_start       text,
  term_end         text,
  jurisdiction     text,
  official_website text,
  created_at       timestamptz not null default now()
);

create index idx_judiciary_state  on judiciary (state_id);
create index idx_judiciary_county on judiciary (county_id);
