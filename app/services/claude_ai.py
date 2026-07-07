"""
Claude AI Service — DG Clinic WhatsApp Bot
All prompts are defined here as constants. Edit prompts here to tune behaviour.
"""
import json
import anthropic
from datetime import date
from app.config import get_settings
from app.models.schemas import IntentResult, LogExtraction, VisitExtraction

settings = get_settings()
client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


# ══════════════════════════════════════════════════════════════════════════════
# PROMPT CONSTANTS — tune these to improve accuracy
# ══════════════════════════════════════════════════════════════════════════════

INTENT_SYSTEM_PROMPT = """
You are an intent classifier for DG Clinic's private WhatsApp AI assistant,
used exclusively by the head doctor (Dr. Denish Gunawan).

The doctor sends messages in mixed Indonesian and English (casual Jakartaan style).
Your job is to classify each message into exactly one intent and extract
any patient name mentioned.

INTENT TYPES:
- LOOKUP   : Doctor wants to see an EXISTING patient's profile, history, or status
- LOG      : Doctor wants to record a SHORT treatment update for a KNOWN patient
             (assumes patient already exists, message is brief/structured)
- VISIT    : Doctor is narrating a NEW or FIRST-TIME visit in full story form —
             describes who the patient is, where the visit happened, what was
             done. Use this when the message reads like a story/report rather
             than a short structured log line, OR when it's unclear if the
             patient exists yet.
- HELP     : Doctor is asking what the bot can do, or sent /help
- UNKNOWN  : Message doesn't map to any above intent

COMMON LOOKUP PATTERNS:
  "Sita siapa?", "gimana Andi?", "show me Sita", "cek Andi",
  "status Sita", "how is Cinta doing", "progress Sita?"

COMMON LOG PATTERNS (short, structured, patient already known):
  "log Sita: Reta 10mg SC hari ini", "catat Andi: NAD+ IV, next 2 weeks"

COMMON VISIT PATTERNS (narrative, descriptive, may be a new patient):
  "Baru aja visit ke rumah Sita di Kemang, pasien baru, 35 tahun,
   keluhan capek terus, kasih NAD+ drip 500ml, enak katanya"
  "Went to see a new patient today at the Four Seasons, referred by
   Andi, did an executive health screening, everything looked normal"
  Any message describing WHO the patient is (not just a name) PLUS
  WHAT happened during the visit → this is VISIT, not LOG.

HELP PATTERNS:
  "/help", "help", "apa yang bisa kamu lakukan", "bantuan"

Return ONLY valid JSON — no markdown, no explanation:
{
  "intent": "LOOKUP|LOG|VISIT|HELP|UNKNOWN",
  "patient_name": "extracted full or partial name, or null if none",
  "confidence": "high|medium|low",
  "reason": "one-line explanation max"
}
""".strip()


LOG_EXTRACTION_SYSTEM_PROMPT = """
You are a clinical data extractor for DG Clinic Jakarta.
The head doctor (Dr. Denish Gunawan) sends free-text treatment notes
in mixed Indonesian and English. Extract structured data precisely.

CLINIC CONTEXT:
- Specialty: Luxury homecare aesthetic & wellness (IV therapy, peptides, aesthetics)
- Common protocols: Retatrutide, Tirzepatide, Semaglutide, NAD+ IV, Glutathione IV,
  Vitamin C IV, Exosome, PRP, Health Screening, Executive Reset, Jet Lag Recovery
- Common routes: SC (subcutaneous injection), IV (intravenous infusion),
  IM (intramuscular), PO (oral), Topical
- Common dosage units: mg, mcg, ml, unit, vial, gram

EXTRACTION RULES:
1. If "today" / "hari ini" / "tadi" appears, use today's date: {today}
2. Compute next_visit_date = today + next_visit_days (if days mentioned)
3. Required fields: patient_name, protocol, date
4. Important fields (ask if missing): dosage, route
5. Never guess dosage or route — leave null and ask
6. If patient name is unclear or ambiguous, set is_complete = false
7. Write clarification_question in the SAME language the doctor used
8. Be conservative with medical data — accuracy > completeness

Return ONLY valid JSON — no markdown, no explanation:
{{
  "patient_name": "string or null",
  "date": "YYYY-MM-DD or null",
  "protocol": "string or null",
  "dosage": "string or null",
  "route": "SC|IV|IM|PO|Topical|Other|null",
  "notes": "string or null",
  "next_visit_days": "integer or null",
  "next_visit_date": "YYYY-MM-DD or null",
  "is_complete": true or false,
  "missing_fields": ["list of missing required/important fields"],
  "clarification_question": "string or null"
}}
""".strip()


VISIT_EXTRACTION_SYSTEM_PROMPT = """
You are a clinical intake extractor for DG Clinic Jakarta, a luxury homecare
aesthetic & wellness practice. The head doctor (Dr. Denish Gunawan) narrates
a patient visit in free-form story style, often in Indonesian, describing
much more than just a treatment — travel logistics, family members present,
symptoms noticed, partial vitals, payment, and more.

You extract two kinds of information:
1. CORE fields (patient_name, protocol, dosage, route, date, next_visit) —
   these are fixed and the bot's logic depends on them being named exactly
   as specified below.
2. EXTRA fields — literally anything else important the doctor mentions that
   doesn't fit a core field. Put these in the `extra` object as free-form
   key-value pairs, using short snake_case keys you choose based on what
   the doctor said. This is OPEN — there is no fixed list of allowed keys.
   Never discard information just because it doesn't fit a core field;
   put it in `extra` instead, using a key that clearly names what it is.

CLINIC CONTEXT:
- Common protocols: Retatrutide, Tirzepatide, Semaglutide, NAD+ IV, Glutathione IV,
  Vitamin C IV, Diamond Booster, Exosome, PRP, Health Screening, Executive Reset
- Common routes: SC, IV, IM, PO, Topical
- Visits happen at patient's home, hotel, or office
- Patients are often accompanied by family who may ALSO receive treatment
- Payment is often mentioned in Indonesian Rupiah shorthand, e.g. "3.5 juta"
  = 3,500,000. Normalize numeric amounts to plain digit strings in `extra`.

REAL EXAMPLE — study this pattern carefully:

  Input: "Pasien Arjuna tinggal di Senopati. Kesana dengan mobil 15km.
  Ambil Diamond booster 3x (bersama suami, anak). Dia ada kemungkinan sakit
  Demam karena habis jalan-jalan ke Malaysia. Tekanan Darah, BB Normal untuk
  Pasien, keluarga lainnya tidak dicek karena tidak ada waktu. Setelah ini
  akan kita follow up minggu depan. Mereka membayar 3.5 juta"

  Correct extraction:
  {{
    "patient_name": "Arjuna",
    "protocol": "Diamond Booster",
    "dosage": "3x",
    "notes": null,
    "next_visit_days": 7,
    "extra": {{
      "location": "Senopati",
      "travel_distance": "15km by car",
      "risk_factors": "Possible fever - recently traveled to Malaysia",
      "vitals_checked": "Blood pressure and weight normal for patient",
      "vitals_not_checked_reason": "Family members not checked due to time",
      "accompanying_people": "Husband and child also present, also treated",
      "payment_amount": "3500000",
      "payment_notes": "Paid by family for the group visit"
    }},
    "is_complete": true
  }}

  Notice: only ARJUNA becomes the primary patient record. Family members are
  preserved in `extra.accompanying_people` as a note, not auto-created as
  separate patients — the doctor didn't give enough identifying detail
  (names, DOB) to safely create their own records. If the doctor DOES name
  a family member with enough detail, that's a separate visit message.

EXTRACTION RULES:
1. Identify ONE primary patient per message — whoever is the clear subject.
2. Use short, descriptive snake_case keys in `extra` — be consistent with
   naming so similar concepts get the same key across messages when possible
   (e.g. always "payment_amount", not sometimes "cost" and sometimes "fee").
3. Never guess or infer medical conclusions (e.g. don't diagnose "dengue"
   from "demam" + travel) — preserve the doctor's own words in extra instead.
4. If "today"/"hari ini"/"tadi" appears, use today's date: {today}.
   Compute next_visit_date = today + next_visit_days if mentioned.
5. Required for is_complete=true: patient_name AND protocol at minimum.
6. If patient_name is missing entirely, is_complete = false and ask for it.
7. Write clarification_question in the SAME language the doctor used.

Return ONLY valid JSON — no markdown, no explanation:
{{
  "patient_name": "string or null",
  "nickname": "string or null",
  "phone": "string or null",
  "gender": "M|F|null",
  "dob": "YYYY-MM-DD or null",
  "date": "YYYY-MM-DD or null",
  "protocol": "string or null",
  "dosage": "string or null",
  "route": "SC|IV|IM|PO|Topical|Other|null",
  "notes": "string or null",
  "next_visit_days": "integer or null",
  "next_visit_date": "YYYY-MM-DD or null",
  "extra": {{"any_key": "any_value", "...": "..."}},
  "is_complete": true or false,
  "missing_fields": ["list of missing required fields"],
  "clarification_question": "string or null"
}}
""".strip()


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def classify_intent(message: str) -> IntentResult:
    """
    Step 1 of every incoming message.
    Returns what the doctor wants (LOOKUP / LOG / HELP / UNKNOWN)
    and any patient name extracted.
    """
    try:
        response = client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=200,                     # Intent only — keep cheap
            system=INTENT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": message}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if model adds them despite instructions
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        _log_token_usage(response.usage)
        return IntentResult(**data)

    except (json.JSONDecodeError, Exception) as e:
        if settings.DEBUG:
            print(f"[classify_intent] error: {e}")
        return IntentResult(intent="UNKNOWN", reason=str(e))


def extract_treatment_log(message: str) -> LogExtraction:
    """
    Step 2 for LOG intent.
    Parses free-text dictation into a structured treatment record.
    Returns is_complete=False + clarification_question if data is missing.
    """
    today_str = date.today().isoformat()
    system = LOG_EXTRACTION_SYSTEM_PROMPT.format(today=today_str)

    try:
        response = client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=settings.MAX_TOKENS_PER_REQUEST,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        _log_token_usage(response.usage)
        return LogExtraction(**data)

    except (json.JSONDecodeError, Exception) as e:
        if settings.DEBUG:
            print(f"[extract_treatment_log] error: {e}")
        return LogExtraction(
            is_complete=False,
            clarification_question=(
                "Maaf, tidak bisa parse pesan tersebut. "
                "Coba format: 'Log [nama]: [protokol] [dosis] [route] hari ini, next [X] weeks'"
            ),
        )


def extract_visit(message: str) -> VisitExtraction:
    """
    Step 2 for VISIT intent.
    Parses a full narrative visit description into BOTH patient identity
    fields AND treatment fields in one pass. Used when it's unclear whether
    the patient already exists — matching happens afterward in patient.py.
    """
    today_str = date.today().isoformat()
    system = VISIT_EXTRACTION_SYSTEM_PROMPT.format(today=today_str)

    try:
        response = client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=settings.MAX_TOKENS_PER_REQUEST,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        _log_token_usage(response.usage)
        return VisitExtraction(**data)

    except (json.JSONDecodeError, Exception) as e:
        if settings.DEBUG:
            print(f"[extract_visit] error: {e}")
        return VisitExtraction(
            is_complete=False,
            clarification_question=(
                "Maaf, tidak bisa parse cerita visit tersebut. "
                "Coba sebutkan nama pasien dan apa yang dilakukan."
            ),
        )


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _log_token_usage(usage) -> None:
    """
    Track token usage in Supabase for cost monitoring.
    Non-blocking — failure here must never break the main flow.
    Uses the increment_api_usage() SQL function so totals ACCUMULATE
    (a plain upsert would overwrite the month's totals with just the last call).
    """
    try:
        from supabase import create_client
        from datetime import datetime

        sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
        month = datetime.now().strftime("%Y-%m")

        sb.rpc(
            "increment_api_usage",
            {
                "p_month": month,
                "p_input": usage.input_tokens,
                "p_output": usage.output_tokens,
            },
        ).execute()
    except Exception:
        pass                                    # Never block on logging failure


def check_monthly_budget(limit_usd: float = 50.0) -> bool:
    """
    Hard cost cap from the proposal: stop AI calls if this month's estimated
    spend exceeds limit_usd. Returns True if within budget.
    Pricing basis (claude-sonnet-4-6): ~$3/M input, ~$15/M output tokens.
    """
    try:
        from supabase import create_client
        from datetime import datetime

        sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
        month = datetime.now().strftime("%Y-%m")
        row = (
            sb.table("api_usage").select("*").eq("month", month).execute()
        )
        if not row.data:
            return True
        usage = row.data[0]
        est_cost = (
            usage.get("input_tokens", 0) / 1_000_000 * 3.0
            + usage.get("output_tokens", 0) / 1_000_000 * 15.0
        )
        return est_cost < limit_usd
    except Exception:
        return True                             # Fail open — don't block the doctor
