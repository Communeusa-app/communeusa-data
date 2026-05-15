create table campaign_finance (
  id              uuid        primary key default gen_random_uuid(),
  official_id     uuid        not null references officials(id) on delete cascade,
  donor_name      text,
  donor_type      text,
  amount          numeric,
  election_cycle  text,
  donation_date   text,
  industry_sector text,
  source_url      text,
  filing_source   text,
  created_at      timestamptz not null default now()
);

alter table campaign_finance
  add constraint campaign_finance_dedup
  unique nulls not distinct (official_id, donor_name, amount, donation_date);

create index campaign_finance_official_id_idx  on campaign_finance (official_id);
create index campaign_finance_election_cycle_idx on campaign_finance (election_cycle);
