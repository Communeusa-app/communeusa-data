-- ─────────────────────────────────────────────────────────────────────────────
-- CommuneUSA — add_corrections.sql
-- Stores user-submitted data corrections for review by the editorial team.
-- ─────────────────────────────────────────────────────────────────────────────

create table corrections (
  id               uuid        primary key default gen_random_uuid(),
  entity_type      text        not null,             -- official | election | candidate | law_enforcement | ...
  entity_id        text,                             -- UUID of the record being corrected (nullable for general)
  entity_name      text,                             -- human-readable name of what is being corrected
  field_name       text,                             -- which specific field is wrong
  current_value    text,                             -- what it currently says
  suggested_value  text        not null,             -- what it should say
  reason           text,                             -- why this correction is needed
  submitter_email  text,                             -- optional contact for follow-up
  status           text        not null default 'pending',   -- pending | reviewed | accepted | rejected
  source_url       text,                             -- link to official source proving the correction
  created_at       timestamptz not null default now(),
  reviewed_at      timestamptz
);

create index idx_corrections_entity_type   on corrections (entity_type);
create index idx_corrections_entity_id     on corrections (entity_id) where entity_id is not null;
create index idx_corrections_status        on corrections (status);
create index idx_corrections_created_at    on corrections (created_at desc);

-- ── Row-level security ────────────────────────────────────────────────────────

alter table corrections enable row level security;

-- Public users may INSERT (submit corrections) but not read, update, or delete.
create policy "Public can submit corrections"
  on corrections
  for insert
  with check (true);

-- The service role (admin scripts / edge functions) bypasses RLS entirely.
-- No additional SELECT/UPDATE policies needed for anon users.
