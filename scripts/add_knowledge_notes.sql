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

-- ── Row Level Security ────────────────────────────────────────────────────────
-- The bot writes with the service_role key (bypasses RLS). The admin page reads
-- with the anon key + the signed-in admin's JWT, so it needs the same
-- admins-allowlist policy the `posts` table uses. Read + update (soft-delete
-- sets deleted_at, and future edits set revised_at) for signed-in admins only.
ALTER TABLE knowledge_notes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "admins read knowledge" ON knowledge_notes;
CREATE POLICY "admins read knowledge" ON knowledge_notes
    FOR SELECT TO authenticated
    USING ((auth.jwt() ->> 'email') IN (SELECT email FROM admins));

DROP POLICY IF EXISTS "admins update knowledge" ON knowledge_notes;
CREATE POLICY "admins update knowledge" ON knowledge_notes
    FOR UPDATE TO authenticated
    USING ((auth.jwt() ->> 'email') IN (SELECT email FROM admins))
    WITH CHECK ((auth.jwt() ->> 'email') IN (SELECT email FROM admins));
