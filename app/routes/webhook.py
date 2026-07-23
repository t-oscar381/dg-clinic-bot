"""
Webhook Routes — DG Clinic WhatsApp Bot (V2)
GET  /webhook  — Meta verification handshake (one-time setup)
POST /webhook  — Incoming WhatsApp messages (every message)

V2 change: every text message flows through the agentic tool loop
(app/services/agent.py) instead of the old classify → extract → route
pipeline. Voice notes (V2 phase 3) are transcribed via Groq Whisper
(app/services/voice.py), echoed back to the doctor for confirmation, then fed
into the SAME agent loop as if typed — never skip the echo, since a silent
wrong transcription writing a wrong dose is the one failure this bot can't have.

V2 phase 4: the daily recap is DOCTOR-TRIGGERED ONLY — there is no scheduled/
automatic evening push. Sending "/recap" (or "rekap"/"rekap hari ini") short-
circuits straight to a deterministic DB call + formatted reply, with no LLM
round trip — the fastest, cheapest, and most reliable way to trigger it. The
doctor can also just ask naturally ("gimana hari ini?") and the agent's
get_daily_recap tool handles it, but that tool is documented as on-demand only.
"""
import sys

from fastapi import APIRouter, Request, HTTPException, BackgroundTasks, Query
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.services import (
    whatsapp, agent, voice, media, proof, knowledge, patient as patient_svc,
)

# Exact-match trigger phrases (case-insensitive, trimmed) for the recap
# fast path. Deliberately a small fixed set — anything else ("rekap kemarin",
# "recap minggu ini") falls through to the agent, which can still call
# get_daily_recap with a specific date via natural language.
_RECAP_TRIGGERS = {
    "/recap", "/rekap",
    "recap", "rekap",
    "recap hari ini", "rekap hari ini",
    "daily recap", "rekap harian",
}

settings = get_settings()
router   = APIRouter()


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
    Meta calls this URL when the webhook is first registered in the developer
    portal. Respond with hub.challenge only if the verify token matches.
    """
    if hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN and hub_challenge:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403, detail="Verify token mismatch")


# ══════════════════════════════════════════════════════════════════════════════
# POST /webhook — Incoming messages
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    """
    Meta sends every WhatsApp event here.
    MUST return 200 immediately — process in the background to avoid a timeout.
    """
    raw_body = await request.body()

    sig = request.headers.get("X-Hub-Signature-256", "")
    if not whatsapp.verify_signature(raw_body, sig):
        raise HTTPException(status_code=403, detail="Invalid signature")

    body = await request.json()
    background_tasks.add_task(_handle_message, body)
    return {"status": "ok"}


# ══════════════════════════════════════════════════════════════════════════════
# CORE MESSAGE HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_message(body: dict) -> None:
    """
    1. Parse the webhook payload
    2. Security gate — only the doctor's number is served
    3. Mark as read
    4. Route by message type: text -> agent directly; audio -> transcribe,
       echo, then agent; anything else -> "not supported" reply
    """
    parsed = whatsapp.extract_message(body)
    if not parsed:
        return                                  # status update / nothing to reply to

    # ── Security gate — only authorized numbers may use this bot ──────────────
    # allowed_numbers = doctors (patient CRM) ∪ knowledge-mode users. Each doctor
    # gets an isolated session (memory is keyed by sender). Knowledge-mode numbers
    # are routed to the note handler instead of the CRM agent.
    if parsed.sender not in settings.allowed_numbers:
        # Still a silent drop toward the SENDER (never reveal the bot exists),
        # but log it server-side — a mis-configured allowlist otherwise looks
        # identical to "no messages arriving at all".
        print(
            f"[gate] dropped message from {parsed.sender} "
            f"(allowed: {sorted(settings.allowed_numbers) or 'NONE CONFIGURED'})",
            file=sys.stderr, flush=True,
        )
        return

    try:
        await whatsapp.mark_as_read(parsed.message_id)
    except Exception:
        pass                                    # non-critical

    is_knowledge = parsed.sender in settings.knowledge_numbers

    try:
        if is_knowledge:
            await _handle_knowledge(parsed)

        elif parsed.msg_type == "text":
            if parsed.text.strip().lower() in _RECAP_TRIGGERS:
                await _send_daily_recap(parsed.sender)
            else:
                await _run_agent_and_reply(parsed.sender, parsed.text)

        elif parsed.msg_type == "audio":
            await _handle_voice_note(parsed.sender, parsed.media_id)

        elif parsed.msg_type == "image":
            await _handle_payment_proof(parsed.sender, parsed.media_id, parsed.text)

        else:
            await _reply_unsupported(parsed.sender, parsed.unsupported_type)

    except Exception as e:
        print(f"[error] _handle_message: {e}", file=sys.stderr)
        await whatsapp.send_text(parsed.sender, "⚠️ Terjadi error. Coba lagi ya.")


async def _run_agent_and_reply(sender: str, text: str) -> None:
    reply = agent.run_agent(sender, text)
    await whatsapp.send_text(sender, reply)


async def _handle_knowledge(parsed) -> None:
    """
    Knowledge-mode routing (Dr. Denish's second brain). Text and voice become
    notes; a leading "cari catatan …" searches instead. Voice is transcribed and
    echoed first (same safety as CRM voice), then captured. Images/other types
    aren't note material yet.
    """
    if parsed.msg_type == "text":
        text = parsed.text
        if knowledge.is_search(text):
            await whatsapp.send_text(parsed.sender, knowledge.search_notes(parsed.sender, text))
        else:
            await whatsapp.send_text(parsed.sender, knowledge.capture_note(parsed.sender, text, "text"))
        return

    if parsed.msg_type == "audio":
        try:
            transcript = await voice.transcribe_voice_note(parsed.media_id)
        except voice.TranscriptionError as e:
            if settings.DEBUG:
                print(f"[knowledge] voice transcription failed: {e}")
            await whatsapp.send_text(
                parsed.sender,
                "🎤 Maaf, voice note-nya tidak bisa diproses. Coba kirim ulang."
            )
            return
        await whatsapp.send_text(parsed.sender, f"🎤 Saya dengar: _{transcript}_")
        if knowledge.is_search(transcript):
            await whatsapp.send_text(parsed.sender, knowledge.search_notes(parsed.sender, transcript))
        else:
            await whatsapp.send_text(parsed.sender, knowledge.capture_note(parsed.sender, transcript, "voice"))
        return

    await whatsapp.send_text(
        parsed.sender,
        "📎 Untuk knowledge bank, kirim teks atau voice note ya. "
        "(gambar/file belum didukung)"
    )


async def _send_daily_recap(sender: str) -> None:
    """
    Explicit, doctor-triggered daily recap ("/recap") — a direct DB call and
    formatted reply, no LLM call needed. This is the ONLY way a recap goes
    out; nothing runs on a schedule. The formatted message itself is the
    confirmation of what was compiled.
    """
    try:
        recap = patient_svc.daily_recap()
        await whatsapp.send_text(sender, patient_svc.format_daily_recap(recap))
    except Exception as e:
        print(f"[error] daily recap: {e}", file=sys.stderr)
        await whatsapp.send_text(sender, "⚠️ Gagal mengambil rekap hari ini. Coba lagi ya.")


async def _handle_voice_note(sender: str, media_id: str) -> None:
    """
    Voice-note pipeline (blueprint §5): transcribe, ECHO the transcript back
    to the doctor first, then feed it into the agent loop exactly like a typed
    message. The echo is the safety step — the doctor sees precisely what the
    bot heard before it can touch the database.
    """
    try:
        transcript = await voice.transcribe_voice_note(media_id)
    except voice.TranscriptionError as e:
        if settings.DEBUG:
            print(f"[voice] transcription failed: {e}")
        await whatsapp.send_text(
            sender,
            "🎤 Maaf, tidak bisa memproses voice note ini.\n"
            "Coba kirim ulang, atau ketik pesannya."
        )
        return

    await whatsapp.send_text(sender, f"🎤 Saya dengar: _{transcript}_")
    await _run_agent_and_reply(sender, transcript)


async def _handle_payment_proof(sender: str, media_id: str, caption: str | None) -> None:
    """
    Payment-screenshot pipeline: download -> store in the private bucket ->
    read with Claude vision -> hand to the agent as text. The agent decides
    which visit it belongs to (from conversation context), attaches it, and
    reads back what the screenshot showed — mirroring the voice-note pattern
    of extract-then-confirm before anything touches a record.
    """
    try:
        image_bytes, mime_type = await media.fetch_media(media_id)
        proof_path = proof.store_proof(image_bytes, mime_type, sender)
        extracted = proof.read_payment_screenshot(image_bytes, mime_type)
    except (media.MediaError, proof.ProofError) as e:
        if settings.DEBUG:
            print(f"[proof] failed: {e}", file=sys.stderr, flush=True)
        await whatsapp.send_text(
            sender,
            "📸 Maaf, gambar ini tidak bisa diproses. Coba kirim ulang "
            "screenshot-nya, atau ketik detail pembayarannya."
        )
        return

    synthetic = (
        "[The doctor sent a payment screenshot. It is already stored as proof. "
        f"What it shows: {extracted}]"
    )
    if caption:
        synthetic += f"\nDoctor's caption: {caption}"

    reply = agent.run_agent(sender, synthetic, proof_path=proof_path)
    await whatsapp.send_text(sender, reply)


async def _reply_unsupported(sender: str, msg_type: str | None) -> None:
    await whatsapp.send_text(
        sender,
        f"📎 Maaf, tipe pesan ini ({msg_type or 'unknown'}) belum didukung.\n"
        "Coba ketik pesannya sebagai teks."
    )
