"""
Payment-proof pipeline — DG Clinic WhatsApp Bot

Doctors forward bank-transfer screenshots (bukti transfer) as proof a patient
paid. This module:
1. Stores the image durably in the PRIVATE Supabase Storage bucket
   "payment-proofs" (financial PII — never a public bucket).
2. Reads the screenshot with Claude vision into a short text summary
   (amount, bank, sender, date) so the agent can act on it as text.

The agent — not this module — decides which visit the proof belongs to,
using conversation context, and attaches it via the attach_payment_proof
tool. Same division of labor as voice.py: extract here, decide there.
"""
import base64
from datetime import datetime, timezone

import anthropic
from supabase import create_client

from app.config import get_settings
from app.services import claude_ai

settings = get_settings()

BUCKET = "payment-proofs"

_MIME_TO_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
# Claude vision accepts exactly these image types; WhatsApp sends jpeg/png.
_VISION_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

VISION_PROMPT = """
This is a payment screenshot (bukti transfer) sent by a doctor at an Indonesian
clinic, usually a bank/e-wallet transfer receipt in Indonesian.

Extract ONLY what is visibly readable — never guess:
- amount (the transferred amount, plain digits, e.g. 850000)
- bank_or_wallet (e.g. BCA, Mandiri, GoPay)
- sender_name (who paid)
- transfer_date (as shown)
- reference/status if visible

Reply with 2-3 short plain-text lines summarising these fields. If the image
is not a payment receipt, say exactly what it appears to be instead.
""".strip()


class ProofError(Exception):
    """Storage upload or vision extraction failed."""


def store_proof(image_bytes: bytes, mime_type: str, sender: str) -> str:
    """
    Upload the screenshot to the private bucket. Returns the storage path
    (kept on the visit row as payment_proof_path).
    """
    ext = _MIME_TO_EXT.get(mime_type.split(";")[0].strip(), "jpg")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = f"{sender}/{stamp}.{ext}"

    try:
        db = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
        db.storage.from_(BUCKET).upload(
            path, image_bytes, file_options={"content-type": mime_type}
        )
        return path
    except Exception as e:
        raise ProofError(f"Storage upload failed: {e}") from e


def read_payment_screenshot(image_bytes: bytes, mime_type: str) -> str:
    """
    One small Claude vision call -> short text summary of the receipt.
    Kept OUTSIDE the agent loop so the image tokens are paid once, not
    re-sent on every tool round.
    """
    mime = mime_type.split(";")[0].strip()
    if mime not in _VISION_MIMES:
        raise ProofError(f"Unsupported image type for vision: {mime}")

    try:
        response = claude_ai.client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=300,
            thinking={"type": "disabled"},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": base64.standard_b64encode(image_bytes).decode(),
                        },
                    },
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }],
        )
        claude_ai._log_token_usage(response.usage)
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise ProofError("Empty vision response")
        return text
    except anthropic.APIError as e:
        raise ProofError(f"Vision extraction failed: {e}") from e
