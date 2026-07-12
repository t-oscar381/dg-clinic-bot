"""
Patient Service — DG Clinic WhatsApp Bot
Database operations (Supabase) + WhatsApp response formatting.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from supabase import create_client, Client
from app.config import get_settings
from app.models.schemas import Patient, TreatmentLog, LogExtraction, VisitExtraction, PatientMatch

settings = get_settings()

# ── Shared rendering maps for the OPEN `extra` dict ─────────────────────────
# Used by BOTH format_new_patient_confirmation() and format_patient_card(),
# so a lookup always shows the same icons/labels as the original visit
# confirmation did — no drift between "just logged" and "looked up later".
# Add new keys here anytime; anything not listed falls back to a bullet "•"
# and a title-cased version of the key, so nothing ever fails to render.
_EXTRA_ICON_MAP = {
    "risk_factors": "⚠️",
    "vitals_checked": "✅",
    "vitals_not_checked_reason": "⏭️",
    "accompanying_people": "👥",
    "location": "📍",
    "travel_distance": "🚗",
    "payment_amount": "💰",
    "payment_notes": "💬",
    "referral_source": "🔗",
}
_EXTRA_LABEL_MAP = {
    "risk_factors": "Risk factors",
    "vitals_checked": "Vitals",
    "vitals_not_checked_reason": "Not checked",
    "accompanying_people": "Accompanying",
    "location": "Location",
    "travel_distance": "Distance",
    "payment_amount": "Payment",
    "payment_notes": "Payment note",
    "referral_source": "Referral",
}


def _get_db() -> Client:
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)


# ══════════════════════════════════════════════════════════════════════════════
# PATIENT LOOKUP
# ══════════════════════════════════════════════════════════════════════════════

def lookup_patient(name_query: str) -> tuple[list[Patient], Optional[dict]]:
    """
    Fuzzy-search patients by name using the Postgres pg_trgm function.
    Returns (list_of_matches, latest_treatment_log_of_best_match).

    The Supabase RPC call maps directly to the SQL function search_patient().
    """
    db = _get_db()

    result = db.rpc(
        "search_patient",
        {"query": name_query, "similarity_threshold": 0.2},
    ).execute()

    if not result.data:
        return [], None

    patients = [Patient(**row) for row in result.data]
    best = patients[0]

    # Fetch the latest treatment log for the best match
    # (excludes soft-deleted rows so an undone visit stops showing as "latest")
    log_result = (
        db.table("treatment_logs")
        .select("*")
        .eq("patient_id", best.id)
        .is_("deleted_at", "null")
        .order("date", desc=True)
        .limit(1)
        .execute()
    )
    latest_log = log_result.data[0] if log_result.data else None

    return patients, latest_log


# ══════════════════════════════════════════════════════════════════════════════
# TREATMENT LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def save_treatment_log(
    patient_id: str,
    extraction: LogExtraction,
    extra: Optional[dict] = None,
    logged_by: str = "doctor",
) -> Optional[TreatmentLog]:
    """
    Insert a new treatment log row and return the saved record.
    `extra` is an OPEN dict (Cekat-style) for anything the doctor mentioned
    that doesn't fit a fixed column — stored as JSONB, no schema change needed.
    `logged_by` records WHICH doctor (wa_number) wrote the entry — with several
    doctors sharing one patient pool, authorship is part of the medical record.
    """
    db = _get_db()

    payload = {
        "patient_id": patient_id,
        "date": extraction.date or date.today().isoformat(),
        "protocol": extraction.protocol,
        "dosage": extraction.dosage,
        "route": extraction.route,
        "notes": extraction.notes,
        "next_visit_date": extraction.next_visit_date,
        "logged_by": logged_by,
    }
    if extra:
        payload["extra"] = extra

    # ── Structured revenue — only write money columns when a charge is present,
    # so a no-charge visit leaves them NULL rather than logging 0/IDR noise. ──
    if any(v is not None for v in (
        extraction.amount_treatment, extraction.amount_homecare, extraction.amount_total
    )):
        payload["amount_treatment"] = extraction.amount_treatment
        payload["amount_homecare"] = extraction.amount_homecare
        payload["amount_total"] = extraction.amount_total
        payload["currency"] = extraction.currency or "IDR"

    result = db.table("treatment_logs").insert(payload).execute()
    if result.data:
        return TreatmentLog(**{k: v for k, v in result.data[0].items() if k != "extra"})
    return None


# ══════════════════════════════════════════════════════════════════════════════
# VISIT WORKFLOW — match existing patient OR create new, then log the visit
# ══════════════════════════════════════════════════════════════════════════════

# Similarity thresholds for the 3-tier match decision.
# Tuned conservatively — clinical data should err toward asking, not assuming.
CONFIDENT_MATCH_THRESHOLD = 0.55   # auto-proceed with this patient
POSSIBLE_MATCH_THRESHOLD  = 0.25   # ask doctor to confirm which one


def match_patient_from_visit(extraction: VisitExtraction) -> PatientMatch:
    """
    Given identity details extracted from a visit narrative, decide whether
    this is:
      - CONFIDENT : a clear existing match — proceed automatically
      - POSSIBLE  : one or more plausible matches — ask doctor to confirm
      - NONE      : no match found — treat as a new patient
    """
    if not extraction.patient_name:
        return PatientMatch(match_tier="NONE")

    db = _get_db()
    result = db.rpc(
        "search_patient",
        {"query": extraction.patient_name, "similarity_threshold": POSSIBLE_MATCH_THRESHOLD},
    ).execute()

    if not result.data:
        return PatientMatch(match_tier="NONE")

    candidates = [Patient(**row) for row in result.data]
    best = candidates[0]

    # Cross-check with phone number if both extraction and record have one —
    # a phone match is strong secondary confirmation even at lower name similarity
    phone_confirms = bool(
        extraction.phone and getattr(best, "phone", None) == extraction.phone
    )

    if best.similarity >= CONFIDENT_MATCH_THRESHOLD or phone_confirms:
        return PatientMatch(match_tier="CONFIDENT", patient=best, candidates=candidates)

    return PatientMatch(match_tier="POSSIBLE", candidates=candidates)


def create_patient_from_visit(extraction: VisitExtraction) -> Optional[Patient]:
    """
    Create a new patient record from visit-narrative identity fields.
    Called when match_patient_from_visit() returns NONE.

    IMPORTANT: allergies, medical_notes, and vip_tier are read from the
    CORE extraction fields (not `extra`) — these have dedicated columns
    because format_patient_card() shows a safety-critical allergy warning
    that only fires if patient.allergies is actually set on this column.
    """
    db = _get_db()

    payload = {
        "full_name": extraction.patient_name,
        "nickname": extraction.nickname,
        "dob": extraction.dob,
        "gender": extraction.gender,
        "phone": extraction.phone,
        "allergies": extraction.allergies,
        "medical_notes": extraction.medical_notes,
        "vip_tier": extraction.vip_tier or "Standard",   # explicit default only if not mentioned
        "referral_source": (extraction.extra or {}).get("referral_source"),
        "is_active": True,
    }
    # Strip None values so Supabase uses column defaults where relevant
    payload = {k: v for k, v in payload.items() if v is not None}

    result = db.table("patients").insert(payload).execute()
    if result.data:
        return Patient(**result.data[0])
    return None


def enrich_existing_patient(patient: Patient, extraction: VisitExtraction) -> Optional[Patient]:
    """
    When a visit narrative mentions NEW allergies, medical notes, or VIP
    tier for a patient who ALREADY EXISTS (confident match), update those
    fields — but only fill in what's currently missing. This never
    overwrites an already-set value, since silently replacing verified
    medical data based on a casual mention is riskier than just leaving
    a gap for the doctor to fill deliberately via a direct correction.
    Returns the updated Patient, or the original if nothing changed.
    """
    updates = {}
    if extraction.allergies and not patient.allergies:
        updates["allergies"] = extraction.allergies
    if extraction.medical_notes and not patient.medical_notes:
        updates["medical_notes"] = extraction.medical_notes
    if extraction.vip_tier and patient.vip_tier == "Standard":
        updates["vip_tier"] = extraction.vip_tier

    if not updates:
        return patient

    db = _get_db()
    result = db.table("patients").update(updates).eq("id", patient.id).execute()
    if result.data:
        return Patient(**result.data[0])
    return patient


def find_duplicate_candidates(similarity_threshold: float = 0.4) -> list[dict]:
    """
    Weekly dedupe check: finds pairs of patients whose names are similar
    enough that they might be the same person entered twice (e.g. once via
    LOG with a typo, once via a new VISIT). Returns pairs for manual review —
    this does NOT auto-merge, since merging medical records must be a
    deliberate doctor decision.

    Call this from a weekly cron/scheduled job and message the doctor
    a summary of any pairs found.
    """
    db = _get_db()
    result = db.rpc(
        "find_duplicate_patients",
        {"similarity_threshold": similarity_threshold},
    ).execute()
    return result.data or []


# ══════════════════════════════════════════════════════════════════════════════
# AGENT TOOL BACKENDS
# Pure database operations used by the agentic tool loop (app/services/agent.py).
# These deliberately DO NOT send WhatsApp messages — the agent composes the reply
# from the structured results these return.
# ══════════════════════════════════════════════════════════════════════════════

def get_patient_with_history(patient_id: str, limit: int = 5) -> tuple[Optional[Patient], list[dict]]:
    """
    Fetch one patient by id plus their most recent (non-deleted) treatment logs.
    Returns (patient, logs) — (None, []) if the patient id is unknown.
    """
    db = _get_db()
    p = db.table("patients").select("*").eq("id", patient_id).limit(1).execute()
    if not p.data:
        return None, []

    logs = (
        db.table("treatment_logs")
        .select("*")
        .eq("patient_id", patient_id)
        .is_("deleted_at", "null")
        .order("date", desc=True)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return Patient(**p.data[0]), (logs.data or [])


def update_patient_fields(patient_id: str, fields: dict) -> Optional[Patient]:
    """
    Update explicit patient columns (allergies, medical_notes, vip_tier, phone,
    nickname, gender, dob, referral_source). None/empty values are dropped so a
    partial update never blanks an existing field.
    """
    allowed = {
        "full_name", "nickname", "phone", "gender", "dob",
        "vip_tier", "allergies", "medical_notes", "referral_source",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v not in (None, "")}
    if not updates:
        return None

    db = _get_db()
    result = db.table("patients").update(updates).eq("id", patient_id).execute()
    return Patient(**result.data[0]) if result.data else None


def soft_delete_last_log(patient_id: str) -> Optional[dict]:
    """
    Soft-delete the most recent non-deleted treatment log for a patient by
    stamping deleted_at. Returns the deleted row (for read-back), or None if the
    patient has no active logs. Soft delete keeps the row for auditability and
    makes an accidental undo recoverable, per the V2 safety guarantees.
    """
    db = _get_db()
    res = (
        db.table("treatment_logs")
        .select("*")
        .eq("patient_id", patient_id)
        .is_("deleted_at", "null")
        .order("date", desc=True)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None

    log = res.data[0]
    db.table("treatment_logs").update(
        {"deleted_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", log["id"]).execute()
    return log


def attach_payment_proof(
    patient_id: str,
    proof_path: str,
    amounts: Optional[dict] = None,
) -> Optional[dict]:
    """
    Attach a stored payment-screenshot path to the patient's most recent
    non-deleted visit. If `amounts` is given (amount_treatment/homecare/total),
    ONLY fills fields that are currently NULL — a screenshot never silently
    overwrites an amount the doctor already logged; mismatches are for the
    doctor to resolve, not the bot.
    Returns a summary of the updated row, or None if the patient has no visits.
    """
    db = _get_db()
    res = (
        db.table("treatment_logs")
        .select("id, date, protocol, amount_treatment, amount_homecare, amount_total")
        .eq("patient_id", patient_id)
        .is_("deleted_at", "null")
        .order("date", desc=True)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None

    log = res.data[0]
    updates: dict = {"payment_proof_path": proof_path}
    filled = []
    for field in ("amount_treatment", "amount_homecare", "amount_total"):
        val = (amounts or {}).get(field)
        if val is not None and log.get(field) is None:
            updates[field] = val
            filled.append(field)
    if filled:
        updates["currency"] = "IDR"

    db.table("treatment_logs").update(updates).eq("id", log["id"]).execute()
    return {
        "log_id": log["id"],
        "date": log["date"],
        "protocol": log["protocol"],
        "proof_path": proof_path,
        "amounts_filled": filled,
        "existing_amount_total": log.get("amount_total"),
    }


def daily_recap(recap_date: Optional[str] = None) -> dict:
    """
    Aggregate a day's activity for the evening recap: how many visits, the
    protocol breakdown, who was seen, and which patients are due for a next
    visit tomorrow. `recap_date` is YYYY-MM-DD; defaults to today.
    """
    day = recap_date or date.today().isoformat()
    db = _get_db()

    logs = (
        db.table("treatment_logs")
        .select("*, patients(full_name, vip_tier)")
        .eq("date", day)
        .is_("deleted_at", "null")
        .execute()
    ).data or []

    protocol_counts: dict[str, int] = {}
    patients_seen: list[str] = []
    revenue_total = 0.0
    revenue_treatment = 0.0
    revenue_homecare = 0.0
    for row in logs:
        proto = row.get("protocol") or "Unspecified"
        protocol_counts[proto] = protocol_counts.get(proto, 0) + 1
        name = (row.get("patients") or {}).get("full_name")
        if name and name not in patients_seen:
            patients_seen.append(name)

        # Revenue: trust amount_total when set, otherwise fall back to the sum of
        # the components (a total-only or split-only visit both aggregate right).
        t = float(row.get("amount_treatment") or 0)
        h = float(row.get("amount_homecare") or 0)
        total = row.get("amount_total")
        total = float(total) if total is not None else (t + h)
        revenue_total += total
        revenue_treatment += t
        revenue_homecare += h

    tomorrow = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    upcoming = (
        db.table("treatment_logs")
        .select("next_visit_date, protocol, patients(full_name)")
        .eq("next_visit_date", tomorrow)
        .is_("deleted_at", "null")
        .execute()
    ).data or []

    return {
        "date": day,
        "total_visits": len(logs),
        "protocol_breakdown": protocol_counts,
        "patients_seen": patients_seen,
        "revenue_total": revenue_total,
        "revenue_treatment": revenue_treatment,
        "revenue_homecare": revenue_homecare,
        "due_tomorrow": [
            {
                "patient": (u.get("patients") or {}).get("full_name"),
                "protocol": u.get("protocol"),
            }
            for u in upcoming
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP MESSAGE FORMATTERS
# ══════════════════════════════════════════════════════════════════════════════

def format_patient_card(patient: Patient, latest_log: Optional[dict]) -> str:
    """
    Builds the WhatsApp patient profile card.
    WhatsApp formatting: *bold*, _italic_, newlines only (no markdown tables).
    """
    lines = []

    # ── Header ──
    age_str = _calculate_age(patient.dob) if patient.dob else "?"
    gender_str = patient.gender or "?"
    vip_emoji = {"Platinum": "💎", "Gold": "🥇", "Silver": "🥈"}.get(patient.vip_tier, "⚪")

    lines.append(f"🟢 *{patient.full_name}*  ·  {gender_str}  ·  {age_str}y")
    lines.append(f"{vip_emoji} {patient.vip_tier} Member")

    # ── Allergy Alert ──
    if patient.allergies:
        lines.append(f"\n⚠️ *ALLERGIES:* {patient.allergies}")

    # ── Medical Notes ──
    if patient.medical_notes:
        lines.append(f"📋 {patient.medical_notes}")

    # ── Latest Treatment ──
    if latest_log:
        lines.append("")
        lines.append("*— Last Treatment —*")
        log_date = _format_date(latest_log.get("date"))
        protocol  = latest_log.get("protocol") or ""
        dosage    = latest_log.get("dosage") or ""
        route     = latest_log.get("route") or ""
        notes     = latest_log.get("notes") or ""

        lines.append(f"📌 {protocol} {dosage} {route}".strip())
        lines.append(f"🗓  {log_date}")
        if notes:
            lines.append(f"💬 {notes}")

        # ── Open extra context from the visit (risk factors, payment, etc.) ──
        # Same rendering logic as format_new_patient_confirmation, so a
        # lookup shows exactly what a visit confirmation showed originally —
        # nothing the doctor mentioned is lost between logging and lookup.
        extra = latest_log.get("extra") or {}
        if extra:
            lines.append("")
            for key, value in extra.items():
                if not value:
                    continue
                icon = _EXTRA_ICON_MAP.get(key, "•")
                label = _EXTRA_LABEL_MAP.get(key, key.replace("_", " ").title())
                if key == "payment_amount":
                    try:
                        value = f"Rp {int(value):,}".replace(",", ".")
                    except (ValueError, TypeError):
                        pass
                lines.append(f"{icon} *{label}:* {value}")

        # ── Next Visit ──
        next_date = latest_log.get("next_visit_date")
        if next_date:
            days_delta = _days_from_today(next_date)
            lines.append("")
            lines.append("*— Next Session —*")
            if days_delta < 0:
                lines.append(f"🔴 {_format_date(next_date)} — *OVERDUE {abs(days_delta)} days*")
            elif days_delta == 0:
                lines.append(f"🔴 *TODAY*")
            elif days_delta <= 3:
                lines.append(f"🟡 {_format_date(next_date)} (in {days_delta} days)")
            else:
                lines.append(f"📅 {_format_date(next_date)} (in {days_delta} days)")
    else:
        lines.append("\n_No treatment records yet._")

    lines.append("\n_Reply: /log [name] to add a treatment_")
    return "\n".join(lines)


def format_multi_match(patients: list[Patient]) -> str:
    """When more than one patient matches the search query."""
    lines = ["🔍 *Multiple patients found:*\n"]
    for i, p in enumerate(patients[:4], 1):
        age = _calculate_age(p.dob) if p.dob else "?"
        lines.append(f"  {i}. {p.full_name}  ({p.gender or '?'}, {age}y)  [{p.vip_tier}]")
    lines.append("\n_Reply with a more specific name to look up._")
    return "\n".join(lines)


def format_not_found(name_query: str) -> str:
    return (
        f"❌ No patient found matching *\"{name_query}\"*\n\n"
        "• Check spelling or try nickname\n"
        "• Type /help for available commands"
    )


def format_log_confirmation(patient: Patient, log: TreatmentLog) -> str:
    """Confirmation message sent back after a treatment is logged."""
    lines = [
        f"✅ *Treatment logged*\n",
        f"*Patient:* {patient.full_name}",
        f"*Date:*    {_format_date(str(log.date))}",
        f"*Protocol:* {log.protocol} {log.dosage or ''} {log.route or ''}".strip(),
    ]
    if log.notes:
        lines.append(f"*Notes:*   {log.notes}")
    if log.next_visit_date:
        days = _days_from_today(str(log.next_visit_date))
        lines.append(f"*Next:*    {_format_date(str(log.next_visit_date))} ({days} days)")
    lines.append("\n_/undo to delete the last entry_")
    return "\n".join(lines)


def format_new_patient_confirmation(patient: Patient, log: Optional[TreatmentLog], extraction=None) -> str:
    """
    Confirmation when a brand-new patient was created from a visit narrative.
    Reads `extraction.extra` — an OPEN dict of whatever the doctor mentioned
    that didn't fit a core field. No fixed list of keys; renders whatever
    is present, in the order Claude returned them.
    """
    lines = [
        "🆕 *New patient created*\n",
        f"*Name:* {patient.full_name}",
    ]
    if patient.nickname:
        lines.append(f"*Nickname:* {patient.nickname}")
    if patient.gender:
        lines.append(f"*Gender:* {patient.gender}")
    if patient.dob:
        lines.append(f"*DOB:* {_format_date(str(patient.dob))}")

    if log:
        lines.append("")
        lines.append("*— Visit Logged —*")
        lines.append(f"📌 {log.protocol} {log.dosage or ''} {log.route or ''}".strip())
        if log.notes:
            lines.append(f"💬 {log.notes}")
        if log.next_visit_date:
            lines.append(f"📅 Next: {_format_date(str(log.next_visit_date))}")

    # ── OPEN extra fields — render whatever the doctor mentioned ─────────────
    extra = getattr(extraction, "extra", None) if extraction else None
    if extra:
        lines.append("")
        for key, value in extra.items():
            if not value:
                continue
            icon = _EXTRA_ICON_MAP.get(key, "•")
            label = _EXTRA_LABEL_MAP.get(key, key.replace("_", " ").title())
            if key == "payment_amount":
                try:
                    value = f"Rp {int(value):,}".replace(",", ".")
                except (ValueError, TypeError):
                    pass
            lines.append(f"{icon} *{label}:* {value}")

    lines.append("\n_Reply to add more details (VIP tier, allergies, etc.)_")
    return "\n".join(lines)


def format_possible_match(candidates: list[Patient], extraction_summary: str) -> str:
    """
    When a visit narrative partially matches existing patients but isn't
    confident enough to proceed automatically. Ask the doctor to confirm.
    """
    lines = [
        "🤔 *Is this an existing patient?*\n",
        f"_{extraction_summary}_\n",
        "Possible matches:",
    ]
    for i, p in enumerate(candidates[:3], 1):
        age = _calculate_age(p.dob) if p.dob else "?"
        lines.append(f"  {i}. {p.full_name} ({p.gender or '?'}, {age}y) [{p.vip_tier}]")

    lines.append(
        "\nReply with the *number* to confirm, or say *\"baru\"/\"new\"* "
        "to create a new patient record."
    )
    return "\n".join(lines)


def format_daily_recap(recap: dict) -> str:
    """
    WhatsApp-formatted daily recap. This is ONLY ever sent in response to the
    doctor explicitly asking for it (a "/recap" command or a natural-language
    request routed through the agent's get_daily_recap tool) — there is no
    scheduled/automatic recap. The formatted reply itself is the confirmation
    of what was compiled: the date, the count, and the breakdown.
    """
    day_str = _format_date(recap.get("date"))
    total = recap.get("total_visits", 0)

    lines = [f"📊 *Rekap Hari Ini — {day_str}*\n", f"Total visit: *{total}*"]

    revenue_total = recap.get("revenue_total") or 0
    if revenue_total:
        lines.append(f"\n💰 *Revenue: {_format_rupiah(revenue_total)}*")
        treatment = recap.get("revenue_treatment") or 0
        homecare = recap.get("revenue_homecare") or 0
        # Only show the split when the components were actually recorded.
        if treatment or homecare:
            lines.append(
                f"   _Treatment {_format_rupiah(treatment)} · "
                f"Homecare {_format_rupiah(homecare)}_"
            )

    breakdown = recap.get("protocol_breakdown") or {}
    if breakdown:
        lines.append("\n*— Protocol —*")
        for protocol, count in sorted(breakdown.items(), key=lambda kv: -kv[1]):
            lines.append(f"• {protocol}: {count}")

    patients = recap.get("patients_seen") or []
    if patients:
        lines.append("\n*— Pasien —*")
        lines.append(", ".join(patients))

    due_tomorrow = recap.get("due_tomorrow") or []
    lines.append("\n*— Besok —*")
    if due_tomorrow:
        for item in due_tomorrow:
            name = item.get("patient") or "?"
            protocol = item.get("protocol") or ""
            lines.append(f"🔴 {name} — {protocol}".rstrip(" —"))
    else:
        lines.append("_Tidak ada jadwal follow-up besok._")

    return "\n".join(lines)


def format_help() -> str:
    return (
        f"👋 *{settings.CLINIC_NAME} Doctor Assistant*\n\n"
        "*Patient Lookup:*\n"
        "  • Sita siapa?\n"
        "  • Show me [name]\n"
        "  • Gimana [name]?\n\n"
        "*Log Treatment:*\n"
        "  • Log [name]: [protocol] [dose] [route] hari ini\n"
        "  • Catat Sita: Reta 10mg SC today, next 4 weeks\n\n"
        "*Other:*\n"
        "  • /help — show this menu\n\n"
        "_Language: Indonesian & English both work_ 🇮🇩🇬🇧"
    )


def format_unknown() -> str:
    return (
        "🤔 Hmm, tidak yakin maksudnya.\n\n"
        "Coba:\n"
        "  • *Lookup:* \"Gimana Sita?\"\n"
        "  • *Log:* \"Log Sita: Reta 10mg SC hari ini\"\n"
        "  • */help* untuk daftar lengkap"
    )


# ══════════════════════════════════════════════════════════════════════════════
# PRIVATE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _calculate_age(dob) -> str:
    if not dob:
        return "?"
    if isinstance(dob, str):
        dob = datetime.fromisoformat(dob).date()
    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return str(age)


def _format_date(date_str: Optional[str]) -> str:
    if not date_str:
        return "?"
    try:
        d = datetime.fromisoformat(str(date_str)).date()
        return d.strftime("%-d %b %Y")                  # e.g. "12 Jun 2026"
    except Exception:
        return str(date_str)


def _days_from_today(date_str: str) -> int:
    try:
        d = datetime.fromisoformat(str(date_str)).date()
        return (d - date.today()).days
    except Exception:
        return 0


def _format_rupiah(amount) -> str:
    """Render a number as Indonesian rupiah, e.g. 850000 -> 'Rp 850.000'."""
    try:
        return f"Rp {int(round(float(amount))):,}".replace(",", ".")
    except (ValueError, TypeError):
        return f"Rp {amount}"
