"""
Knowledge bank — DG Clinic WhatsApp Bot

Dr. Denish's "second brain": messages from a knowledge-mode number become notes,
kept SEPARATE from the patient CRM. This is a deliberately lean flow — NOT the
6-tool clinical agent:

- capture_note(): store his words verbatim (raw_text), then ONE cheap Haiku call
  produces a topic + a cleaned knowledge statement + a one-line key message.
  The AI never discards his words; it only organizes them.
- search_notes(): keyword search over his own notes ("cari catatan <topic>").

Both are stateless — each note stands alone; the notes ARE the memory, so no
conversation_messages here.
"""
import json

import anthropic
from supabase import create_client, Client

from app.config import get_settings
from app.services import claude_ai

settings = get_settings()

SEARCH_LIMIT = 5

# Leading words that mean "find my notes" rather than "save a new note".
_SEARCH_TRIGGERS = ("cari catatan", "cari ilmu", "cari note", "cari ", "search ", "temukan ")

_TIDY_SYSTEM = """
You organize a doctor's free-form knowledge note (Indonesian/English, may be a
rough voice transcript) WITHOUT changing its meaning or inventing anything.

Return ONLY JSON:
{
  "topic": "2-4 word subject, Title Case",
  "knowledge": "the note restated clearly in 1-3 sentences, same language as the doctor",
  "key_message": "one short line (max ~12 words) capturing the single most useful takeaway"
}

Never add facts the note doesn't contain. If the note is too vague to organize,
set topic to "Catatan" and put the note as-is into knowledge.
""".strip()


class KnowledgeError(Exception):
    pass


def _db() -> Client:
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)


def _tidy(raw_text: str) -> dict:
    """One cheap Haiku call: raw note -> {topic, knowledge, key_message}.
    Falls back to a minimal record if the call/parse fails — a note is never
    lost just because tidying hiccuped."""
    try:
        resp = claude_ai.client.messages.create(
            model=settings.KNOWLEDGE_MODEL,
            max_tokens=400,
            system=_TIDY_SYSTEM,
            messages=[{"role": "user", "content": raw_text}],
        )
        claude_ai._log_token_usage(resp.usage)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return {
            "topic": (data.get("topic") or "Catatan").strip(),
            "knowledge": (data.get("knowledge") or raw_text).strip(),
            "key_message": (data.get("key_message") or "").strip() or None,
        }
    except (anthropic.APIError, json.JSONDecodeError, Exception) as e:
        if settings.DEBUG:
            print(f"[knowledge] tidy failed, storing raw: {e}")
        return {"topic": "Catatan", "knowledge": raw_text, "key_message": None}


def is_search(text: str) -> bool:
    return text.strip().lower().startswith(_SEARCH_TRIGGERS)


def capture_note(sender: str, raw_text: str, source: str = "text") -> str:
    """Store one note and reply with the AI's summary so the doctor sees it was
    understood. Raises KnowledgeError on a storage failure."""
    tidy = _tidy(raw_text)
    row = {
        "author_wa_number": sender,
        "source": source,
        "raw_text": raw_text,
        "topic": tidy["topic"],
        "knowledge": tidy["knowledge"],
        "key_message": tidy["key_message"],
    }
    try:
        _db().table("knowledge_notes").insert(row).execute()
    except Exception as e:
        if settings.DEBUG:
            print(f"[knowledge] insert failed: {e}")
        raise KnowledgeError(str(e)) from e

    lines = [f"📝 *Tersimpan* — {tidy['topic']}"]
    if tidy["key_message"]:
        lines.append(f"_{tidy['key_message']}_")
    lines.append("\n(ketik *cari catatan [topik]* untuk mencari)")
    return "\n".join(lines)


def search_notes(sender: str, query_text: str) -> str:
    """Keyword search over the author's own (non-deleted) notes."""
    q = query_text.strip()
    low = q.lower()
    for trig in _SEARCH_TRIGGERS:
        if low.startswith(trig):
            q = q[len(trig):].strip()
            break
    if not q:
        return "❓ Mau cari catatan tentang apa? Contoh: *cari catatan jet lag*"

    try:
        # Match the keyword across topic/knowledge/raw_text, author-scoped.
        res = (
            _db()
            .table("knowledge_notes")
            .select("topic, knowledge, key_message, created_at")
            .eq("author_wa_number", sender)
            .is_("deleted_at", "null")
            .or_(f"topic.ilike.%{q}%,knowledge.ilike.%{q}%,raw_text.ilike.%{q}%")
            .order("created_at", desc=True)
            .limit(SEARCH_LIMIT)
            .execute()
        )
    except Exception as e:
        if settings.DEBUG:
            print(f"[knowledge] search failed: {e}")
        return "⚠️ Gagal mencari catatan. Coba lagi ya."

    rows = res.data or []
    if not rows:
        return f"🔍 Tidak ada catatan tentang *{q}*."

    lines = [f"🔍 *{len(rows)} catatan tentang \"{q}\":*\n"]
    for r in rows:
        line = f"• *{r.get('topic') or 'Catatan'}*"
        km = r.get("key_message")
        if km:
            line += f" — {km}"
        lines.append(line)
    return "\n".join(lines)
