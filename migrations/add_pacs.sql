-- ─────────────────────────────────────────────────────────────────────────────
-- Migration: add_pacs
-- Federal PAC data from the FEC: committees, direct contributions,
-- independent expenditures, and itemized donor records.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 1. pacs ──────────────────────────────────────────────────────────────────
-- One row per FEC committee. fec_committee_id is the canonical FEC identifier
-- (e.g. "C00123456"). Upserts should key on fec_committee_id.

CREATE TABLE IF NOT EXISTS pacs (
  id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  fec_committee_id   TEXT        UNIQUE NOT NULL,
  name               TEXT        NOT NULL,
  committee_type     TEXT        CHECK (committee_type IN ('pac', 'super_pac', 'hybrid_pac', 'party', 'other')),
  designation        TEXT,
  total_raised       NUMERIC,
  total_spent        NUMERIC,
  cycle              INTEGER,
  treasurer          TEXT,
  state              TEXT,
  website            TEXT,
  created_at         TIMESTAMPTZ DEFAULT now(),
  updated_at         TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pacs_fec_committee_id ON pacs (fec_committee_id);
CREATE INDEX IF NOT EXISTS idx_pacs_cycle            ON pacs (cycle);


-- ── 2. pac_contributions ──────────────────────────────────────────────────────
-- Direct PAC-to-candidate contributions. These are legally capped (currently
-- $5,000 per candidate per election for most PACs) and must be reported to the
-- FEC. official_id is nullable — not all FEC candidates have a matching row in
-- the officials table.

CREATE TABLE IF NOT EXISTS pac_contributions (
  id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  pac_id             UUID        NOT NULL REFERENCES pacs (id) ON DELETE CASCADE,
  official_id        UUID        REFERENCES officials (id),
  candidate_name     TEXT        NOT NULL,
  fec_candidate_id   TEXT,
  amount             NUMERIC     NOT NULL,
  contribution_date  DATE,
  cycle              INTEGER,
  created_at         TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pac_contributions_pac_id       ON pac_contributions (pac_id);
CREATE INDEX IF NOT EXISTS idx_pac_contributions_official_id  ON pac_contributions (official_id);
CREATE INDEX IF NOT EXISTS idx_pac_contributions_cycle        ON pac_contributions (cycle);


-- ── 3. pac_independent_expenditures ──────────────────────────────────────────
-- Super PAC and hybrid PAC spending in support of or opposition to candidates.
-- These are legally unlimited and must be uncoordinated with campaigns.
-- support_oppose is constrained to 'support' or 'oppose' (FEC field values).

CREATE TABLE IF NOT EXISTS pac_independent_expenditures (
  id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  pac_id             UUID        NOT NULL REFERENCES pacs (id) ON DELETE CASCADE,
  official_id        UUID        REFERENCES officials (id),
  candidate_name     TEXT        NOT NULL,
  fec_candidate_id   TEXT,
  amount             NUMERIC     NOT NULL,
  support_oppose     TEXT        NOT NULL CHECK (support_oppose IN ('support', 'oppose')),
  expenditure_date   DATE,
  cycle              INTEGER,
  created_at         TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pac_ie_pac_id        ON pac_independent_expenditures (pac_id);
CREATE INDEX IF NOT EXISTS idx_pac_ie_official_id   ON pac_independent_expenditures (official_id);
CREATE INDEX IF NOT EXISTS idx_pac_ie_support_oppose ON pac_independent_expenditures (support_oppose);
CREATE INDEX IF NOT EXISTS idx_pac_ie_cycle         ON pac_independent_expenditures (cycle);


-- ── 4. pac_donors ────────────────────────────────────────────────────────────
-- Itemized contributions to PACs from individuals, corporations, unions, and
-- other PACs. The FEC requires itemization for contributions over $200 per cycle.

CREATE TABLE IF NOT EXISTS pac_donors (
  id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  pac_id             UUID        NOT NULL REFERENCES pacs (id) ON DELETE CASCADE,
  donor_name         TEXT        NOT NULL,
  donor_type         TEXT        CHECK (donor_type IN ('individual', 'corporation', 'union', 'other_pac', 'other')),
  donor_employer     TEXT,
  amount             NUMERIC     NOT NULL,
  contribution_date  DATE,
  cycle              INTEGER,
  created_at         TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pac_donors_pac_id ON pac_donors (pac_id);
CREATE INDEX IF NOT EXISTS idx_pac_donors_cycle  ON pac_donors (cycle);
