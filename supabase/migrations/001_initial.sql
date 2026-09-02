-- Funding Blotter initial schema (mirrors src/store.py SQLite tables)
-- Apply in Supabase SQL Editor or via supabase CLI.

CREATE TABLE IF NOT EXISTS deals (
    id            BIGSERIAL PRIMARY KEY,
    company_key   TEXT NOT NULL,
    company       TEXT NOT NULL,
    amount_usd    DOUBLE PRECISION,
    amount_raw    TEXT,
    stage         TEXT,
    location      TEXT,
    description   TEXT,
    investors     TEXT,
    source        TEXT,
    url           TEXT,
    published_at  TEXT,
    first_seen    TEXT,
    priority      INTEGER DEFAULT 0,
    score         INTEGER DEFAULT 0,
    extracted_by  TEXT,
    sector_label  TEXT,
    sector_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_deals_key_pub ON deals (company_key, published_at);
CREATE INDEX IF NOT EXISTS idx_deals_pub ON deals (published_at);

CREATE TABLE IF NOT EXISTS company_sector (
    company_key TEXT PRIMARY KEY,
    label       TEXT,
    reason      TEXT,
    enriched_at TEXT
);

CREATE TABLE IF NOT EXISTS scanned_posts (
    url          TEXT PRIMARY KEY,
    company_key  TEXT,
    published_at TEXT,
    scanned_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_scanned_pub ON scanned_posts (published_at);

CREATE TABLE IF NOT EXISTS blocked_companies (
    company_key TEXT PRIMARY KEY,
    company     TEXT,
    blocked_at  TEXT,
    reason      TEXT
);

-- RLS: deny public access; service-role key (pipeline + Netlify Functions) bypasses RLS.
ALTER TABLE deals ENABLE ROW LEVEL SECURITY;
ALTER TABLE company_sector ENABLE ROW LEVEL SECURITY;
ALTER TABLE scanned_posts ENABLE ROW LEVEL SECURITY;
ALTER TABLE blocked_companies ENABLE ROW LEVEL SECURITY;
