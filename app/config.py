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
    # Number(s) in "knowledge mode" — their messages become knowledge-bank notes
    # (Dr. Denish's second brain) instead of patient-CRM records. Same format.
    KNOWLEDGE_MODE_NUMBERS: str = ""

    @staticmethod
    def _parse_numbers(raw: str) -> frozenset[str]:
        """Tolerant allowlist parse. Meta delivers senders as bare digits, so a
        pasted '+62 811-...' silently failing an equality gate is exactly the
        config bug this strips (leading '+', spaces, dashes)."""
        cleaned = (
            n.strip().lstrip("+").replace(" ", "").replace("-", "")
            for n in raw.split(",")
        )
        return frozenset(n for n in cleaned if n)

    @property
    def doctor_numbers(self) -> frozenset[str]:
        return self._parse_numbers(self.DOCTOR_WHATSAPP_NUMBER)

    @property
    def knowledge_numbers(self) -> frozenset[str]:
        return self._parse_numbers(self.KNOWLEDGE_MODE_NUMBERS)

    @property
    def allowed_numbers(self) -> frozenset[str]:
        """Everyone the bot serves — doctors (patient CRM) plus knowledge-mode
        users. The security gate checks membership here; routing then splits by
        whether the sender is a knowledge number."""
        return self.doctor_numbers | self.knowledge_numbers

    # ── Claude ────────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_MODEL: str = "claude-sonnet-5"
    MAX_TOKENS_PER_REQUEST: int = 1024
    # Sonnet 5 runs adaptive thinking by DEFAULT (extra tokens per call). Off keeps
    # cost minimal for this structured tool-dispatch bot; flip on if reasoning
    # quality on tricky messages matters more than token spend.
    ENABLE_THINKING: bool = False
    # Knowledge-bank note tidying (topic/summary) is a light, constrained task —
    # the cheaper model handles it well at ~3x lower cost than the CRM model.
    KNOWLEDGE_MODEL: str = "claude-haiku-4-5"

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
