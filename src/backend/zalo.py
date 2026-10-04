"""Zalo Bot Platform integration (bot.zaloplatforms.com).

Official API: https://bot-api.zaloplatforms.com/bot${BOT_TOKEN}/<function>
- sendMessage: POST {chat_id, text, parse_mode}; the text limit is 2,000 characters
- setWebhook:  POST {url, secret_token}; Zalo then calls the webhook with the header X-Bot-Api-Secret-Token
- Webhook body: {"ok": true, "result": {"event_name": "message.text.received",
                 "message": {"from": {"id": "...", "display_name": "..."},
                             "chat": {"id": "...", "chat_type": "PRIVATE"}, "text": "..."}}}

Docs: https://bot.zaloplatforms.com/docs/build-your-bot-with-webhook/

Everything here is per process: the dedupe cache and the per-chat queues live in memory, so a
restart (or a second replica) forgets them. Run a single replica, or accept that a retried
message may be answered twice after a restart.
"""

import logging
import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx

logger = logging.getLogger("zalo-restaurant-bot")

ZALO_BOT_TOKEN = os.getenv("ZALO_BOT_TOKEN", "")
ZALO_WEBHOOK_SECRET = os.getenv("ZALO_WEBHOOK_SECRET", "")
API_BASE = os.getenv("ZALO_API_BASE", "https://bot-api.zaloplatforms.com")

ZALO_TEXT_LIMIT = 2000
DEDUPE_TTL_SECONDS = 600.0
DEDUPE_MAX_ENTRIES = 10_000
ME_TTL_SECONDS = 300.0
ME_RETRY_SECONDS = 30.0


def zalo_configured() -> bool:
    return bool(ZALO_BOT_TOKEN)


def webhook_secret_configured() -> bool:
    return bool(ZALO_WEBHOOK_SECRET)


def _api(fn: str) -> str:
    return f"{API_BASE}/bot{ZALO_BOT_TOKEN}/{fn}"


# ---------------------------------------------------------------- getMe


_me_lock = threading.Lock()
_me_cache: tuple[float, dict] | None = None  # (expiry on the monotonic clock, getMe response)


def _fetch_me() -> dict:
    # Never log the exception text or URL: the URL contains the bot token.
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(_api("getMe"))
        data = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Zalo getMe failed: %s", type(e).__name__)
        return {}
    return data if isinstance(data, dict) and data.get("ok") else {}


def get_me() -> dict:
    """getMe: check the token and read the bot profile. Blocking (up to 5 s).

    Returns {} when no token is set or Zalo cannot be reached. A successful answer is cached for
    5 minutes, a failed one for 30 seconds, so a Zalo outage neither hangs every caller nor
    sticks forever.
    """
    global _me_cache
    if not ZALO_BOT_TOKEN:
        return {}
    with _me_lock:
        now = time.monotonic()
        if _me_cache is not None and now < _me_cache[0]:
            return _me_cache[1]
        data = _fetch_me()
        _me_cache = (now + (ME_TTL_SECONDS if data else ME_RETRY_SECONDS), data)
        return data


def bot_name() -> str:
    result = get_me().get("result")
    result = result if isinstance(result, dict) else {}
    return result.get("display_name") or result.get("account_name") or ""


# ---------------------------------------------------------------- webhook


def webhook_secret_ok(header_value: str) -> bool:
    """Verify the X-Bot-Api-Secret-Token header in constant time. Fails closed without a secret."""
    if not ZALO_WEBHOOK_SECRET:
        return False
    return secrets.compare_digest((header_value or "").encode(), ZALO_WEBHOOK_SECRET.encode())


def _event_body(payload: dict) -> dict:
    # Zalo really sends event_name/message at the TOP level; the docs show them nested in "result".
    if "event_name" in payload:
        return payload
    return payload.get("result") or payload.get("data") or {}


def event_name(payload: dict) -> str:
    body = _event_body(payload)
    return str(body.get("event_name") or body.get("eventName") or "")


def parse_webhook(payload: dict) -> dict | None:
    """Extract message_id, sender_id, chat_id, display_name and text from a Zalo webhook body.

    Returns None for anything that is not a non-empty text message from an identifiable sender
    (image, sticker, voice, empty or whitespace-only text, unsupported events).
    """
    if "message.text" not in event_name(payload):
        return None
    msg = _event_body(payload).get("message") or {}
    text = str(msg.get("text") or "").strip()
    if not text:
        return None
    sender = msg.get("from") or {}
    chat = msg.get("chat") or {}
    sender_id = str(sender.get("id") or chat.get("id") or "")
    chat_id = str(chat.get("id") or sender.get("id") or "")
    if not sender_id or not chat_id:
        return None
    return {
        "message_id": str(msg.get("message_id") or msg.get("messageId") or ""),
        "sender_id": sender_id,
        "chat_id": chat_id,
        "display_name": str(sender.get("display_name") or sender.get("displayName") or ""),
        "text": text,
    }


# ---------------------------------------------------------------- dedupe


class SeenCache:
    """Bounded in-memory set of recently seen keys: entries expire after `ttl` seconds and the
    oldest are dropped beyond `max_size`. Thread-safe; per process only."""

    def __init__(self, ttl: float = DEDUPE_TTL_SECONDS, max_size: int = DEDUPE_MAX_ENTRIES,
                 clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl
        self._max_size = max_size
        self._clock = clock
        self._seen: OrderedDict[str, float] = OrderedDict()  # key -> time first seen, oldest first
        self._lock = threading.Lock()

    def check_and_add(self, key: str) -> bool:
        """Record `key` and return True when it was already seen (and not yet expired)."""
        now = self._clock()
        with self._lock:
            while self._seen and next(iter(self._seen.values())) + self._ttl <= now:
                self._seen.popitem(last=False)
            if key in self._seen:
                return True
            self._seen[key] = now
            while len(self._seen) > self._max_size:
                self._seen.popitem(last=False)
            return False


_seen = SeenCache()


def is_duplicate(message_id: str) -> bool:
    """True when Zalo already delivered this message_id (a retry). Marks it as seen atomically,
    so call it before processing. A message without an id cannot be deduplicated."""
    return bool(message_id) and _seen.check_and_add(message_id)


# ---------------------------------------------------------------- per-chat ordering


class ChatDispatcher:
    """Run `handler(item)` on a bounded thread pool, one chat at a time and in arrival order.

    Items for the same chat are queued and drained by a single worker, so two quick messages
    from one guest are answered in order and none is lost, and a busy chat never ties up more
    than one worker. At most `max_workers` chats are processed concurrently.
    """

    def __init__(self, handler: Callable[[dict], None], max_workers: int = 8):
        self._handler = handler
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="zalo-chat")
        self._queues: dict[str, deque[dict]] = {}
        self._lock = threading.Lock()

    def submit(self, chat_id: str, item: dict) -> None:
        with self._lock:
            queue = self._queues.get(chat_id)
            if queue is not None:  # a worker is already draining this chat: it will pick the item up
                queue.append(item)
                return
            self._queues[chat_id] = deque([item])
        self._pool.submit(self._drain, chat_id)

    def _drain(self, chat_id: str) -> None:
        while True:
            with self._lock:
                queue = self._queues[chat_id]
                if not queue:
                    del self._queues[chat_id]
                    return
                item = queue.popleft()
            try:
                self._handler(item)
            except Exception:
                logger.exception("Zalo message handler failed (chat_id=%s)", chat_id)


# ---------------------------------------------------------------- sending


def split_message(text: str, limit: int = ZALO_TEXT_LIMIT) -> list[str]:
    """Split text into parts of at most `limit` characters, in order.

    Cuts at the last paragraph break, line break, sentence end or space that falls in the second
    half of the window, and only cuts mid-word when there is none. No part is empty.
    """
    text = str(text or "").strip()
    parts: list[str] = []
    while len(text) > limit:
        cut = limit
        # (separator, characters of the separator that stay with the left part)
        for sep, keep in (("\n\n", 0), ("\n", 0), (". ", 1), ("! ", 1), ("? ", 1), (" ", 0)):
            idx = text.rfind(sep, 0, limit)
            if idx > limit // 2:
                cut = idx + keep
                break
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def send_message(chat_id: str, text: str) -> dict:
    """Send text to a chat (parse_mode=markdown), as several messages when it exceeds 2,000 characters.

    The parts are sent in order and sending stops at the first failure.
    Returns {"ok": True, "parts": n} or {"ok": False, "sent": k, "parts": n, "error": ...}.
    """
    parts = split_message(text)
    if not parts:
        return {"ok": False, "sent": 0, "parts": 0, "error": "empty message"}
    with httpx.Client(timeout=20) as c:
        for sent, part in enumerate(parts):
            body = {"chat_id": chat_id, "text": part, "parse_mode": "markdown"}
            try:
                r = c.post(_api("sendMessage"), json=body)
                answer = r.json() if r.status_code == 200 else {"ok": False, "http": r.status_code, "body": r.text[:300]}
            except (httpx.HTTPError, ValueError) as e:
                answer = {"ok": False, "error": type(e).__name__}
            if not answer.get("ok"):
                return {"ok": False, "sent": sent, "parts": len(parts), "error": str(answer)[:300]}
    return {"ok": True, "parts": len(parts)}
