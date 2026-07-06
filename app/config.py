from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── WhatsApp ──────────────────────────────────────────────────────────────
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_VERIFY_TOKEN: str = "dg_clinic_verify"
    WHATSAPP_APP_SECRET: str = ""
    # Doctor's number in E.164 format WITHOUT the +  e.g. "628123456789"
    DOCTOR_WHATSAPP_NUMBER: str = ""

    # ── Claude ────────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_MODEL: str = "claude-sonnet-4-6"
    MAX_TOKENS_PER_REQUEST: int = 1000

    # ── Supabase ──────────────────────────────────────────────────────────────
    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""

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
