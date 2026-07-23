-- ═══════════════════════════════════════════════════════════════════════════════
-- DG CLINIC — KNOWLEDGE BANK MIGRATION
-- Dr. Denish's "second brain": free-form knowledge he dictates via WhatsApp,
-- kept SEPARATE from the patient CRM. The bot (dg-clinic-bot) writes rows here;
-- the admin page (dg-clinic /dgc-master) reads them into the Knowledge Bank tab.
--
-- Each note keeps his verbatim words (raw_text) AND an AI-organized view
-- (topic / knowledge / key_message) so the admin table is tidy without ever
-- discarding what he actually said.
-- Run in: Supabase Dashboard → SQL Editor → New Query → Run
-- Safe to run more than once.
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS knowledge_notes (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    author_wa_number  TEXT NOT NULL,                       -- who dictated it
    source            TEXT NOT NULL DEFAULT 'text'
                           CHECK (source IN ('text', 'voice')),
    raw_text          TEXT NOT NULL,                       -- verbatim / transcript
    topic             TEXT,                                -- AI: short topic
    knowledge         TEXT,                                -- AI: cleaned statement
    key_message       TEXT,                                -- AI: one-line summary
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    revised_at        TIMESTAMPTZ,                         -- set when edited in admin
    deleted_at        TIMESTAMPTZ                          -- soft-delete (recoverable)
);

-- Newest-first listing per author (admin table + the bot's "cari catatan").
CREATE INDEX IF NOT EXISTS idx_knowledge_notes_author
    ON knowledge_notes (author_wa_number, created_at DESC)
    WHERE deleted_at IS NULL;
