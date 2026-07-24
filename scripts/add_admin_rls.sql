-- ═══════════════════════════════════════════════════════════════════════════════
-- DG CLINIC — ADMIN RLS FOR PATIENT CRM
-- patients & treatment_logs had RLS ENABLED but ZERO policies (default-deny), so
-- the admin app (anon key + signed-in admin JWT) could read nothing. These
-- admins-allowlist policies let the admin page read / import / soft-delete /
-- purge patient records. The bot uses the service_role key and BYPASSES RLS, so
-- it is unaffected. Mirrors the posts / knowledge_notes policy shape.
-- Applied to the live DB via the Supabase MCP. Safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════════

ALTER TABLE patients ENABLE ROW LEVEL SECURITY;
ALTER TABLE treatment_logs ENABLE ROW LEVEL SECURITY;

-- patients: read / insert (import) / update
DROP POLICY IF EXISTS "admins read patients" ON patients;
CREATE POLICY "admins read patients" ON patients FOR SELECT TO authenticated
  USING ((auth.jwt() ->> 'email') IN (SELECT email FROM admins));
DROP POLICY IF EXISTS "admins insert patients" ON patients;
CREATE POLICY "admins insert patients" ON patients FOR INSERT TO authenticated
  WITH CHECK ((auth.jwt() ->> 'email') IN (SELECT email FROM admins));
DROP POLICY IF EXISTS "admins update patients" ON patients;
CREATE POLICY "admins update patients" ON patients FOR UPDATE TO authenticated
  USING ((auth.jwt() ->> 'email') IN (SELECT email FROM admins))
  WITH CHECK ((auth.jwt() ->> 'email') IN (SELECT email FROM admins));

-- treatment_logs: read / insert (import) / update (soft-delete, restore) / delete (purge)
DROP POLICY IF EXISTS "admins read treatment_logs" ON treatment_logs;
CREATE POLICY "admins read treatment_logs" ON treatment_logs FOR SELECT TO authenticated
  USING ((auth.jwt() ->> 'email') IN (SELECT email FROM admins));
DROP POLICY IF EXISTS "admins insert treatment_logs" ON treatment_logs;
CREATE POLICY "admins insert treatment_logs" ON treatment_logs FOR INSERT TO authenticated
  WITH CHECK ((auth.jwt() ->> 'email') IN (SELECT email FROM admins));
DROP POLICY IF EXISTS "admins update treatment_logs" ON treatment_logs;
CREATE POLICY "admins update treatment_logs" ON treatment_logs FOR UPDATE TO authenticated
  USING ((auth.jwt() ->> 'email') IN (SELECT email FROM admins))
  WITH CHECK ((auth.jwt() ->> 'email') IN (SELECT email FROM admins));
DROP POLICY IF EXISTS "admins delete treatment_logs" ON treatment_logs;
CREATE POLICY "admins delete treatment_logs" ON treatment_logs FOR DELETE TO authenticated
  USING ((auth.jwt() ->> 'email') IN (SELECT email FROM admins));
