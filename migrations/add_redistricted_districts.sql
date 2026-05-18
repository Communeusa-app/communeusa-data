-- Migration: redistricted_districts table
-- Tracks district boundary changes detected by redistricting-sync.py.
-- Powers the "Redrawn recently" dashed border on the elections map.

create table redistricted_districts (
  id               uuid        primary key default gen_random_uuid(),
  state            varchar(2)  not null,
  district_type    text        not null check (district_type in ('congressional', 'house', 'senate')),
  district_number  varchar(10) not null,
  changed_date     date        not null,
  description      text,
  source_url       text,
  created_at       timestamptz not null default now(),

  -- One record per district per change event
  unique (state, district_type, district_number, changed_date)
);

create index idx_redistricted_state      on redistricted_districts (state, district_type);
create index idx_redistricted_changed    on redistricted_districts (changed_date);

comment on table redistricted_districts is
  'District boundary changes detected from Census TIGER/Line file updates.';
comment on column redistricted_districts.district_type is
  'congressional | house | senate';
comment on column redistricted_districts.district_number is
  'Integer district number as a string (no leading zeros), e.g. "3", "42".';
