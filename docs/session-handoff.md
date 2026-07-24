# Robby / DG Clinic — Session Handoff

Paste this as the first message in a new session ("Here's the project state, continue
from here"), or rely on the auto-loaded memory file `dg-clinic-bot-v2.md`. Dates are
absolute. Today's baseline: **2026-07-23**.

## What the project is
**Robby** — a WhatsApp AI assistant for DG Clinic (Jakarta homecare aesthetic/wellness).
Doctors message it on WhatsApp (text or voice, Indonesian/English); it structures those
messages into a Supabase database. An admin dashboard (separate Next.js site) reads/manages
that data. Owner/dev: Tommy (tomzzz81@gmail.com).

## Two codebases, one database
- **`dg-clinic-bot/`** — Python FastAPI bot. Deployed on **Railway** (service `dg-clinic-bot`,
  project `exquisite-luck`, https://dg-clinic-bot-production.up.railway.app), auto-deploys from
  GitHub `t-oscar381/dg-clinic-bot` `main`. Uses Supabase **service_role** key (bypasses RLS).
- **`dg-clinic/`** — Next.js admin site (`/dgc-master`). Deployed on **Vercel**, auto-deploys
  from GitHub `t-oscar381/dg-clinic` `main`. Uses Supabase **anon key + RLS** (admins email
  allowlist in an `admins` table).
- **Supabase project `zkivvqugzehdbytuhnkv`** ("WhatsApp AI Medical Assistant"). A second
  project "DG Medika" (`wzyjdmzxobcqmpggepfw`, created 2026-07-17) exists but is **unused** by
  these apps — confirm whether that's intentional.
- The **Supabase MCP is connected** and can apply migrations directly — more reliable than
  hand-running SQL (which failed/was forgotten twice for the payments migration).

## Architecture (bot)
- `app/routes/webhook.py` — Meta webhook. Security gate = `allowed_numbers`
  (`DOCTOR_WHATSAPP_NUMBER` ∪ `KNOWLEDGE_MODE_NUMBERS`). Routes: knowledge numbers →
  `_handle_knowledge`; else text→agent (or `/recap`), audio→voice, image→payment proof.
- `app/services/agent.py` — the CRM agentic tool loop (Claude + 7 tools: search_patient,
  get_patient_history, log_visit, update_patient, undo_last_visit, get_daily_recap,
  attach_payment_proof). Model `CLAUDE_MODEL` (prod = claude-sonnet-5), thinking off, prompt
  caching on. `sender` + `proof_path` injected server-side (never model-controlled).
- `app/services/memory.py` — Postgres chat memory (`conversation_messages`), keyed by sender.
- `app/services/voice.py` + `media.py` — Graph API media fetch → Groq Whisper transcription.
- `app/services/proof.py` — payment screenshot → Supabase Storage (private `payment-proofs`
  bucket) + Claude vision extraction.
- `app/services/knowledge.py` — Dr. Denish's "second brain": capture_note (Haiku tidy →
  topic/knowledge/key_message, verbatim raw_text) + search_notes.
- `app/services/patient.py` — all DB ops + WhatsApp formatters.

## What's LIVE and verified
- Agentic CRM logging, Postgres memory, voice notes (Groq), daily `/recap`, structured revenue
  (amount_treatment/homecare/total), payment-proof screenshots. Bot went live ~2026-07-11/12.
- Multi-doctor: `DOCTOR_WHATSAPP_NUMBER` is a comma-separated allowlist (no cap). Each doctor
  gets an isolated session; `treatment_logs.logged_by` records the authoring number.
- Knowledge Bank Part 1 (bot capture) + Part 2 (admin tab) — live-verified end to end.
- Admin Patient Records tab: date-range view, CSV export (intake format), add-only import,
  soft-delete + 30-day recycle bin. Build-verified; NOT visually verified (auth-gated).

## Railway env (prod)
`DOCTOR_WHATSAPP_NUMBER=628119856889,6281802686305,62811969866` ·
`KNOWLEDGE_MODE_NUMBERS=14016882393` (Dr. Denish → knowledge mode) ·
`CLAUDE_MODEL=claude-sonnet-5` · `KNOWLEDGE_MODEL=claude-haiku-4-5` · `ENABLE_THINKING=false` ·
`DEBUG=true` (turn off once stable) · plus WhatsApp/Anthropic/Supabase/Groq secrets.

## Migrations (all in `dg-clinic-bot/scripts/`, applied to live DB)
`setup_db.sql` (base) · `add_soft_delete.sql` · `add_conversation_memory.sql` ·
`add_payments.sql` · `add_knowledge_notes.sql` (+RLS) · `add_admin_rls.sql`.
**Still NOT applied:** `add_payment_proof.sql` (adds `treatment_logs.payment_proof_path`) —
the attach-proof tool errors until this runs.

## Known open items / next work
1. **Run `add_payment_proof.sql`**; then live-verify the payment + screenshot flow end to end.
2. **Data hygiene**: protocol names fragment (`NAD+` vs `NAD+ IV`), some patients are
   relationship-named ("Mother of Marsha/Barry", "anaknya zoomi"). Canonical vocabulary =
   the **Daftar Pusaka** sheet in the Dokumen Hati Gembira (~92 packages).
3. **Schema+bot EXPANSION** (the big value unlock): capture the full Hati Gembira intake fields
   so the admin CSV export stops having ~10 blank columns. Add visit_type (Telemedis/HS/OTS/
   Radiant/Jaga), km_bracket (0-<8/8-<15/15-<25/25-35/>35 km), address, wa_name, diagnosis,
   oral_meds, delivery, procedures, chief_complaint, vitals; teach the bot to extract them.
   NOTE the logbook FUSES clinical + payroll (each row is a visit AND a salary line) — decide
   whether payroll stays in the existing finance-simulator or merges.
4. **RAG** on the Knowledge Bank (pgvector + embeddings) — only if Denish wants Robby to
   *answer* from notes.
5. **Multi-clinic** (`clinic_id` columns + `clinic_users` table) — deferred until a 2nd customer.
6. **Cost**: `api_usage` logs input/output but NOT cache-read tokens (overstates cost).
7. **Security cleanup**: turn off `DEBUG` in prod; several provider keys were printed to an
   earlier chat transcript and Tommy planned to rotate them (`SUPABASE_KEY`/service role,
   `WHATSAPP_TOKEN`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `WHATSAPP_APP_SECRET`).

## Working style / preferences (Tommy)
- Confines each task to the named folder; review-then-copy workflow. Wants plain-language
  explanations (the doctor must approve plans — avoid jargon like "RAG/pgvector" in
  doctor-facing text). Values pushback on premature optimization. Local venv anthropic was
  upgraded to 0.116; local `.env` `CLAUDE_MODEL` may lag prod (override to sonnet-5 when
  sim-testing). Test harnesses live in the session scratchpad (may be cleaned between sessions).
