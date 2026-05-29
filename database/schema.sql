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

-- Listings that could not be automatically identified, pending community review.
CREATE TABLE IF NOT EXISTS unidentified_listings (
    id                  TEXT    PRIMARY KEY,    -- Same as listing_id
    title               TEXT    NOT NULL,
    url                 TEXT    NOT NULL,
    price               REAL    NOT NULL,
    currency            TEXT    DEFAULT 'EUR',
    description         TEXT,
    images              TEXT,                   -- JSON array of image URLs
    confidence          TEXT    DEFAULT 'Low',
    failure_reason      TEXT,                   -- Why identification failed
    ocr_text            TEXT,                   -- OCR output from listing images
    review_message_id   TEXT,                   -- Discord message ID in review channel
    status              TEXT    DEFAULT 'pending',  -- pending | identified | dismissed | research_needed
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

-- Reference URLs submitted by community members to identify a listing.
CREATE TABLE IF NOT EXISTS reference_submissions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id          TEXT    NOT NULL REFERENCES unidentified_listings(id) ON DELETE CASCADE,
    submitted_by        TEXT    NOT NULL,       -- Discord user ID (snowflake string)
    reference_url       TEXT    NOT NULL,
    platform            TEXT,                   -- cardmarket | ebay | pricecharting | tcgplayer | other
    market_value        REAL,                   -- Price extracted from reference or message
    confirm_message_id  TEXT,                   -- Discord message ID of the bot's confirmation embed
    validated           INTEGER DEFAULT 0,      -- 0 = pending, 1 = approved, -1 = rejected
    created_at          TEXT    NOT NULL
);

-- Human-approved identification patterns used to improve future recognition.
CREATE TABLE IF NOT EXISTS identification_memory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title_pattern   TEXT    NOT NULL,           -- Normalised listing title stored as a lookup key
    card_name       TEXT    NOT NULL,
    card_set        TEXT,
    card_number     TEXT,
    language        TEXT,
    reference_url   TEXT,
    market_value    REAL,
    source_listing_id TEXT,                     -- The listing_id that triggered this memory entry
    approved_by     TEXT    NOT NULL,           -- Discord user ID
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_unidentified_status
    ON unidentified_listings (status);

CREATE INDEX IF NOT EXISTS idx_unidentified_review_msg
    ON unidentified_listings (review_message_id);

CREATE INDEX IF NOT EXISTS idx_ref_listing
    ON reference_submissions (listing_id);

CREATE INDEX IF NOT EXISTS idx_ref_confirm_msg
    ON reference_submissions (confirm_message_id);
