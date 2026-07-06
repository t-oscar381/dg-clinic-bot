"""
Claude AI Service — DG Clinic WhatsApp Bot
All prompts are defined here as constants. Edit prompts here to tune behaviour.
"""
import json
import anthropic
from datetime import date
from app.config import get_settings
from app.models.schemas import IntentResult, LogExtraction

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
- LOOKUP   : Doctor wants to see a patient profile, history, or current status
- LOG      : Doctor wants to record a treatment, procedure, injection, or clinical note
- HELP     : Doctor is asking what the bot can do, or sent /help
- UNKNOWN  : Message doesn't map to any above intent

COMMON LOOKUP PATTERNS (Indonesian/English mix):
  "Sita siapa?", "gimana Andi?", "show me Sita", "cek Andi",
  "status Sita", "how is Cinta doing", "lihat Budi", "update Andi",
  "progress Sita?", "terakhir Andi kapan?"

COMMON LOG PATTERNS:
  "log Sita:", "catat:", "record Andi:", "Sita hari ini Reta 10mg",
  "log: [name] [treatment]", "input Andi:"

HELP PATTERNS:
  "/help", "help", "apa yang bisa kamu lakukan", "command apa saja",
  "bantuan", "cara pakai"

Return ONLY valid JSON — no markdown, no explanation:
{
  "intent": "LOOKUP|LOG|HELP|UNKNOWN",
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


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _log_token_usage(usage) -> None:
    """
    Track token usage in Supabase for cost monitoring.
    Non-blocking — failure here must never break the main flow.
    """
    try:
        from supabase import create_client
        from datetime import datetime

        sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
        month = datetime.now().strftime("%Y-%m")

        # Upsert monthly counter
        sb.table("api_usage").upsert(
            {
                "month": month,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_calls": 1,
            },
            on_conflict="month",
            # Use Supabase's increment via RPC if available; plain upsert here
        ).execute()
    except Exception:
        pass                                    # Never block on logging failure
