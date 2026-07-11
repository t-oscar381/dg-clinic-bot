-- ═══════════════════════════════════════════════════════════════════════════════
-- DG CLINIC — V2 PAYMENTS / REVENUE MIGRATION
-- Gives each visit STRUCTURED money fields so revenue is queryable with SUM(),
-- instead of being trapped in the free-text notes column. Homecare practice
-- bills a treatment amount + a separate homecare/travel fee, so both are kept
-- as their own components alongside an authoritative total.
-- Run in: Supabase Dashboard → SQL Editor → New Query → Run
-- Safe to run more than once. All columns are nullable — existing rows and any
-- no-charge visit stay valid.
-- ═══════════════════════════════════════════════════════════════════════════════

ALTER TABLE treatment_logs
    ADD COLUMN IF NOT EXISTS amount_treatment NUMERIC(12,2),   -- meds / procedure cost
    ADD COLUMN IF NOT EXISTS amount_homecare  NUMERIC(12,2),   -- travel / homecare fee
    ADD COLUMN IF NOT EXISTS amount_total     NUMERIC(12,2),   -- authoritative billed total
    ADD COLUMN IF NOT EXISTS currency         TEXT DEFAULT 'IDR';

-- Speeds up "revenue for month X" style scans that filter by date and only
-- look at charged, non-deleted visits.
CREATE INDEX IF NOT EXISTS idx_treatment_logs_revenue
    ON treatment_logs (date)
    WHERE deleted_at IS NULL AND amount_total IS NOT NULL;
