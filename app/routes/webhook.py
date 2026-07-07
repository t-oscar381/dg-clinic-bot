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

# ── Pending VISIT clarification ─────────────────────────────────────────────────
# Key: sender_number  Value: extraction dict (the ORIGINAL partial narrative
# extraction, e.g. patient_name="Dicky", protocol="Exosome" already known,
# just missing dosage/route). BUGFIX: this dict did not exist before — a
# VISIT clarification question was asked but nothing was remembered, so a
# one-word reply like "5mg" arrived with zero context and always failed.
_pending_visit: dict[str, dict] = {}

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


def _log_extraction_to_visit_extraction(log_extraction) -> VisitExtraction:
    """
    Converts a LogExtraction into a VisitExtraction so a message classified
    as LOG (rather than VISIT) can still go through the same match/create
    pipeline when the patient turns out not to exist yet. LOG and VISIT
    share the same core treatment fields — only VisitExtraction additionally
    supports creating a brand-new patient record.
    """
    return VisitExtraction(
        patient_name=log_extraction.patient_name,
        date=log_extraction.date,
        protocol=log_extraction.protocol,
        dosage=log_extraction.dosage,
        route=log_extraction.route,
        notes=log_extraction.notes,
        next_visit_days=log_extraction.next_visit_days,
        next_visit_date=log_extraction.next_visit_date,
        is_complete=True,
    )


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

    # ── Unsupported message type (voice, image, etc.) — reply, don't go silent ──
    if text.startswith("[unsupported:"):
        msg_type = text.split(":", 1)[1].rstrip("]")
        if msg_type == "audio":
            await whatsapp.send_text(
                sender,
                "🎤 Maaf, voice message belum bisa diproses saat ini.\n"
                "Coba ketik pesannya, ya. (Voice note ada di roadmap Phase 2)"
            )
        else:
            await whatsapp.send_text(
                sender,
                f"📎 Maaf, tipe pesan ini ({msg_type}) belum didukung.\n"
                "Coba ketik pesannya sebagai teks."
            )
        return

    # ── Check pending LOG clarification ──────────────────────────────────────
    if sender in _pending_log:
        await _handle_log_clarification(sender, text)
        return

    # ── Check pending VISIT clarification ────────────────────────────────────
    if sender in _pending_visit:
        await _handle_visit_clarification(sender, text)
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
        if intent.intent == "BILLING_ERROR":
            # Anthropic credit balance exhausted — tell the doctor plainly
            # instead of masking it as "the bot didn't understand".
            await whatsapp.send_text(
                sender,
                "⚠️ *Sistem AI sedang tidak aktif* (credit habis).\n"
                "Pesan ini belum bisa diproses. Hubungi Tommy untuk top-up "
                "credit Anthropic API secepatnya."
            )

        elif intent.intent == "LOOKUP":
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

    await _save_log_extraction(sender, extraction)


async def _save_log_extraction(sender: str, extraction) -> None:
    """
    Shared save step used by BOTH the direct text-based LOG flow and the
    clarification-merge flow. Taking an already-built extraction directly
    (rather than re-parsing text) is the fix for a real bug: re-extracting
    from a reconstructed text string was silently dropping dosage/route
    that had already been merged in from a prior clarification turn.
    """
    if not extraction.patient_name:
        await whatsapp.send_text(sender, "❓ Nama pasien tidak ditemukan di pesan.")
        return

    patients, _ = patient_svc.lookup_patient(extraction.patient_name)

    if not patients:
        # BUGFIX: previously dead-ended here with "patient not found" —
        # but a LOG message and a VISIT message describing the exact same
        # new patient should behave the same way. Whichever intent the
        # classifier happens to pick, an unknown patient should always be
        # createable, not silently rejected depending on phrasing.
        # Convert this LOG extraction into a VisitExtraction and run it
        # through the same match/create pipeline VISIT uses.
        visit_extraction = _log_extraction_to_visit_extraction(extraction)
        await _resolve_and_log_visit(sender, visit_extraction)
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

    BUGFIX: previously only carried forward patient_name/protocol/date when
    reconstructing context — dosage, route, and notes already confirmed in
    earlier turns were silently dropped, causing the clarification loop to
    lose progress and ask the doctor to start over. Now every known field
    is passed forward as context AND used as a merge fallback.
    """
    pending = _pending_log.get(sender, {})

    # Combine ALL previously known fields as context — not just name/protocol/date.
    # This is what was missing before: dosage and route confirmed in an
    # earlier turn must still be visible to the next extraction call.
    known_parts = []
    for field, label in [
        ("patient_name", "Patient"), ("protocol", "Protocol"),
        ("dosage", "Dosage"), ("route", "Route"), ("date", "Date"),
        ("notes", "Notes"), ("next_visit_days", "Next visit in days"),
    ]:
        val = pending.get(field)
        if val:
            known_parts.append(f"{label}: {val}")

    combined_message = (
        f"Original treatment note context — {', '.join(known_parts)}. "
        f"Doctor replied with additional info: {clarification_text}"
    )

    # Re-extract with combined context
    extraction = claude_ai.extract_treatment_log(combined_message)

    # Merge EVERY known field from pending into the new extraction if the
    # new extraction didn't determine it — not just three of them.
    for field in ["patient_name", "protocol", "dosage", "route", "date",
                  "notes", "next_visit_days", "next_visit_date"]:
        if not getattr(extraction, field, None) and pending.get(field):
            setattr(extraction, field, pending.get(field))

    # BUGFIX: extraction.is_complete reflects only what THIS API call saw,
    # not the merged result above. Recompute it now that dosage/route/etc.
    # from earlier turns have been filled back in — otherwise a fully
    # complete record (after merge) still gets treated as incomplete and
    # the doctor is asked to start over, even though nothing is missing.
    extraction.is_complete = bool(
        extraction.patient_name and extraction.protocol and extraction.date
    )

    # Clear pending state
    del _pending_log[sender]

    # Use the MERGED extraction object directly — do NOT re-extract from
    # combined_message text, since that would silently discard the
    # dosage/route we just manually restored from `pending` above.
    if extraction.is_complete:
        await _save_log_extraction(sender, extraction)
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
        # BUGFIX: previously nothing was saved here — a one-word reply like
        # "5mg" to this question arrived with zero memory of the patient
        # name/protocol already extracted, and always failed. Now the
        # partial extraction is preserved so the next message can merge
        # into it instead of starting from scratch.
        _pending_visit[sender] = extraction.model_dump()
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


async def _handle_visit_clarification(sender: str, clarification_text: str) -> None:
    """
    Doctor replied to a VISIT clarification question (e.g. answered "5mg"
    after being asked for dosage). Merges the reply into the ORIGINAL
    partial extraction — patient_name, protocol, and any `extra` context
    already gathered — rather than treating the reply as a brand-new,
    contextless message. This is the fix for the exact bug reported: a
    bare "5mg" or "Injeksi" reply used to fail because nothing was
    remembered about the visit being discussed.
    """
    pending = _pending_visit.pop(sender, {})
    pending_extraction = VisitExtraction(**pending)

    # Build a context summary of everything already known, so the next
    # extraction call can fill in just the missing piece intelligently.
    known_parts = []
    for field, label in [
        ("patient_name", "Patient"), ("protocol", "Protocol"),
        ("dosage", "Dosage"), ("route", "Route"), ("date", "Date"),
        ("notes", "Notes"), ("allergies", "Allergies"),
        ("vip_tier", "VIP tier"), ("medical_notes", "Medical notes"),
    ]:
        val = getattr(pending_extraction, field, None)
        if val:
            known_parts.append(f"{label}: {val}")
    if pending_extraction.extra:
        for k, v in pending_extraction.extra.items():
            if v:
                known_parts.append(f"{k.replace('_', ' ').capitalize()}: {v}")

    combined_message = (
        f"Original visit context — {', '.join(known_parts)}. "
        f"Doctor replied with additional info: {clarification_text}"
    )

    new_extraction = claude_ai.extract_visit(combined_message)

    # Merge every core field the new call didn't determine, falling back
    # to what was already known from the pending extraction.
    for field in ["patient_name", "nickname", "phone", "gender", "dob",
                  "vip_tier", "allergies", "medical_notes",
                  "date", "protocol", "dosage", "route", "notes",
                  "next_visit_days", "next_visit_date"]:
        if not getattr(new_extraction, field, None):
            old_val = getattr(pending_extraction, field, None)
            if old_val:
                setattr(new_extraction, field, old_val)

    # Merge extra dicts (new values win on key collision, old ones fill gaps)
    merged_extra = dict(pending_extraction.extra or {})
    merged_extra.update(new_extraction.extra or {})
    new_extraction.extra = merged_extra

    # Recompute completeness now that merged fields are in place
    new_extraction.is_complete = bool(
        new_extraction.patient_name and new_extraction.protocol
    )

    if not new_extraction.is_complete:
        # Still missing something — ask again, keeping state alive
        _pending_visit[sender] = new_extraction.model_dump()
        question = new_extraction.clarification_question or (
            "❓ Masih ada info yang kurang. Bisa dilengkapi?"
        )
        await whatsapp.send_text(sender, question)
        return

    await _resolve_and_log_visit(sender, new_extraction)


async def _resolve_and_log_visit(sender: str, extraction: VisitExtraction) -> None:
    """
    Shared matching logic used by both the normal VISIT flow and the
    post-switch-confirmation flow: match against existing patients,
    then create/log/ask as appropriate.
    """
    match = patient_svc.match_patient_from_visit(extraction)

    if match.match_tier == "CONFIDENT":
        # If this visit mentions allergies/medical history/VIP tier that
        # the existing record doesn't have yet, fill in the gaps — this
        # is the "append new info to existing patient" behavior requested.
        # Already-set fields are never overwritten by this step.
        enriched = patient_svc.enrich_existing_patient(match.patient, extraction)
        await _log_visit_to_patient(sender, enriched, extraction, is_new=False)

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
