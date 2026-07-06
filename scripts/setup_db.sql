-- ═══════════════════════════════════════════════════════════════════════════════
-- DG CLINIC — SUPABASE SCHEMA
-- Run in: Supabase Dashboard → SQL Editor → New Query → Run All
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── PATIENTS ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS patients (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name       TEXT NOT NULL,
    nickname        TEXT,
    dob             DATE,
    gender          CHAR(1) CHECK (gender IN ('M','F')),
    phone           TEXT,
    vip_tier        TEXT DEFAULT 'Standard'
                         CHECK (vip_tier IN ('Standard','Silver','Gold','Platinum')),
    allergies       TEXT,
    medical_notes   TEXT,
    referral_source TEXT,
    consent_signed  BOOLEAN DEFAULT FALSE,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_patients_fullname_trgm
    ON patients USING GIN (full_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_patients_nickname_trgm
    ON patients USING GIN (nickname gin_trgm_ops);

-- ── TREATMENT LOGS ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS treatment_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    date            DATE NOT NULL DEFAULT CURRENT_DATE,
    protocol        TEXT NOT NULL,
    dosage          TEXT,
    route           TEXT CHECK (route IN ('SC','IV','IM','PO','Topical','Other')),
    notes           TEXT,
    next_visit_date DATE,
    logged_by       TEXT DEFAULT 'doctor',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_treatment_logs_patient
    ON treatment_logs (patient_id, date DESC);

-- ── PROGRESSION METRICS ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS patient_metrics (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id  UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    date        DATE NOT NULL DEFAULT CURRENT_DATE,
    weight_kg   DECIMAL(5,2),
    waist_cm    DECIMAL(5,2),
    hip_cm      DECIMAL(5,2),
    bmi         DECIMAL(4,2),
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_metrics_patient
    ON patient_metrics (patient_id, date DESC);

-- ── API COST TRACKING ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_usage (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    month           CHAR(7) NOT NULL UNIQUE,
    input_tokens    INT DEFAULT 0,
    output_tokens   INT DEFAULT 0,
    total_calls     INT DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── FUZZY PATIENT SEARCH FUNCTION ────────────────────────────────────────────
CREATE OR REPLACE FUNCTION search_patient(
    query TEXT,
    similarity_threshold FLOAT DEFAULT 0.2
)
RETURNS TABLE (
    id            UUID,
    full_name     TEXT,
    nickname      TEXT,
    dob           DATE,
    gender        CHAR(1),
    vip_tier      TEXT,
    allergies     TEXT,
    medical_notes TEXT,
    similarity    FLOAT
)
LANGUAGE SQL STABLE AS $$
    SELECT
        p.id, p.full_name, p.nickname, p.dob, p.gender,
        p.vip_tier, p.allergies, p.medical_notes,
        GREATEST(
            similarity(lower(p.full_name), lower(query)),
            similarity(lower(COALESCE(p.nickname,'')), lower(query))
        ) AS similarity
    FROM patients p
    WHERE p.is_active = TRUE
      AND (
          similarity(lower(p.full_name), lower(query)) > similarity_threshold
          OR similarity(lower(COALESCE(p.nickname,'')), lower(query)) > similarity_threshold
          OR lower(p.full_name) LIKE '%' || lower(query) || '%'
          OR lower(COALESCE(p.nickname,'')) LIKE '%' || lower(query) || '%'
      )
    ORDER BY similarity DESC
    LIMIT 5;
$$;

-- ── AUTO-UPDATE TIMESTAMP ─────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;

CREATE TRIGGER trg_patients_updated_at
    BEFORE UPDATE ON patients
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
