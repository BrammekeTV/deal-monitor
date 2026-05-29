-- =============================================================================
-- database/schema.sql
-- SQLite schema for the deal-monitor bot.
-- =============================================================================

-- Vinted listings that have already been seen (used for deduplication).
CREATE TABLE IF NOT EXISTS seen_listings (
    id          TEXT    PRIMARY KEY,          -- Vinted listing ID (string)
    title       TEXT    NOT NULL,
    url         TEXT    NOT NULL,
    price       REAL    NOT NULL,
    seller      TEXT,
    currency    TEXT    DEFAULT 'EUR',
    posted_at   TEXT    NOT NULL,             -- ISO-8601 UTC timestamp
    score       INTEGER DEFAULT 0,
    posted_to_discord INTEGER DEFAULT 0       -- 1 = already posted, 0 = not yet
);

-- Optional: track which search terms produced which listings.
CREATE TABLE IF NOT EXISTS listing_terms (
    listing_id  TEXT NOT NULL REFERENCES seen_listings(id) ON DELETE CASCADE,
    term        TEXT NOT NULL,
    PRIMARY KEY (listing_id, term)
);

-- Runtime filter overrides set via Discord slash commands.
CREATE TABLE IF NOT EXISTS filter_overrides (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Index to speed up deduplication checks.
CREATE INDEX IF NOT EXISTS idx_seen_posted_at
    ON seen_listings (posted_at);
