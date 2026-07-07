"""
Webhook Routes — DG Clinic WhatsApp Bot
GET  /webhook  — Meta verification handshake (one-time setup)
POST /webhook  — Incoming WhatsApp messages (every message)
"""
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks, Query
from fastapi.responses import PlainTextResponse
from datetime import datetime, timedelta

from app.config import get_settings
from app.services import whatsapp, claude_ai, patient as patient_svc
from app.models.schemas import VisitExtraction, Patient

settings = get_settings()
router   = APIRouter()

# ── Simple in-memory state for multi-turn flows (LOG clarification) ───────────
# Key: sender_number  Value: dict with pending extraction data
# NOTE: This resets on server restart. Replace with Supabase for production.
_pending_log: dict[str, dict] = {}

# ── Pending VISIT match confirmations ──────────────────────────────────────────
# Key: sender_number  Value: {"extraction": dict, "candidates": list[dict]}
# Used when a visit narrative has a POSSIBLE (not confident) patient match
# and we're waiting on the doctor to pick a number or say "new".
_pending_visit_match: dict[str, dict] = {}

# ── ACTIVE PATIENT SESSION CACHE ────────────────────────────────────────────────
# Key: sender_number  Value: {"patient": dict, "updated_at": datetime}
# Remembers who the doctor was just talking about, so follow-up messages
# ("gimana dia?", "kasih juga NAD+ minggu depan") don't need the name repeated.
# Expires after ACTIVE_PATIENT_TTL_MINUTES of inactivity — old context should
# not silently apply to an unrelated message hours or days later.
_active_patient: dict[str, dict] = {}
ACTIVE_PATIENT_TTL_MINUTES = 30

# ── Pending PATIENT SWITCH confirmations ────────────────────────────────────────
# Key: sender_number  Value: {"extraction": dict, "new_name": str}
# Used when a message names a patient clearly DIFFERENT from the active
# cached patient — we ask before writing to make sure we don't log the
# wrong person's visit against the wrong record.
_pending_switch: dict[str, dict] = {}

# Words that signal "same patient as before" rather than a new name
_CONTINUATION_WORDS = {
    "dia", "nya", "pasien tadi", "yang tadi", "itu", "beliau",
    "him", "her", "that patient", "the same patient", "he", "she",
}


def _set_active_patient(sender: str, patient: Patient) -> None:
    _active_patient[sender] = {
        "patient": patient.model_dump(),
        "updated_at": datetime.now(),
    }


def _get_active_patient(sender: str) -> Patient | None:
    """Returns the cached active patient if present and not expired."""
    entry = _active_patient.get(sender)
    if not entry:
        return None
    age = datetime.now() - entry["updated_at"]
    if age > timedelta(minutes=ACTIVE_PATIENT_TTL_MINUTES):
        del _active_patient[sender]
        return None
    return Patient(**entry["patient"])


def _names_likely_same(name_a: str, name_b: str) -> bool:
    """
    Cheap local similarity check (no DB call) to decide whether a mentioned
    name is probably the same person as the active cached patient, or a
    clearly different person requiring a switch confirmation.
    """
    a, b = name_a.strip().lower(), name_b.strip().lower()
    if a == b:
        return True
    # One name contains the other (e.g. "Sita" vs "Sita Rahardjo")
    if a in b or b in a:
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# GET /webhook — Meta verification
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/webhook")
async def verify_webhook(
    hub_mode:         str = Query(None, alias="hub.mode"),
    hub_challenge:    str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    """
    Meta calls this URL when you first register the webhook in the developer portal.
    Must respond with hub.challenge if the verify token matches.
    """
    # Log what we received for debugging
    import sys
    expected = settings.WHATSAPP_VERIFY_TOKEN
    print(f"\n[WEBHOOK_VERIFY_DEBUG]", file=sys.stderr)
    print(f"  Received token: {repr(hub_verify_token)} (len={len(hub_verify_token) if hub_verify_token else 0})", file=sys.stderr)
    print(f"  Expected token: {repr(expected)} (len={len(expected)})", file=sys.stderr)
    print(f"  hub_mode: {repr(hub_mode)}", file=sys.stderr)
    print(f"  hub_challenge: {repr(hub_challenge)}", file=sys.stderr)
    print(f"  Match: {hub_verify_token == expected}", file=sys.stderr)
    
    # Check token — mode is optional for this test
    if hub_verify_token == expected and hub_challenge:
        print(f"  ✓ VERIFIED - returning challenge", file=sys.stderr)
        return PlainTextResponse(hub_challenge)
    
    print(f"  ✗ FAILED", file=sys.stderr)
    raise HTTPException(status_code=403, detail="Verify token mismatch")


# ══════════════════════════════════════════════════════════════════════════════
# POST /webhook — Incoming messages
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    """
    Meta sends every WhatsApp event here.
    MUST return 200 immediately — process in background to avoid timeout.
    """
    raw_body = await request.body()

    # ── Signature Verification ───────────────────────────────────────────────
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not whatsapp.verify_signature(raw_body, sig):
        raise HTTPException(status_code=403, detail="Invalid signature")

    body = await request.json()
    background_tasks.add_task(_handle_message, body)

    # Return 200 immediately — Meta will retry if you don't
    return {"status": "ok"}


# ══════════════════════════════════════════════════════════════════════════════
# CORE MESSAGE HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_message(body: dict) -> None:
    """
    Full message processing pipeline:
    1. Extract sender + text from webhook payload
    2. Security gate — only doctor's number allowed
    3. Mark as read
    4. Classify intent (Claude)
    5. Route to LOOKUP / LOG / HELP / UNKNOWN handler
    """
    # ── Parse webhook ────────────────────────────────────────────────────────
    parsed = whatsapp.extract_message(body)
    if not parsed:
        return                                  # Status update or non-text — skip

    sender, message_id, text = parsed

    # ── Security Gate ────────────────────────────────────────────────────────
    # ONLY the registered doctor number can use this bot.
    if sender != settings.DOCTOR_WHATSAPP_NUMBER:
        # Silently drop — don't reveal the bot exists to others
        return

    # ── Mark as read ─────────────────────────────────────────────────────────
    try:
        await whatsapp.mark_as_read(message_id)
    except Exception:
        pass                                    # Non-critical — don't block

    # ── Check pending LOG clarification ──────────────────────────────────────
    if sender in _pending_log:
        await _handle_log_clarification(sender, text)
        return

    # ── Check pending VISIT match confirmation ───────────────────────────────
    if sender in _pending_visit_match:
        await _handle_visit_match_reply(sender, text)
        return

    # ── Check pending PATIENT SWITCH confirmation ────────────────────────────
    if sender in _pending_switch:
        await _handle_switch_reply(sender, text)
        return

    # ── Intent Classification ─────────────────────────────────────────────────
    intent = claude_ai.classify_intent(text)

    if settings.DEBUG:
        print(f"[intent] {intent.intent} | patient={intent.patient_name} | conf={intent.confidence}")

    # ── Route ─────────────────────────────────────────────────────────────────
    try:
        if intent.intent == "LOOKUP":
            await _handle_lookup(sender, intent.patient_name, text)

        elif intent.intent == "LOG":
            await _handle_log(sender, text)

        elif intent.intent == "VISIT":
            await _handle_visit(sender, text)

        elif intent.intent == "HELP" or text.lower() in ("/help", "help"):
            await whatsapp.send_text(sender, patient_svc.format_help())

        else:
            await whatsapp.send_text(sender, patient_svc.format_unknown())

    except Exception as e:
        # Catch-all — never let an error leave the doctor without a response
        print(f"[error] _handle_message: {e}")
        await whatsapp.send_text(
            sender,
            "⚠️ Terjadi error. Coba lagi ya. (/help untuk panduan)"
        )


# ══════════════════════════════════════════════════════════════════════════════
# INTENT HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_lookup(sender: str, patient_name: str | None, original_text: str) -> None:
    """
    Look up a patient by name and return their profile card.
    If no name is given (or it's a pronoun/continuation reference like
    "dia"/"nya"), fall back to the cached active patient from a recent
    conversation, so the doctor doesn't have to repeat the name.
    """
    is_continuation_word = (
        patient_name and patient_name.strip().lower() in _CONTINUATION_WORDS
    )

    if not patient_name or is_continuation_word:
        cached = _get_active_patient(sender)
        if cached:
            patients, latest_log = patient_svc.lookup_patient(cached.full_name)
            if patients:
                best = patients[0]
                _set_active_patient(sender, best)   # refresh TTL
                card = patient_svc.format_patient_card(best, latest_log)
                await whatsapp.send_text(sender, card)
                return
        await whatsapp.send_text(
            sender,
            "❓ Siapa yang mau dicek? Tulis nama pasiennya.\nContoh: \"Gimana Sita?\""
        )
        return

    patients, latest_log = patient_svc.lookup_patient(patient_name)

    if not patients:
        await whatsapp.send_text(sender, patient_svc.format_not_found(patient_name))
        return

    if len(patients) > 1 and patients[0].similarity < 0.6:
        # Multiple plausible matches — show list and ask to clarify
        await whatsapp.send_text(sender, patient_svc.format_multi_match(patients))
        return

    # Best match — show full profile, and remember them as the active patient
    best = patients[0]
    _set_active_patient(sender, best)
    card = patient_svc.format_patient_card(best, latest_log)
    await whatsapp.send_text(sender, card)


async def _handle_log(sender: str, text: str) -> None:
    """
    Extract treatment data from free-text and save it.
    If data is incomplete, ask a clarifying question and wait for response.
    Falls back to the active patient cache if no name is mentioned
    (e.g. a quick follow-up right after a lookup or visit).
    """
    extraction = claude_ai.extract_treatment_log(text)

    # ── Incomplete — try the active cache before asking ──────────────────────
    if not extraction.is_complete:
        if not extraction.patient_name:
            cached = _get_active_patient(sender)
            if cached:
                extraction.patient_name = cached.full_name
                extraction.is_complete = bool(extraction.protocol)
        if not extraction.is_complete:
            if extraction.clarification_question:
                # Store partial extraction to continue on next message
                _pending_log[sender] = extraction.model_dump()
                await whatsapp.send_text(sender, extraction.clarification_question)
            else:
                await whatsapp.send_text(
                    sender,
                    "❓ Format log kurang lengkap.\n"
                    "Contoh: _Log Sita: Reta 10mg SC hari ini, next 4 weeks_"
                )
            return

    # ── Find patient in DB ────────────────────────────────────────────────────
    if not extraction.patient_name:
        await whatsapp.send_text(sender, "❓ Nama pasien tidak ditemukan di pesan.")
        return

    patients, _ = patient_svc.lookup_patient(extraction.patient_name)
    if not patients:
        await whatsapp.send_text(sender, patient_svc.format_not_found(extraction.patient_name))
        return

    best_patient = patients[0]

    # ── Multiple matches — confirm which patient ──────────────────────────────
    if len(patients) > 1 and patients[0].similarity < 0.7:
        await whatsapp.send_text(sender, patient_svc.format_multi_match(patients))
        return

    # ── Save treatment log ────────────────────────────────────────────────────
    saved_log = patient_svc.save_treatment_log(best_patient.id, extraction)
    if not saved_log:
        await whatsapp.send_text(sender, "⚠️ Gagal save ke database. Coba lagi.")
        return

    # Refresh active patient cache — this is now who we're talking about
    _set_active_patient(sender, best_patient)

    # ── Send confirmation ─────────────────────────────────────────────────────
    confirmation = patient_svc.format_log_confirmation(best_patient, saved_log)
    await whatsapp.send_text(sender, confirmation)


async def _handle_log_clarification(sender: str, clarification_text: str) -> None:
    """
    Doctor has replied to a clarification question about an incomplete log.
    Merge the reply with the pending extraction and try again.
    """
    pending = _pending_log.get(sender, {})

    # Combine original message context with clarification
    combined_message = (
        f"Original treatment note context: "
        f"Patient: {pending.get('patient_name')}, "
        f"Protocol: {pending.get('protocol')}, "
        f"Date: {pending.get('date')}. "
        f"Doctor replied with additional info: {clarification_text}"
    )

    # Re-extract with combined context
    extraction = claude_ai.extract_treatment_log(combined_message)

    # Merge known fields from pending into new extraction
    if not extraction.patient_name:
        extraction.patient_name = pending.get("patient_name")
    if not extraction.date:
        extraction.date = pending.get("date")
    if not extraction.protocol:
        extraction.protocol = pending.get("protocol")

    # Clear pending state
    del _pending_log[sender]

    # Recurse into normal log handler
    if extraction.is_complete:
        await _handle_log(sender, combined_message)
    else:
        # Still missing data — give up gracefully
        await whatsapp.send_text(
            sender,
            "❓ Masih kurang info. Coba tulis ulang lengkap:\n"
            "_Log [nama]: [protokol] [dosis] [route] hari ini, next [X] weeks_"
        )


# ══════════════════════════════════════════════════════════════════════════════
# VISIT HANDLER — narrative visit → match existing OR create new patient
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_visit(sender: str, text: str) -> None:
    """
    Doctor described a full visit in narrative form. Steps:
    1. Extract identity + visit details from the narrative (one Claude call)
    2. If incomplete (no patient name at all) — check active cache for a
       continuation reference, otherwise ask for the name
    3. If a name IS given and differs from the cached active patient —
       ask the doctor to confirm before writing (protects against logging
       to the wrong patient by mistake)
    4. Otherwise, match against existing patients as before:
       - CONFIDENT match → log visit against that patient directly
       - POSSIBLE match  → ask doctor to confirm which patient (or "new")
       - NONE            → create a new patient, then log the visit
    """
    extraction = claude_ai.extract_visit(text)

    is_continuation_word = (
        extraction.patient_name
        and extraction.patient_name.strip().lower() in _CONTINUATION_WORDS
    )

    # ── No name given, or a pronoun — try the active cache ───────────────────
    if not extraction.patient_name or is_continuation_word:
        cached = _get_active_patient(sender)
        if cached:
            extraction.patient_name = cached.full_name
            await _log_visit_to_patient(sender, cached, extraction, is_new=False)
            return
        # No cache to fall back on — ask explicitly
        await whatsapp.send_text(
            sender,
            "❓ Pasien siapa yang dimaksud? Sebutkan namanya dulu ya."
        )
        return

    if not extraction.is_complete:
        question = extraction.clarification_question or (
            "❓ Siapa nama pasiennya? Ceritakan sedikit tentang visit-nya."
        )
        await whatsapp.send_text(sender, question)
        return

    # ── A clear name is given — check against the active cache ──────────────
    cached = _get_active_patient(sender)
    if cached and not _names_likely_same(cached.full_name, extraction.patient_name):
        # Clearly a different person than who we were just discussing.
        # Ask for explicit confirmation before writing anything — this is
        # the "confirm the ground" step: never silently assume a switch,
        # and never silently ignore a possible new patient either.
        _pending_switch[sender] = {
            "extraction": extraction.model_dump(),
            "active_patient": cached.model_dump(),
        }
        await whatsapp.send_text(
            sender,
            f"🔄 Masih tentang *{cached.full_name}*, atau ini pasien baru "
            f"*{extraction.patient_name}*?\n\n"
            f"Balas *1* untuk lanjut {cached.full_name}, atau *2* untuk "
            f"pasien baru {extraction.patient_name}."
        )
        return

    await _resolve_and_log_visit(sender, extraction)


async def _resolve_and_log_visit(sender: str, extraction: VisitExtraction) -> None:
    """
    Shared matching logic used by both the normal VISIT flow and the
    post-switch-confirmation flow: match against existing patients,
    then create/log/ask as appropriate.
    """
    match = patient_svc.match_patient_from_visit(extraction)

    if match.match_tier == "CONFIDENT":
        await _log_visit_to_patient(sender, match.patient, extraction, is_new=False)

    elif match.match_tier == "POSSIBLE":
        _pending_visit_match[sender] = {
            "extraction": extraction.model_dump(),
            "candidates": [c.model_dump() for c in match.candidates],
        }
        summary = f"{extraction.patient_name} — {extraction.protocol or 'visit'}"
        await whatsapp.send_text(
            sender,
            patient_svc.format_possible_match(match.candidates, summary),
        )

    else:  # NONE — create new patient
        new_patient = patient_svc.create_patient_from_visit(extraction)
        if not new_patient:
            await whatsapp.send_text(sender, "⚠️ Gagal membuat data pasien baru. Coba lagi.")
            return
        await _log_visit_to_patient(sender, new_patient, extraction, is_new=True)


async def _handle_visit_match_reply(sender: str, reply_text: str) -> None:
    """
    Doctor replied to a "possible match" question with either:
    - A number (1, 2, 3) picking one of the candidates
    - "new" / "baru" to create a new patient instead
    """
    pending = _pending_visit_match.pop(sender, {})
    extraction = VisitExtraction(**pending.get("extraction", {}))
    candidates_data = pending.get("candidates", [])

    reply_clean = reply_text.strip().lower()

    # ── Doctor wants a new patient ────────────────────────────────────────────
    if reply_clean in ("new", "baru", "pasien baru"):
        new_patient = patient_svc.create_patient_from_visit(extraction)
        if not new_patient:
            await whatsapp.send_text(sender, "⚠️ Gagal membuat data pasien baru. Coba lagi.")
            return
        await _log_visit_to_patient(sender, new_patient, extraction, is_new=True)
        return

    # ── Doctor picked a number ────────────────────────────────────────────────
    try:
        choice_idx = int(reply_clean) - 1
        if 0 <= choice_idx < len(candidates_data):
            chosen = Patient(**candidates_data[choice_idx])
            await _log_visit_to_patient(sender, chosen, extraction, is_new=False)
            return
    except ValueError:
        pass

    # ── Unclear reply — put state back and ask again ─────────────────────────
    _pending_visit_match[sender] = pending
    await whatsapp.send_text(
        sender,
        "❓ Reply dengan nomor pasien, atau ketik *\"baru\"* untuk pasien baru."
    )


async def _log_visit_to_patient(
    sender: str,
    patient,
    extraction: VisitExtraction,
    is_new: bool,
) -> None:
    """
    Shared final step: save the visit as a treatment log against the
    given patient (new or matched), then send the appropriate confirmation.
    Also refreshes the active-patient cache so follow-up messages
    ("gimana dia?", "kasih juga NAD+ minggu depan") target the same person.
    """
    from app.models.schemas import LogExtraction

    # ── Fold the OPEN extra dict into the note text ──────────────────────────
    # treatment_logs.extra (JSONB) stores it structured too, so nothing here
    # is lost even as the doctor's narration style evolves over time.
    note_parts = [extraction.notes] if extraction.notes else []
    for key, value in (extraction.extra or {}).items():
        if value:
            label = key.replace("_", " ").capitalize()
            note_parts.append(f"{label}: {value}")
    combined_notes = " | ".join(note_parts) if note_parts else extraction.notes

    # VisitExtraction and LogExtraction share the core treatment fields —
    # reuse save_treatment_log() by converting.
    log_extraction = LogExtraction(
        patient_name=patient.full_name,
        date=extraction.date,
        protocol=extraction.protocol,
        dosage=extraction.dosage,
        route=extraction.route,
        notes=combined_notes,
        next_visit_days=extraction.next_visit_days,
        next_visit_date=extraction.next_visit_date,
        is_complete=True,
    )

    saved_log = None
    if extraction.protocol:  # only log if there's actually a treatment described
        saved_log = patient_svc.save_treatment_log(
            patient.id, log_extraction, extra=extraction.extra
        )

    # Update the active patient cache — this visit's subject becomes
    # "who we're talking about" for any follow-up messages.
    _set_active_patient(sender, patient)

    if is_new:
        confirmation = patient_svc.format_new_patient_confirmation(patient, saved_log, extraction)
    elif saved_log:
        confirmation = patient_svc.format_log_confirmation(patient, saved_log)
    else:
        confirmation = f"✅ Confirmed patient: *{patient.full_name}*\n(No treatment details to log)"

    await whatsapp.send_text(sender, confirmation)


async def _handle_switch_reply(sender: str, reply_text: str) -> None:
    """
    Doctor replied to the "still [Active] or new patient [Name]?" prompt.
    Reply "1" (or the active patient's name) → continue with cached patient.
    Reply "2" (or "baru"/"new") → proceed with the newly named patient.
    """
    pending = _pending_switch.pop(sender, {})
    extraction = VisitExtraction(**pending.get("extraction", {}))
    active_patient = Patient(**pending.get("active_patient", {}))

    reply_clean = reply_text.strip().lower()

    stay_words = {"1", "lanjut", "tetap", "masih", "stay"} | {active_patient.full_name.lower()}
    switch_words = {"2", "baru", "new", "pasien baru"} | {extraction.patient_name.lower() if extraction.patient_name else ""}

    if reply_clean in stay_words:
        # Continue logging against the ACTIVE cached patient, keeping the
        # rest of the extracted visit details (protocol, dosage, etc.)
        extraction.patient_name = active_patient.full_name
        await _log_visit_to_patient(sender, active_patient, extraction, is_new=False)
        return

    if reply_clean in switch_words:
        await _resolve_and_log_visit(sender, extraction)
        return

    # Unclear reply — restore pending state and ask again
    _pending_switch[sender] = pending
    await whatsapp.send_text(
        sender,
        f"❓ Balas *1* untuk {active_patient.full_name}, atau *2* untuk "
        f"pasien baru {extraction.patient_name}."
    )
