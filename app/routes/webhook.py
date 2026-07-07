"""
Webhook Routes — DG Clinic WhatsApp Bot
GET  /webhook  — Meta verification handshake (one-time setup)
POST /webhook  — Incoming WhatsApp messages (every message)
"""
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks, Query
from fastapi.responses import PlainTextResponse

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
    """
    if not patient_name:
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

    # Best match — show full profile
    best = patients[0]
    card = patient_svc.format_patient_card(best, latest_log)
    await whatsapp.send_text(sender, card)


async def _handle_log(sender: str, text: str) -> None:
    """
    Extract treatment data from free-text and save it.
    If data is incomplete, ask a clarifying question and wait for response.
    """
    extraction = claude_ai.extract_treatment_log(text)

    # ── Incomplete — ask for missing info ────────────────────────────────────
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
    2. If incomplete (no patient name at all) — ask for it
    3. Try to match against existing patients:
       - CONFIDENT match → log visit against that patient directly
       - POSSIBLE match  → ask doctor to confirm which patient (or "new")
       - NONE            → create a new patient, then log the visit
    """
    extraction = claude_ai.extract_visit(text)

    if not extraction.is_complete:
        question = extraction.clarification_question or (
            "❓ Siapa nama pasiennya? Ceritakan sedikit tentang visit-nya."
        )
        await whatsapp.send_text(sender, question)
        return

    match = patient_svc.match_patient_from_visit(extraction)

    if match.match_tier == "CONFIDENT":
        await _log_visit_to_patient(sender, match.patient, extraction, is_new=False)

    elif match.match_tier == "POSSIBLE":
        # Store pending state, ask doctor to confirm
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
    """
    from app.models.schemas import LogExtraction

    # VisitExtraction and LogExtraction share the treatment fields —
    # reuse save_treatment_log() by converting.
    log_extraction = LogExtraction(
        patient_name=patient.full_name,
        date=extraction.date,
        protocol=extraction.protocol,
        dosage=extraction.dosage,
        route=extraction.route,
        notes=extraction.notes,
        next_visit_days=extraction.next_visit_days,
        next_visit_date=extraction.next_visit_date,
        is_complete=True,
    )

    saved_log = None
    if extraction.protocol:  # only log if there's actually a treatment described
        saved_log = patient_svc.save_treatment_log(patient.id, log_extraction)

    if is_new:
        confirmation = patient_svc.format_new_patient_confirmation(patient, saved_log)
    elif saved_log:
        confirmation = patient_svc.format_log_confirmation(patient, saved_log)
    else:
        confirmation = f"✅ Confirmed patient: *{patient.full_name}*\n(No treatment details to log)"

    await whatsapp.send_text(sender, confirmation)
