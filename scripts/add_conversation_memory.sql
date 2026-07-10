-- ═══════════════════════════════════════════════════════════════════════════════
-- DG CLINIC — V2 CONVERSATION MEMORY MIGRATION
-- Persists the doctor's chat transcript so a Railway redeploy no longer wipes
-- mid-conversation context (V1/agent-loop memory lived in process RAM).
-- Run in: Supabase Dashboard → SQL Editor → New Query → Run
-- Safe to run more than once.
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS conversation_messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wa_number   TEXT NOT NULL,                     -- doctor's WhatsApp number
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Newest-first lookups per conversation (load the last N recent messages).
CREATE INDEX IF NOT EXISTS idx_conversation_messages_wa
    ON conversation_messages (wa_number, created_at DESC);
