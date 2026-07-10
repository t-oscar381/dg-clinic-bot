"""
WhatsApp Cloud API Service — DG Clinic
Handles sending messages, marking as read, and webhook signature verification.
Meta Cloud API docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""
import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional

import httpx
from app.config import get_settings

settings = get_settings()

WA_API_VERSION = "v19.0"
WA_BASE_URL    = f"https://graph.facebook.com/{WA_API_VERSION}"


@dataclass
class IncomingMessage:
    """One parsed WhatsApp message. `msg_type` selects which fields are set:
    "text" -> text, "audio" -> media_id, "unsupported" -> unsupported_type."""
    sender: str
    message_id: str
    msg_type: str
    text: Optional[str] = None
    media_id: Optional[str] = None
    unsupported_type: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# SEND MESSAGES
# ══════════════════════════════════════════════════════════════════════════════

async def send_text(to: str, body: str) -> dict:
    """
    Send a plain text WhatsApp message.
    `to` is the recipient phone number in E.164 format without +
    e.g. "628123456789"
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    return await _post(f"{WA_BASE_URL}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages", payload)


async def mark_as_read(message_id: str) -> None:
    """
    Marks a received message as read (shows double blue ticks in WhatsApp).
    Call this immediately on receipt, before processing.
    """
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    await _post(f"{WA_BASE_URL}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages", payload)


async def send_typing(to: str) -> None:
    """
    Sends a 'typing...' indicator so the doctor sees the bot is working.
    Note: WhatsApp Cloud API doesn't have a native typing indicator endpoint;
    we simulate it with a brief delay before the actual message.
    This function is a placeholder for future use.
    """
    pass    # Implement with asyncio.sleep if needed


# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOK PARSING
# ══════════════════════════════════════════════════════════════════════════════

def extract_message(body: dict) -> Optional[IncomingMessage]:
    """
    Parse a WhatsApp webhook body into an IncomingMessage.
    Returns None if the body contains no message at all (e.g. status/delivery
    updates — nothing to reply to).

    Audio messages carry the Graph API media id (msg_type="audio") so the
    caller can run the voice-transcription pipeline. Any other non-text type
    (image, document, video, sticker, ...) comes back as msg_type="unsupported"
    with unsupported_type set, so the handler can reply "not supported yet"
    instead of silently doing nothing.
    """
    try:
        entry   = body["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]

        # Ignore status updates (delivery/read receipts) — nothing to reply to
        if "statuses" in value and "messages" not in value:
            return None

        message = value["messages"][0]
        sender_number = message["from"]
        message_id    = message["id"]
        msg_type      = message.get("type")

        if msg_type == "text":
            return IncomingMessage(
                sender=sender_number,
                message_id=message_id,
                msg_type="text",
                text=message["text"]["body"].strip(),
            )

        if msg_type == "audio":
            media_id = message.get("audio", {}).get("id")
            if media_id:
                return IncomingMessage(
                    sender=sender_number,
                    message_id=message_id,
                    msg_type="audio",
                    media_id=media_id,
                )
            # Audio payload with no media id — treat as unsupported rather
            # than crash the voice pipeline on a missing field.

        return IncomingMessage(
            sender=sender_number,
            message_id=message_id,
            msg_type="unsupported",
            unsupported_type=msg_type,
        )

    except (KeyError, IndexError, TypeError):
        return None


def verify_signature(payload_bytes: bytes, x_hub_signature: str) -> bool:
    """
    Verify that the webhook POST came from Meta (not a spoofed request).
    Meta signs the body with your App Secret using HMAC-SHA256.
    Always verify in production.
    """
    if not settings.WHATSAPP_APP_SECRET:
        # If no secret configured, skip verification (dev mode only)
        return True

    if not x_hub_signature or not x_hub_signature.startswith("sha256="):
        return False

    expected_sig = x_hub_signature[len("sha256="):]
    computed_sig = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed_sig, expected_sig)


# ══════════════════════════════════════════════════════════════════════════════
# PRIVATE
# ══════════════════════════════════════════════════════════════════════════════

async def _post(url: str, payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
        "Content-Type":  "application/json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if settings.DEBUG:
            print(f"[WA] POST {url} → {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json()
