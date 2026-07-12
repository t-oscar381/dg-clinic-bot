from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── WhatsApp ──────────────────────────────────────────────────────────────
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_VERIFY_TOKEN: str = "dg_clinic_verify"
    WHATSAPP_APP_SECRET: str = ""
    # Authorized doctor number(s), comma-separated, E.164 digits.
    # One value or several: "628119856889" or "628119856889,628123456789".
    # No hard cap — the env var itself is the access-control surface.
    DOCTOR_WHATSAPP_NUMBER: str = ""

    @property
    def doctor_numbers(self) -> frozenset[str]:
        """Parsed allowlist. Tolerates '+', spaces, and dashes in the env value —
        Meta delivers senders as bare digits, and a pasted '+62 811-...' silently
        failing the equality gate is exactly the config bug this prevents."""
        cleaned = (
            n.strip().lstrip("+").replace(" ", "").replace("-", "")
            for n in self.DOCTOR_WHATSAPP_NUMBER.split(",")
        )
        return frozenset(n for n in cleaned if n)

    # ── Claude ────────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_MODEL: str = "claude-sonnet-5"
    MAX_TOKENS_PER_REQUEST: int = 1024
    # Sonnet 5 runs adaptive thinking by DEFAULT (extra tokens per call). Off keeps
    # cost minimal for this structured tool-dispatch bot; flip on if reasoning
    # quality on tricky messages matters more than token spend.
    ENABLE_THINKING: bool = False

    # ── Supabase ──────────────────────────────────────────────────────────────
    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""

    # ── Groq (voice-note transcription) ──────────────────────────────────────
    GROQ_API_KEY: str = ""

    # ── App ───────────────────────────────────────────────────────────────────
    CLINIC_NAME: str = "DG Clinic"
    DOCTOR_NAME: str = "Dr. Denish"
    DEBUG: bool = False

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
