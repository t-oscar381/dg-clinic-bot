-- ═══════════════════════════════════════════════════════════════════════════════
-- DG CLINIC — PAYMENT PROOF MIGRATION
-- Links a stored payment-screenshot (bukti transfer) to the visit it pays for.
-- The image itself lives in the private Supabase Storage bucket
-- "payment-proofs"; this column stores its path.
-- Run in: Supabase Dashboard → SQL Editor → New Query → Run
-- Safe to run more than once.
-- ═══════════════════════════════════════════════════════════════════════════════

ALTER TABLE treatment_logs
    ADD COLUMN IF NOT EXISTS payment_proof_path TEXT;
