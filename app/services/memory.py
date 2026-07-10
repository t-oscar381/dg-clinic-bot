"""
Conversation memory — DG Clinic WhatsApp Bot (V2, phase 2)

Postgres-backed chat transcript keyed by the doctor's WhatsApp number, replacing
the in-memory dict the agent loop used before. This is the fix for the V1/V2
"Railway redeploy forgets mid-conversation" pain: memory now lives in Supabase,
not process RAM.

Token-minimal by design: only the last few text turns within a short TTL window
are loaded back into the model context — not the full history — and tool_use /
tool_result blocks are never stored, so a stored turn is a couple of short
strings, not a transcript of every tool round.

Both operations fail OPEN: if Supabase is unreachable, load returns no history
and append silently drops, so the bot still answers the current message.
"""
from datetime import datetime, timedelta, timezone

from supabase import create_client, Client
from app.config import get_settings

settings = get_settings()

# How much prior context to feed back to the model. Small on purpose — each
# message is resent on every request, so this is a direct token cost.
MAX_HISTORY_MESSAGES = 10          # ≈ last 5 user/assistant turns
HISTORY_TTL_MINUTES  = 30          # ignore context older than this


def _db() -> Client:
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)


def load_history(wa_number: str) -> list[dict]:
    """
    Return the recent conversation as [{role, content}] in chronological order,
    bounded by MAX_HISTORY_MESSAGES and HISTORY_TTL_MINUTES. Empty list on any
    failure — the current message is still answerable without prior context.
    """
    try:
        since = (
            datetime.now(timezone.utc) - timedelta(minutes=HISTORY_TTL_MINUTES)
        ).isoformat()
        res = (
            _db()
            .table("conversation_messages")
            .select("role, content")
            .eq("wa_number", wa_number)
            .gte("created_at", since)
            .order("created_at", desc=True)
            .limit(MAX_HISTORY_MESSAGES)
            .execute()
        )
        rows = list(reversed(res.data or []))     # newest-first → chronological
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        if settings.DEBUG:
            print(f"[memory] load_history failed: {e}")
        return []


def append_turn(wa_number: str, user_text: str, assistant_text: str) -> None:
    """Persist one user→assistant exchange. Non-blocking — never breaks a reply."""
    try:
        _db().table("conversation_messages").insert(
            [
                {"wa_number": wa_number, "role": "user", "content": user_text},
                {"wa_number": wa_number, "role": "assistant", "content": assistant_text},
            ]
        ).execute()
    except Exception as e:
        if settings.DEBUG:
            print(f"[memory] append_turn failed: {e}")
