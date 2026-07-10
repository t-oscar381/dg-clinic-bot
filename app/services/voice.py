"""
Voice-note pipeline — DG Clinic WhatsApp Bot (V2, phase 3)

Matches blueprint §5 exactly:
1. Resolve the short-lived media URL from Meta's Graph API using the media id
   already present in the webhook payload.
2. Download the raw audio bytes (same Bearer token — Graph API requires it on
   both the resolve call and the download itself).
3. Transcribe via Groq Whisper (whisper-large-v3-turbo), with no `language`
   parameter so it auto-detects Indonesian/English code-switching.

This module only transcribes — it never decides what to do with the text.
The caller is responsible for the "echo before acting" safety step: send the
transcript back to the doctor for confirmation BEFORE it drives any tool call,
since a silently-wrong transcription writing a wrong dose is the one failure a
clinical tool cannot have.
"""
import httpx

from app.config import get_settings
from app.services.whatsapp import WA_BASE_URL

settings = get_settings()

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"

_MIME_TO_EXT = {"ogg": "ogg", "mp4": "m4a", "mpeg": "mp3", "wav": "wav"}


class TranscriptionError(Exception):
    """Media resolution, download, or transcription failed."""


async def transcribe_voice_note(media_id: str) -> str:
    """
    Full voice pipeline: resolve media URL -> download bytes -> Groq Whisper.
    Returns the transcript. Raises TranscriptionError on any failure so the
    caller can send a clear "couldn't transcribe" reply instead of guessing.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        media_url, mime_type = await _resolve_media_url(client, media_id)
        audio_bytes = await _download_media(client, media_url)
        return await _transcribe(client, audio_bytes, mime_type)


async def _resolve_media_url(client: httpx.AsyncClient, media_id: str) -> tuple[str, str]:
    """Step 2 of §5: Graph API media-id -> short-lived download URL."""
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}
    resp = await client.get(f"{WA_BASE_URL}/{media_id}", headers=headers)
    if resp.status_code != 200:
        raise TranscriptionError(f"Failed to resolve media URL (HTTP {resp.status_code})")

    data = resp.json()
    url = data.get("url")
    if not url:
        raise TranscriptionError("Media resolve response missing 'url'")
    return url, data.get("mime_type", "audio/ogg")


async def _download_media(client: httpx.AsyncClient, media_url: str) -> bytes:
    """Step 3 of §5: download the OGG/Opus bytes (Bearer header again)."""
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}
    resp = await client.get(media_url, headers=headers)
    if resp.status_code != 200:
        raise TranscriptionError(f"Failed to download media (HTTP {resp.status_code})")
    return resp.content


async def _transcribe(client: httpx.AsyncClient, audio_bytes: bytes, mime_type: str) -> str:
    """Step 4 of §5: Groq Whisper, language auto for ID/EN code-switching."""
    if not settings.GROQ_API_KEY:
        raise TranscriptionError("GROQ_API_KEY not configured")

    subtype = mime_type.split("/", 1)[-1].split(";", 1)[0]
    ext = _MIME_TO_EXT.get(subtype, "ogg")

    files = {"file": (f"voice.{ext}", audio_bytes, mime_type)}
    data = {
        "model": GROQ_MODEL,
        "response_format": "text",
        # No `language` — auto-detect handles ID/EN code-switching per §5.
    }
    headers = {"Authorization": f"Bearer {settings.GROQ_API_KEY}"}

    resp = await client.post(GROQ_TRANSCRIBE_URL, headers=headers, data=data, files=files)
    if resp.status_code != 200:
        if settings.DEBUG:
            print(f"[voice] Groq transcription failed: {resp.status_code} {resp.text[:200]}")
        raise TranscriptionError(f"Groq transcription failed (HTTP {resp.status_code})")

    text = resp.text.strip()
    if not text:
        raise TranscriptionError("Empty transcription")
    return text
