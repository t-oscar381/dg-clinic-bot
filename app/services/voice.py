"""
Voice-note pipeline — DG Clinic WhatsApp Bot (V2, phase 3)

Matches blueprint §5: Graph API media fetch (shared app/services/media.py) →
Groq Whisper (whisper-large-v3-turbo) with no `language` parameter so it
auto-detects Indonesian/English code-switching.

This module only transcribes — it never decides what to do with the text.
The caller is responsible for the "echo before acting" safety step: send the
transcript back to the doctor for confirmation BEFORE it drives any tool call,
since a silently-wrong transcription writing a wrong dose is the one failure a
clinical tool cannot have.
"""
import httpx

from app.config import get_settings
from app.services import media

settings = get_settings()

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"

_MIME_TO_EXT = {"ogg": "ogg", "mp4": "m4a", "mpeg": "mp3", "wav": "wav"}


class TranscriptionError(Exception):
    """Media resolution, download, or transcription failed."""


async def transcribe_voice_note(media_id: str) -> str:
    """
    Full voice pipeline: fetch audio bytes from Graph API -> Groq Whisper.
    Returns the transcript. Raises TranscriptionError on any failure so the
    caller can send a clear "couldn't transcribe" reply instead of guessing.
    """
    try:
        audio_bytes, mime_type = await media.fetch_media(media_id)
    except media.MediaError as e:
        raise TranscriptionError(str(e)) from e
    return await _transcribe(audio_bytes, mime_type)


async def _transcribe(audio_bytes: bytes, mime_type: str) -> str:
    """Groq Whisper, language auto for ID/EN code-switching (§5)."""
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

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(GROQ_TRANSCRIBE_URL, headers=headers, data=data, files=files)
    if resp.status_code != 200:
        if settings.DEBUG:
            print(f"[voice] Groq transcription failed: {resp.status_code} {resp.text[:200]}")
        raise TranscriptionError(f"Groq transcription failed (HTTP {resp.status_code})")

    text = resp.text.strip()
    if not text:
        raise TranscriptionError("Empty transcription")
    return text
