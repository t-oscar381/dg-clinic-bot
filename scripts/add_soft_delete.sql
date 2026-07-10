-- ═══════════════════════════════════════════════════════════════════════════════
-- DG CLINIC — V2 SOFT-DELETE MIGRATION
-- Adds deleted_at to treatment_logs so undo_last_visit can soft-delete a visit
-- (recoverable + auditable) instead of destroying the row.
-- Run in: Supabase Dashboard → SQL Editor → New Query → Run
-- Safe to run more than once.
-- ═══════════════════════════════════════════════════════════════════════════════

ALTER TABLE treatment_logs
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- Partial index so "latest active log" lookups stay fast as soft-deletes accrue.
CREATE INDEX IF NOT EXISTS idx_treatment_logs_active
    ON treatment_logs (patient_id, date DESC)
    WHERE deleted_at IS NULL;
