-- Short background paragraph for each official, with the source and license it came from.
-- Run in the Supabase SQL editor before the bio sync or the website changes that use it.

alter table officials
  add column if not exists bio_summary    text,
  add column if not exists bio_source     text,
  add column if not exists bio_source_url text,
  add column if not exists bio_license    text,
  add column if not exists bio_updated_at timestamptz;

comment on column officials.bio_summary is 'Short background paragraph shown on the profile';
comment on column officials.bio_source is 'Where the paragraph came from, for example Wikipedia';
comment on column officials.bio_source_url is 'Link to the exact page the paragraph came from';
comment on column officials.bio_license is 'License the text is shared under, shown next to the credit';
