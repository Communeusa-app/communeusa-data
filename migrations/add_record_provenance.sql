-- Where each official came from and when a sync last confirmed it.
-- Existing rows stay null until the next sync run stamps them, so nothing is claimed that has not been checked.

alter table officials
  add column if not exists source           text,
  add column if not exists source_url       text,
  add column if not exists last_verified_at timestamptz;

comment on column officials.source is 'Name of the directory or API the record was last confirmed against';
comment on column officials.source_url is 'Link to that source';
comment on column officials.last_verified_at is 'When a sync last found this record in the source';

create index if not exists officials_last_verified_idx on officials (last_verified_at);
