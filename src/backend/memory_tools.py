"""AgentBase Memory helpers: guest memory tools, listings for the UI, and the agent loop.

The memory actor id ALWAYS comes from the LangGraph run config (the Zalo sender id, or the
X-GreenNode-AgentBase-User-Id header for the simulator) - the model never chooses whose memory
it reads or writes. The strategy id is deployment-level configuration (env), not a tool parameter.

The SDK caches its async HTTP client per event loop, so every SDK / agent call runs on one
persistent background loop (see `arun_coro` and `stream_on_loop`); using several loops
fails with "Event loop is closed".
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from typing import Any

import httpx
from greennode_agentbase.exceptions import GreenNodeRequestError
from greennode_agentbase.memory import MemoryClient
from greennode_agentbase.memory.models import (
    EventCreateRequest,
    EventPayload,
    MemoryRecordInsertDirectlyRequest,
    MemoryRecordSearchRequest,
)
from langchain_core.tools import ToolException, tool
from langgraph.config import get_config

logger = logging.getLogger("memory-tools")

MEMORY_ID = os.environ.get("AGENTBASE_MEMORY_ID", "")
# The CUSTOM strategy "customer-profile": `remember` writes to it and `recall` searches it.
MEMORY_STRATEGY_ID = os.environ.get("MEMORY_STRATEGY_ID", "")

# Top-k per strategy and minimum similarity score for `recall`. The score scale is defined
# by the Memory service (higher = more similar); tune the threshold against your own data.
RECALL_LIMIT = 5
RECALL_MIN_SCORE = 0.3
MAX_FACT_CHARS = 500

# Paging for the UI listings: the Memory API paginates every listing.
_PAGE_SIZE = 100
_RETRY_BASE_DELAY = 0.6
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}

_client: MemoryClient | None = None


def memory_client() -> MemoryClient:
    global _client
    if _client is None:
        # MemoryClient reads GREENNODE_CLIENT_ID / GREENNODE_CLIENT_SECRET from the env.
        _client = MemoryClient()
    return _client


def field(obj: Any, key: str, default: Any = "") -> Any:
    """Read a field from an SDK entity or a plain dict, mapping None to `default`."""
    value = obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
    return default if value is None else value


# --- persistent event loop ---------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def agent_loop() -> asyncio.AbstractEventLoop:
    """The persistent background loop that runs every agent and memory SDK call."""
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True, name="agent-loop").start()
        return _loop


async def arun_coro(coro: Coroutine, timeout: float = 600):
    """Run `coro` on the agent loop and await its result without blocking the caller's loop.

    Cancelling the caller (or hitting `timeout`) cancels the coroutine on the agent loop.
    """
    future = asyncio.run_coroutine_threadsafe(coro, agent_loop())
    return await asyncio.wait_for(asyncio.wrap_future(future), timeout)


_ITEM, _ERROR, _END = "item", "error", "end"


async def stream_on_loop(produce: Callable[[Callable[[Any], None]], Awaitable[None]]) -> AsyncIterator:
    """Run `produce(emit)` on the agent loop and yield every emitted item on the caller's loop.

    `emit` is thread-safe. An exception raised by `produce` is re-raised here. When the
    consumer stops early (e.g. the HTTP client disconnected) the producer is cancelled, so an
    abandoned request stops spending LLM tokens.
    """
    here = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def emit(item: Any) -> None:
        here.call_soon_threadsafe(queue.put_nowait, (_ITEM, item))

    async def run() -> None:
        try:
            await produce(emit)
        except Exception as e:
            here.call_soon_threadsafe(queue.put_nowait, (_ERROR, e))
        else:
            here.call_soon_threadsafe(queue.put_nowait, (_END, None))

    future = asyncio.run_coroutine_threadsafe(run(), agent_loop())
    try:
        while True:
            kind, value = await queue.get()
            if kind == _END:
                return
            if kind == _ERROR:
                raise value
            yield value
    finally:
        future.cancel()


# --- retry policy --------------------------------------------------------------------------

def _causes(exc: BaseException) -> list[BaseException]:
    """`exc` followed by its chain of causes (the SDK wraps httpx errors in GreenNodeRequestError)."""
    chain: list[BaseException] = []
    while exc is not None and exc not in chain and len(chain) < 5:
        chain.append(exc)
        exc = exc.__cause__ or getattr(exc, "cause", None)
    return chain


def _is_connect_failure(exc: Exception) -> bool:
    """The request never reached the server, so repeating it is always safe."""
    return any(isinstance(c, (httpx.ConnectError, httpx.ConnectTimeout)) for c in _causes(exc))


def _is_transient(exc: Exception) -> bool:
    """Network error, timeout, HTTP 429 or 5xx: worth repeating an idempotent request."""
    return any(
        isinstance(c, httpx.TransportError)
        or (isinstance(c, GreenNodeRequestError) and c.status_code in _TRANSIENT_STATUS)
        for c in _causes(exc)
    )


async def with_retry(
    factory: Callable[[], Awaitable], *, idempotent: bool, attempts: int = 3
):
    """Await `factory()`, retrying transient failures up to `attempts` times in total.

    Idempotent calls (searches) retry on any transient error. Non-idempotent calls (inserts)
    retry only on connection failures, where the request provably was not sent; retrying
    after a timeout or a 5xx could store the same record twice.
    """
    delay = _RETRY_BASE_DELAY
    for attempt in range(1, attempts):
        try:
            return await factory()
        except Exception as e:
            if not (_is_transient(e) if idempotent else _is_connect_failure(e)):
                raise
            logger.warning("memory call failed (%s) - retry %d/%d", e, attempt, attempts - 1)
            await asyncio.sleep(delay)
            delay *= 2
    return await factory()  # final attempt: errors go to the caller


# --- long-term memory tools ----------------------------------------------------------------

def get_actor_id() -> str:
    """actor_id from the LangGraph run config; refuses to run without one."""
    actor = (get_config().get("configurable") or {}).get("actor_id", "")
    if not actor:
        raise RuntimeError("actor_id is missing from the run config")
    return actor


def build_namespace(actor_id: str, strategy_id: str = "") -> str:
    """Default namespace template: /strategies/{memoryStrategyId}/actors/{actorId}."""
    return f"/strategies/{strategy_id or MEMORY_STRATEGY_ID}/actors/{actor_id}"


def _recall_strategy_ids() -> list[str]:
    """Every configured strategy `recall` searches (one here; add more by listing them)."""
    return [sid for sid in (MEMORY_STRATEGY_ID,) if sid]


@tool(response_format="content_and_artifact")
async def remember(fact: str) -> tuple[str, list[str]]:
    """Save one stable fact about the guest to the guest's profile (long-term memory).

    Use it for lasting facts (allergies, diet, favourite dishes, usual table, birthday), not for
    one-off requests or small talk.

    Args:
        fact: The fact to remember, written as one complete sentence.
    """
    fact = fact.strip()
    if not fact:
        raise ToolException("fact must not be empty")
    if len(fact) > MAX_FACT_CHARS:
        raise ToolException(f"fact is too long (max {MAX_FACT_CHARS} characters); shorten it")
    actor = get_actor_id()

    async def insert():
        return await memory_client().insert_memory_records_directly_async(
            id=MEMORY_ID,
            namespace=build_namespace(actor),
            request=MemoryRecordInsertDirectlyRequest(memoryRecords=[fact]),
        )

    await with_retry(insert, idempotent=False)
    return f"Saved to long-term memory: {fact}", [fact]


@tool(response_format="content_and_artifact")
async def recall(query: str) -> tuple[str, list[str]]:
    """Search the guest's profile (long-term memory) for what is known about them.

    Args:
        query: A natural-language query, e.g. 'allergies and favourite dishes'.
    """
    actor = get_actor_id()
    strategy_ids = _recall_strategy_ids()

    async def search(strategy_id: str):
        request = MemoryRecordSearchRequest(
            query=query, limit=RECALL_LIMIT, score_threshold=RECALL_MIN_SCORE
        )
        return await with_retry(
            lambda: memory_client().search_memory_records_async(
                id=MEMORY_ID, namespace=build_namespace(actor, strategy_id), request=request
            ),
            idempotent=True,
        )

    outcomes = await asyncio.gather(*(search(sid) for sid in strategy_ids), return_exceptions=True)
    hits: dict[str, float] = {}
    errors: list[BaseException] = []
    for sid, outcome in zip(strategy_ids, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning("recall failed for strategy %s: %s", sid, outcome)
            errors.append(outcome)
            continue
        for record in outcome or []:
            text = str(field(record, "memory")).strip()
            if text:
                hits[text] = max(hits.get(text, 0.0), float(field(record, "score", 0) or 0))
    if errors and len(errors) == len(strategy_ids):
        raise errors[0]  # every strategy failed: let the tool-error middleware report it

    if not hits:
        return "No relevant memories found.", []
    ranked = sorted(hits.items(), key=lambda kv: kv[1], reverse=True)
    lines = "\n".join(f"- {text} (score: {score:.2f})" for text, score in ranked)
    return lines, [text for text, _ in ranked]


# --- listings for the UI -------------------------------------------------------------------

async def _paged(
    what: str, call: Callable[..., Awaitable], *, max_pages: int, **kwargs: Any
) -> list:
    """Collect the items of a paginated SDK listing (`what` is for logs), up to `max_pages` pages."""
    items: list = []
    for page in range(1, max_pages + 1):
        response = await call(page=page, size=_PAGE_SIZE, **kwargs)
        items.extend(response.list_data or [])
        if page >= (response.total_page or 1):
            break
    else:
        logger.warning("%s listing truncated after %d pages", what, max_pages)
    return items


async def ping() -> None:
    """Cheap round trip to the Memory service (readiness probe)."""
    await memory_client().list_actors_async(id=MEMORY_ID, page=1, size=1)


async def browse_group(actor_id: str, strategy_id: str, limit: int = 100) -> list[dict]:
    """Memory records of one strategy namespace (memory panel)."""
    records = await memory_client().list_memory_records_async(
        id=MEMORY_ID, namespace=build_namespace(actor_id, strategy_id), limit=limit
    )
    return [
        {
            "id": field(r, "id"),
            "memory": field(r, "memory"),
            "createdAt": str(field(r, "created_at") or field(r, "createdAt")),
        }
        for r in list(records)[:limit]
    ]


async def list_conversation(actor_id: str, session_id: str, limit: int = 50) -> list[dict]:
    """The last `limit` conversational messages of a session, oldest first.

    A session also holds the agent's binary checkpoint blobs (several per turn), so the
    events are paged through completely and filtered instead of trusting a single page.
    """
    events = await _paged(
        "events", memory_client().list_events_async, max_pages=30,
        id=MEMORY_ID, actorId=actor_id, sessionId=session_id,
    )
    messages = []
    for event in reversed(events):  # the API lists newest first; ties then keep chronological order
        payload = field(event, "payload", None)
        if payload is None or field(payload, "type") != "conversational":
            continue
        messages.append(
            {
                "role": field(payload, "role", "user") or "user",
                "message": field(payload, "message"),
                "createdAt": str(field(event, "event_timestamp")),
            }
        )
    messages.sort(key=lambda m: m["createdAt"])  # stable: ISO timestamps sort chronologically
    return messages[-limit:]


async def list_actors() -> list[dict]:
    """Known actors with their session ids (user / session switcher)."""
    client = memory_client()
    actors = await _paged("actors", client.list_actors_async, max_pages=2, id=MEMORY_ID)
    limiter = asyncio.Semaphore(8)

    async def sessions_of(actor_id: str) -> list[str]:
        async with limiter:
            try:
                sessions = await _paged(
                    "sessions", client.list_sessions_async, max_pages=2, id=MEMORY_ID, actorId=actor_id
                )
            except Exception:
                logger.warning("could not list sessions of actor %s", actor_id, exc_info=True)
                return []
        return sorted({str(field(s, "session_id")) for s in sessions if field(s, "session_id")})

    actor_ids = [str(field(a, "actor_id")) for a in actors if field(a, "actor_id")]
    session_lists = await asyncio.gather(*(sessions_of(a) for a in actor_ids))
    return [{"actorId": a, "sessions": s} for a, s in zip(actor_ids, session_lists, strict=True)]


async def add_chat_events(actor_id: str, session_id: str, user_text: str, bot_text: str) -> None:
    """Store the user and assistant messages as conversational events (history panel).

    Best effort: the history is a convenience, so a failure is logged, never raised.
    """
    client = memory_client()
    try:
        for role, message in (("user", user_text), ("assistant", bot_text)):
            await client.create_event_async(
                id=MEMORY_ID,
                actorId=actor_id,
                sessionId=session_id,
                request=EventCreateRequest(
                    payload=EventPayload(type="conversational", role=role, message=message)
                ),
            )
    except Exception:
        logger.warning("could not save conversation events (session=%s)", session_id, exc_info=True)
