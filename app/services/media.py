"""
Meta Graph API media fetch — DG Clinic WhatsApp Bot

Generic two-step download used by every media type the bot handles
(voice notes for transcription, images for payment proofs):
1. Resolve the short-lived media URL from the media id in the webhook payload.
2. Download the raw bytes — the Bearer token is required on BOTH calls.
"""
import httpx

from app.config import get_settings
from app.services.whatsapp import WA_BASE_URL

settings = get_settings()


class MediaError(Exception):
    """Media resolution or download failed."""


async def fetch_media(media_id: str, timeout: float = 30.0) -> tuple[bytes, str]:
    """Return (raw_bytes, mime_type) for a Graph API media id."""
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{WA_BASE_URL}/{media_id}", headers=headers)
        if resp.status_code != 200:
            raise MediaError(f"Failed to resolve media URL (HTTP {resp.status_code})")
        data = resp.json()
        url = data.get("url")
        if not url:
            raise MediaError("Media resolve response missing 'url'")
        mime_type = data.get("mime_type", "application/octet-stream")

        dl = await client.get(url, headers=headers)
        if dl.status_code != 200:
            raise MediaError(f"Failed to download media (HTTP {dl.status_code})")
        return dl.content, mime_type
