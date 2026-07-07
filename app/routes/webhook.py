"""
Webhook Routes — DG Clinic WhatsApp Bot
GET  /webhook  — Meta verification handshake (one-time setup)
POST /webhook  — Incoming WhatsApp messages (every message)
"""
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks, Query
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.services import whatsapp, claude_ai, patient as patient_svc

settings = get_settings()
router   = APIRouter()

# ── Simple in-memory state for multi-turn flows (LOG clarification) ───────────
# Key: sender_number  Value: dict with pending extraction data
# NOTE: This resets on server restart. Replace with Supabase for production.
_pending_log: dict[str, dict] = {}


# ══════════════════════════════════════════════════════════════════════════════
# GET /webhook — Meta verificationgit add app/routes/webhook.py
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
    if hub_mode == "subscribe" and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)
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
