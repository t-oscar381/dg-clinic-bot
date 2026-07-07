from pydantic import BaseModel
from typing import Optional
from datetime import date


class Patient(BaseModel):
    id: str
    full_name: str
    nickname: Optional[str] = None
    dob: Optional[date] = None
    gender: Optional[str] = None
    vip_tier: str = "Standard"
    allergies: Optional[str] = None
    medical_notes: Optional[str] = None
    similarity: Optional[float] = None


class TreatmentLog(BaseModel):
    id: Optional[str] = None
    patient_id: str
    date: date
    protocol: str
    dosage: Optional[str] = None
    route: Optional[str] = None
    notes: Optional[str] = None
    next_visit_date: Optional[date] = None
    logged_by: str = "doctor"


class IntentResult(BaseModel):
    intent: str          # LOOKUP | LOG | VISIT | HELP | UNKNOWN
    patient_name: Optional[str] = None
    confidence: str = "high"
    reason: str = ""


class LogExtraction(BaseModel):
    patient_name: Optional[str] = None
    date: Optional[str] = None
    protocol: Optional[str] = None
    dosage: Optional[str] = None
    route: Optional[str] = None
    notes: Optional[str] = None
    next_visit_days: Optional[int] = None
    next_visit_date: Optional[str] = None
    is_complete: bool = False
    missing_fields: list[str] = []
    clarification_question: Optional[str] = None


class VisitExtraction(BaseModel):
    """
    Extracted from a doctor's natural narrative about a patient visit.
    Covers BOTH identity info (for matching/creating patient)
    AND visit info (for logging the treatment), in one pass.
    """
    # ── Identity fields — used to match or create the patient ──────────────
    patient_name: Optional[str] = None
    nickname: Optional[str] = None
    phone: Optional[str] = None
    gender: Optional[str] = None
    dob: Optional[str] = None
    location: Optional[str] = None          # where the visit happened (home/hotel/office)
    referral_source: Optional[str] = None   # how the patient found the clinic

    # ── Visit / treatment fields ─────────────────────────────────────────────
    date: Optional[str] = None
    protocol: Optional[str] = None
    dosage: Optional[str] = None
    route: Optional[str] = None
    notes: Optional[str] = None
    next_visit_days: Optional[int] = None
    next_visit_date: Optional[str] = None

    is_complete: bool = False
    missing_fields: list[str] = []
    clarification_question: Optional[str] = None


class PatientMatch(BaseModel):
    """Result of trying to match extracted identity against existing patients."""
    match_tier: str                          # CONFIDENT | POSSIBLE | NONE
    patient: Optional[Patient] = None
    candidates: list[Patient] = []           # for POSSIBLE tier — ask doctor to pick
