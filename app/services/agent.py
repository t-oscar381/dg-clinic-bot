"""
Agentic tool loop — DG Clinic WhatsApp Bot (V2)

Replaces the V1 classify → extract → hand-rolled-routing pipeline with a single
Claude agent that drives the whole message. Claude is given a set of clinic
tools (search, history, log, update, undo, recap) and runs the
call → tool → call cycle itself, exactly like the V2 blueprint's AI Agent node,
but as first-party FastAPI + Anthropic SDK code.

Conversation memory is Postgres-backed (app/services/memory.py), so a Railway
redeploy no longer wipes mid-conversation context.

Token usage is kept minimal: the stable system+tools prefix is prompt-cached
(cache reads cost ~0.1x input), extended thinking is off by default, only a
short bounded history is resent, and max_tokens is small.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import anthropic

from app.config import get_settings
from app.services import claude_ai, memory, patient as patient_svc
from app.models.schemas import LogExtraction, VisitExtraction

settings = get_settings()

# Reuse the client + cost/billing helpers already configured in claude_ai.
client = claude_ai.client

# Cap the agentic loop so a tool-call cycle can never run away (blueprint: ~5).
MAX_ITERATIONS = 6


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """
You are the private WhatsApp assistant for Dr. Denish Gunawan, head doctor at
DG Clinic — a luxury homecare aesthetic & wellness practice in Jakarta (IV
therapy, peptides, aesthetics). You serve ONLY the doctor; there is no other user.

LANGUAGE & STYLE
- The doctor writes in mixed Indonesian and English, casual Jakartaan style.
  Reply in the same language and register he used. Keep replies short — this is
  WhatsApp. Use WhatsApp formatting only: *bold*, _italic_, and line breaks
  (never markdown tables or headings).

CLINIC CONTEXT
- Common protocols: Retatrutide, Tirzepatide, Semaglutide, NAD+ IV,
  Glutathione IV, Vitamin C IV, Exosome, PRP, Health Screening, Executive Reset.
- Routes: SC, IV, IM, PO, Topical. Dosage units: mg, mcg, ml, unit, vial, gram.
- Today's date is {today}.

TOOLS — always act through them; never invent patient data.
- Look a patient up (search_patient) BEFORE answering about them or writing to
  their record. Never answer from memory of a patient you have not looked up.
- To record a visit, use log_visit. If the doctor mentions payment, capture it
  in log_visit's amount fields (treatment vs homecare fee split when he gives
  both; otherwise the lump-sum total). Normalize rupiah shorthand to plain
  digits: "600 ribu" -> 600000, "3.5 juta" -> 3500000. Include the amount in
  your read-back so he can confirm the money too. To fix patient details, use
  update_patient.
- To reverse the last logged visit, use undo_last_visit.
- Use get_daily_recap ONLY when the doctor explicitly asks for a summary of
  the day (e.g. "gimana hari ini?", "rekap dong"). Never call it on your own
  initiative or as part of another task — there is no scheduled recap; it is
  always the doctor asking, right now. (He can also type "/recap" directly.)

SAFETY — these are non-negotiable for a clinical tool:
1. CONFIRM IDENTITY BEFORE WRITING. Before log_visit / update_patient, make sure
   you know exactly which patient. If search_patient returns more than one
   plausible match, ask the doctor which one — do not guess.
2. NEW vs EXISTING. Only create a new patient when the doctor has made clear the
   patient is new, or a search found no match. If unsure, ask.
3. NEVER GUESS DOSAGE OR ROUTE. If a visit is missing dosage or route, ask for
   it rather than inventing a value.
4. READ BACK EVERY WRITE. After log_visit, update_patient, or undo_last_visit,
   restate exactly what was saved (patient, protocol, dosage, route, date, next
   visit) so the doctor can catch a mistake immediately.
5. allergies and medical_notes are safety-critical — surface allergies whenever
   you show a patient.

Be a calm, precise clinical assistant. When something is ambiguous, ask one
short question rather than assuming.
""".strip()


# ══════════════════════════════════════════════════════════════════════════════
# TOOL DEFINITIONS
# ══════════════════════════════════════════════════════════════════════════════

TOOLS = [
    {
        "name": "search_patient",
        "description": (
            "Fuzzy-search existing patients by name or nickname. Use this before "
            "showing a patient or writing to their record. Returns candidate "
            "patients with their id, VIP tier, allergies, and last treatment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name or nickname to search for"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_patient_history",
        "description": "Get a patient's profile plus their most recent treatment logs, by patient_id (from search_patient).",
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
            },
            "required": ["patient_id"],
        },
    },
    {
        "name": "log_visit",
        "description": (
            "Record a treatment/visit. To log against an EXISTING patient, pass "
            "patient_id. To create a NEW patient and log in one step, pass "
            "new_patient (only when you are sure the patient is new). Always "
            "confirm identity and never guess dosage/route."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string", "description": "Existing patient to log against"},
                "new_patient": {
                    "type": "object",
                    "description": "Only when creating a brand-new patient",
                    "properties": {
                        "full_name": {"type": "string"},
                        "phone": {"type": "string"},
                        "gender": {"type": "string", "enum": ["M", "F"]},
                        "dob": {"type": "string", "description": "YYYY-MM-DD"},
                        "vip_tier": {"type": "string", "enum": ["Standard", "Silver", "Gold", "Platinum"]},
                        "allergies": {"type": "string"},
                        "medical_notes": {"type": "string"},
                        "referral_source": {"type": "string"},
                    },
                    "required": ["full_name"],
                },
                "protocol": {"type": "string"},
                "dosage": {"type": "string"},
                "route": {"type": "string", "enum": ["SC", "IV", "IM", "PO", "Topical", "Other"]},
                "date": {"type": "string", "description": "Visit date YYYY-MM-DD; defaults to today"},
                "notes": {"type": "string"},
                "next_visit_days": {"type": "integer", "description": "Days until the next visit, if mentioned"},
                "amount_treatment": {
                    "type": "number",
                    "description": (
                        "Treatment/meds cost in rupiah, plain digits. Normalize "
                        "shorthand: '600 ribu' -> 600000, '3.5 juta' -> 3500000."
                    ),
                },
                "amount_homecare": {
                    "type": "number",
                    "description": "Separate homecare/travel fee in rupiah, plain digits (normalize as above).",
                },
                "amount_total": {
                    "type": "number",
                    "description": (
                        "Total billed in rupiah, plain digits. Send this when the "
                        "doctor gives only a lump sum with no treatment/homecare "
                        "split. If you send the two components, you may omit this "
                        "— it is summed for you."
                    ),
                },
            },
            "required": ["protocol"],
        },
    },
    {
        "name": "update_patient",
        "description": "Update details on an existing patient (allergies, medical_notes, vip_tier, phone, nickname, gender, dob, referral_source). Only send fields that should change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "nickname": {"type": "string"},
                "phone": {"type": "string"},
                "gender": {"type": "string", "enum": ["M", "F"]},
                "dob": {"type": "string", "description": "YYYY-MM-DD"},
                "vip_tier": {"type": "string", "enum": ["Standard", "Silver", "Gold", "Platinum"]},
                "allergies": {"type": "string"},
                "medical_notes": {"type": "string"},
                "referral_source": {"type": "string"},
            },
            "required": ["patient_id"],
        },
    },
    {
        "name": "undo_last_visit",
        "description": "Soft-delete a patient's most recent treatment log (reversible). Use when the doctor says to undo/cancel the last visit. Read back what was removed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
            },
            "required": ["patient_id"],
        },
    },
    {
        "name": "get_daily_recap",
        "description": (
            "Summarise a day's activity: number of visits, protocol breakdown, "
            "patients seen, and who is due for a next visit tomorrow. Call this "
            "ONLY when the doctor explicitly asks for a recap/summary right now "
            "— never proactively and never as a scheduled action."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD; defaults to today"},
            },
        },
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# TOOL EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def _patient_brief(p) -> dict:
    return {
        "patient_id": p.id,
        "full_name": p.full_name,
        "nickname": p.nickname,
        "gender": p.gender,
        "vip_tier": p.vip_tier,
        "allergies": p.allergies,
        "medical_notes": p.medical_notes,
        "similarity": round(p.similarity, 2) if p.similarity is not None else None,
    }


def _next_visit_date(date_str: str | None, next_visit_days: int | None) -> str | None:
    if not next_visit_days:
        return None
    base = date.fromisoformat(date_str) if date_str else date.today()
    return (base + timedelta(days=next_visit_days)).isoformat()


def _execute_tool(name: str, tool_input: dict, sender: str = "doctor") -> dict:
    """Dispatch one tool call to the patient service. Returns a JSON-able dict.
    `sender` is the authenticated doctor's wa_number, injected server-side from
    the webhook gate — never taken from the model — and used for authorship."""
    if name == "search_patient":
        patients, _ = patient_svc.lookup_patient(tool_input["query"])
        return {"matches": [_patient_brief(p) for p in patients]}

    if name == "get_patient_history":
        patient, logs = patient_svc.get_patient_with_history(tool_input["patient_id"])
        if not patient:
            return {"error": "No patient with that id."}
        return {
            "patient": _patient_brief(patient),
            "recent_logs": [
                {
                    "date": l.get("date"),
                    "protocol": l.get("protocol"),
                    "dosage": l.get("dosage"),
                    "route": l.get("route"),
                    "notes": l.get("notes"),
                    "next_visit_date": l.get("next_visit_date"),
                }
                for l in logs
            ],
        }

    if name == "log_visit":
        visit_date = tool_input.get("date") or date.today().isoformat()
        nvd = _next_visit_date(visit_date, tool_input.get("next_visit_days"))

        patient_id = tool_input.get("patient_id")
        new_patient = tool_input.get("new_patient")

        if patient_id:
            patient, _ = patient_svc.get_patient_with_history(patient_id, limit=1)
            if not patient:
                return {"error": "No patient with that id — search first."}
        elif new_patient and new_patient.get("full_name"):
            ve = VisitExtraction(
                patient_name=new_patient.get("full_name"),
                phone=new_patient.get("phone"),
                gender=new_patient.get("gender"),
                dob=new_patient.get("dob"),
                vip_tier=new_patient.get("vip_tier"),
                allergies=new_patient.get("allergies"),
                medical_notes=new_patient.get("medical_notes"),
                extra={"referral_source": new_patient.get("referral_source")}
                if new_patient.get("referral_source") else {},
            )
            patient = patient_svc.create_patient_from_visit(ve)
            if not patient:
                return {"error": "Failed to create the new patient."}
        else:
            return {"error": "Provide either patient_id (existing) or new_patient.full_name."}

        # Revenue: if the doctor gave the split but no total, sum it; if only a
        # total, keep components null. Currency defaults to IDR when any charge.
        amt_t = tool_input.get("amount_treatment")
        amt_h = tool_input.get("amount_homecare")
        amt_total = tool_input.get("amount_total")
        if amt_total is None and (amt_t is not None or amt_h is not None):
            amt_total = (amt_t or 0) + (amt_h or 0)
        has_payment = amt_total is not None

        log = LogExtraction(
            patient_name=patient.full_name,
            date=visit_date,
            protocol=tool_input.get("protocol"),
            dosage=tool_input.get("dosage"),
            route=tool_input.get("route"),
            notes=tool_input.get("notes"),
            next_visit_days=tool_input.get("next_visit_days"),
            next_visit_date=nvd,
            amount_treatment=amt_t,
            amount_homecare=amt_h,
            amount_total=amt_total,
            currency="IDR" if has_payment else None,
            is_complete=True,
        )
        saved = patient_svc.save_treatment_log(patient.id, log, logged_by=sender)
        if not saved:
            return {"error": "Failed to save the treatment log."}

        return {
            "saved": True,
            "created_new_patient": bool(new_patient and not patient_id),
            "patient_id": patient.id,
            "patient": patient.full_name,
            "allergies": patient.allergies,
            "protocol": saved.protocol,
            "dosage": saved.dosage,
            "route": saved.route,
            "date": str(saved.date),
            "next_visit_date": str(saved.next_visit_date) if saved.next_visit_date else None,
            "amount_treatment": saved.amount_treatment,
            "amount_homecare": saved.amount_homecare,
            "amount_total": saved.amount_total,
            "currency": saved.currency,
        }

    if name == "update_patient":
        fields = {k: v for k, v in tool_input.items() if k != "patient_id"}
        updated = patient_svc.update_patient_fields(tool_input["patient_id"], fields)
        if not updated:
            return {"error": "Nothing to update, or patient not found."}
        return {"updated": True, "patient": _patient_brief(updated)}

    if name == "undo_last_visit":
        removed = patient_svc.soft_delete_last_log(tool_input["patient_id"])
        if not removed:
            return {"error": "No active visit to undo for that patient."}
        return {
            "undone": True,
            "protocol": removed.get("protocol"),
            "dosage": removed.get("dosage"),
            "route": removed.get("route"),
            "date": removed.get("date"),
        }

    if name == "get_daily_recap":
        return patient_svc.daily_recap(tool_input.get("date"))

    return {"error": f"Unknown tool: {name}"}


# ══════════════════════════════════════════════════════════════════════════════
# THE AGENT LOOP
# ══════════════════════════════════════════════════════════════════════════════

def _thinking_param() -> dict:
    # Sonnet 5 runs adaptive thinking when `thinking` is omitted — spend we don't
    # want by default. Disable it explicitly unless the doctor opts in via config.
    return {"type": "adaptive"} if settings.ENABLE_THINKING else {"type": "disabled"}


def run_agent(sender: str, user_text: str) -> str:
    """
    Run one full agentic turn for the doctor's message and return the reply text.
    Drives the call → tool → call cycle with the Anthropic Messages API, capped at
    MAX_ITERATIONS tool rounds.
    """
    # Cache the stable prefix (tools render before system, so a cache_control on
    # the system block caches tools+system together). The date makes it stable
    # within a day; cache reads across the turn's tool rounds cost ~0.1x input.
    system = [{
        "type": "text",
        "text": SYSTEM_PROMPT.format(today=date.today().isoformat()),
        "cache_control": {"type": "ephemeral"},
    }]
    messages: list[dict] = memory.load_history(sender) + [{"role": "user", "content": user_text}]

    final_text = ""
    try:
        for _ in range(MAX_ITERATIONS):
            response = client.messages.create(
                model=settings.CLAUDE_MODEL,
                max_tokens=settings.MAX_TOKENS_PER_REQUEST,
                system=system,
                tools=TOOLS,
                thinking=_thinking_param(),
                messages=messages,
            )
            claude_ai._log_token_usage(response.usage)

            if response.stop_reason == "tool_use":
                # Echo the assistant turn (text + tool_use blocks) back verbatim.
                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        try:
                            result = _execute_tool(block.name, block.input or {}, sender)
                        except Exception as e:            # never let one tool kill the turn
                            if settings.DEBUG:
                                print(f"[agent] tool {block.name} error: {e}")
                            result = {"error": f"Tool failed: {e}"}
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str),
                        })
                messages.append({"role": "user", "content": tool_results})
                continue

            # Normal completion — collect the text and stop.
            final_text = "".join(b.text for b in response.content if b.type == "text").strip()
            break
        else:
            # Hit the iteration cap without a clean finish.
            final_text = (
                "Maaf, ini agak rumit — bisa dipersingkat atau diulang? "
                "(bot mencapai batas langkah)"
            )

    except anthropic.APIStatusError as e:
        if claude_ai._is_insufficient_balance(e):
            return (
                "⚠️ *Sistem AI sedang tidak aktif* (credit habis).\n"
                "Pesan ini belum bisa diproses. Hubungi Tommy untuk top-up "
                "credit Anthropic API secepatnya."
            )
        if settings.DEBUG:
            print(f"[agent] API error: {e}")
        return "⚠️ Terjadi error dengan sistem AI. Coba lagi ya."
    except Exception as e:
        if settings.DEBUG:
            print(f"[agent] error: {e}")
        return "⚠️ Terjadi error. Coba lagi ya."

    if not final_text:
        final_text = "Maaf, saya belum bisa memproses itu. Coba tulis ulang ya."

    memory.append_turn(sender, user_text, final_text)
    return final_text
