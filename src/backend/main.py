"""Zalo Restaurant Bot — GreenNode AgentBase sample (backend).

Endpoints:
  POST /invocations      — chat (simulator/test), needs the user/session headers
  POST /a2a              — A2A JSON-RPC (message/send), needs the user header
  POST /webhook/zalo     — real webhook from the Zalo Bot Platform (own secret header)
  GET  /webhook/zalo     — configuration check, echoes a challenge when given
  GET  /health           — SDK health
  GET  /ready            — deep readiness (memory + gateway + LLM; Zalo is reported only)
  GET  /                 — serves the frontend simulator
  GET  /api/info         — configuration (full detail only with the API key when one is set)
  GET  /api/memory       — returning-guest profile (memory records per actor)
  GET  /api/history      — conversation events per actor+session
  GET  /api/actors       — guests that already have a profile
  GET  /api/bookings     — one guest's upcoming bookings (calls the MCP tool list_bookings)

When AGENT_API_KEY is set, /invocations, /a2a and /api/* (except the minimal /api/info) require
the X-API-Key header. /webhook/zalo is never behind it: Zalo cannot send one, the webhook has
its own secret.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import uuid
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from greennode_agentbase import (
    GreenNodeAgentBaseApp,
    GreenNodeRequestError,
    RequestContext,
    PingStatus,
)

import agent as agent_mod
import memory_tools
from memory_tools import run_coro
import zalo
from mcp_client import mcp_request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("zalo-restaurant-bot")

app = GreenNodeAgentBaseApp()

MEMORY_ID = os.environ.get("AGENTBASE_MEMORY_ID", "")
MCP_RESTAURANT_URL = os.environ.get("MCP_RESTAURANT_URL", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
# AGENT_API_KEY (optional): protects the REST API in production (the webhook uses its own secret)
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "").strip()
# DEBUG_OPS=1: enables the whoami op (exposes the runtime identity: use it only while setting up policies)
DEBUG_OPS = os.environ.get("DEBUG_OPS", "0").strip() in ("1", "true", "yes")
# Zalo messages processed concurrently (one worker per chat at a time, see zalo.ChatDispatcher)
ZALO_MAX_WORKERS = max(1, int(os.environ.get("ZALO_MAX_WORKERS", "8")))

# A2A (Agent-to-Agent protocol): public URL of this runtime, written into the agent card
A2A_PUBLIC_URL = os.environ.get("A2A_PUBLIC_URL", "").rstrip("/")

USER_HEADER = "X-GreenNode-AgentBase-User-Id"
SESSION_HEADER = "X-GreenNode-AgentBase-Session-Id"
API_KEY_HEADER = "X-API-Key"
MISSING_A2A_USER_MSG = (
    f"Missing header {USER_HEADER} (the memory actor): an A2A caller must go through the "
    "AgentBase Runtime (which attaches the header) or send this header itself."
)
MISSING_HEADERS_MSG = (
    f"Missing required headers: {USER_HEADER} and {SESSION_HEADER} "
    "(they separate memory per user and session). There is no default value, to avoid "
    "mixing data between users."
)
# What a guest reads when a turn fails: generic on purpose, the details go to the log.
GUEST_APOLOGY = "Xin lỗi quý khách, hệ thống đang bận — vui lòng nhắn lại sau ít phút ạ 🙏"

# LangFuse tracing (optional): LANGFUSE_PUBLIC_KEY / SECRET_KEY / HOST


def _lf_tracing() -> bool:
    """LangFuse v4 tracing is on when all 3 env vars are set (the v4 client reads them itself)."""
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
        and os.environ.get("LANGFUSE_HOST")
    )


def _lf_scope(trace_name: str, user_id: str = "", session_id: str = "", tags: list | None = None):
    """LangFuse v4: a `propagate_attributes` scope, so trace_name/user/session/tags apply to the
    root observation AND every child (including the generation that carries the cost).

    Enter the scope BEFORE creating the CallbackHandler and running the agent (same thread/context).
    Tracing off -> nullcontext (the turn runs normally)."""
    if not _lf_tracing():
        return nullcontext()
    try:
        from langfuse import propagate_attributes

        kwargs: dict = {"trace_name": trace_name, "tags": tags or []}
        if user_id:
            kwargs["user_id"] = user_id
        if session_id:
            kwargs["session_id"] = session_id
        return propagate_attributes(**kwargs)
    except Exception as e:
        logger.warning("LangFuse scope disabled: %s", e)
        return nullcontext()


def _lf_callback():
    """LangFuse v4 CallbackHandler (OTel, auth via env). Create it INSIDE the scope so it
    inherits the trace context; None = tracing off."""
    if not _lf_tracing():
        return None
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception as e:
        logger.warning("LangFuse callback disabled: %s", e)
        return None


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
TZ_VN = ZoneInfo("Asia/Ho_Chi_Minh")


def _now() -> str:
    return datetime.now(TZ_VN).isoformat()


# ── API-key auth: /invocations, /a2a and /api/* (the handler of /api/info checks the key itself) ──
def _key_matches(supplied: str) -> bool:
    """Constant-time check of an X-API-Key value. False when no key is configured."""
    return bool(AGENT_API_KEY) and secrets.compare_digest(
        (supplied or "").encode(), AGENT_API_KEY.encode()
    )


class ApiKeyMiddleware:
    """Pure-ASGI middleware. AGENT_API_KEY not set -> open (local dev).
    /webhook/zalo is NOT blocked (it uses Zalo's own X-Bot-Api-Secret-Token)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and AGENT_API_KEY:
            path = scope.get("path", "").rstrip("/")
            protected = path in ("/invocations", "/a2a") or (
                path.startswith("/api/") and path != "/api/info"
            )
            if protected:
                headers = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in scope.get("headers", [])
                }
                if not _key_matches(headers.get(API_KEY_HEADER.lower(), "")):
                    resp = JSONResponse(
                        {"status": "error", "error": f"Unauthorized: missing or wrong {API_KEY_HEADER} header"},
                        status_code=401,
                    )
                    await resp(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def _get_user_id(context) -> str:
    """user_id from the header (SDK 1.0.1 does not expose context.user_id yet: read the request)."""
    uid = getattr(context, "user_id", None)
    if uid:
        return uid
    req = getattr(context, "request", None)
    if req is not None:
        try:
            return req.headers.get(USER_HEADER, "") or ""
        except Exception:
            return ""
    return ""


async def _chat_turn(
    actor_id: str,
    session_id: str,
    message: str,
    trace_name: str = "zalo-chat",
    guest_name: str = "",
) -> dict:
    """One conversation turn through the agent (used by /invocations, the webhook and A2A).

    `guest_name` is the guest's display name when the channel knows it (the Zalo profile name);
    it travels in the run config as `configurable.guest_name` for the agent prompt to use."""
    try:
        # LangFuse v4: the propagate_attributes scope wraps the whole ainvoke (same context)
        with _lf_scope(
            trace_name,
            actor_id,
            session_id,
            ["chat", "a2a"] if trace_name.startswith("a2a") else ["chat"],
        ):
            cb = _lf_callback()
            result = await agent_mod.get_agent().ainvoke(
                {"messages": [{"role": "user", "content": message}]},
                config={
                    "callbacks": [cb] if cb else [],
                    "configurable": {
                        "thread_id": session_id,
                        "actor_id": actor_id,
                        "guest_name": guest_name,
                    },
                },
            )
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}", "timestamp": _now()}

    ai_message = result["messages"][-1]
    memories_used: list[str] = []
    for m in result["messages"]:
        if type(m).__name__ != "ToolMessage":
            continue
        content = str(getattr(m, "content", ""))
        if content.startswith("Đã nhớ: "):
            memories_used.append(content[len("Đã nhớ: "):])
        elif "score:" in content and content.lstrip().startswith("- "):
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("- ") and " (score:" in line:
                    memories_used.append(line[2:].split(" (score:")[0])
    reply = str(ai_message.content or "")
    if reply:
        # Recording the transcript is best effort: a memory outage must not turn a good reply into an error.
        try:
            await memory_tools.add_chat_events(actor_id, session_id, message, reply)
        except Exception:
            logger.exception("could not record chat events (session_id=%s); the reply is still returned", session_id)
    return {
        "status": "success",
        "agent": "zalo-restaurant-bot",
        "response": ai_message.content,
        "memories_used": memories_used,
        "timestamp": _now(),
    }


# ── A2A (Agent-to-Agent protocol): agent card + JSON-RPC /a2a ──


def _a2a_card() -> dict:
    card = {
        "name": "zalo-restaurant-bot",
        "description": (
            "Restaurant assistant on Zalo: menu, table booking, loyalty points and "
            "customer care, with per-guest memory. Replies in Vietnamese by default."
        ),
        "url": f"{A2A_PUBLIC_URL}/a2a" if A2A_PUBLIC_URL else "/a2a",
        "version": "1.0.0",
        "protocolVersion": "0.3.0",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "restaurant-consultation",
                "name": "Restaurant advice and table booking",
                "description": "Menu advice, table booking, opening hours, prices, address and the loyalty programme.",
                "tags": ["restaurant", "booking", "zalo"],
                # Sample guest messages (Vietnamese): "Book a table for 4 on Saturday evening, vegetarian
                # dishes please" and "How many points do I have?"
                "examples": ["Đặt bàn 4 người tối thứ 7, cần món chay", "Mình tích được bao nhiêu điểm rồi?"],
            },
        ],
        "preferredTransport": "JSONRPC",
    }
    if AGENT_API_KEY:
        card["securitySchemes"] = {"apiKey": {"type": "apiKey", "in": "header", "name": API_KEY_HEADER}}
        card["security"] = [{"apiKey": []}]
    return card


async def _agent_card_route(request: Request) -> JSONResponse:
    return JSONResponse(_a2a_card())


def _a2a_text(params: dict) -> str:
    msg = (params or {}).get("message") or {}
    return "".join(
        str(p.get("text", ""))
        for p in msg.get("parts", [])
        if p.get("kind") == "text" or "text" in p
    ).strip()


def _rpc_error(rid, code: int, message: str, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}},
        status_code=status_code,
    )


async def _a2a_route(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error")
    if not isinstance(body, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    method = body.get("method", "")
    rid = body.get("id")
    if method != "message/send":
        return _rpc_error(rid, -32601, f"Method not found: {method}")
    text = _a2a_text(body.get("params"))
    if not text:
        return _rpc_error(rid, -32602, "params.message.parts has no text")
    msg = (body.get("params") or {}).get("message") or {}
    # Memory actor = the real user from the runtime header (never a shared default actor such as
    # "a2a": it would mix memory between callers). Missing header -> 400.
    a2a_user = request.headers.get(USER_HEADER, "").strip()
    if not a2a_user:
        return _rpc_error(rid, -32602, MISSING_A2A_USER_MSG, status_code=400)
    # contextId (A2A) = thread_id: the message's contextId first, then the runtime's Session-Id
    # header; otherwise a new id (a new conversation, never a shared one).
    ctx = (
        msg.get("contextId")
        or request.headers.get(SESSION_HEADER, "").strip()
        or f"a2a-{uuid.uuid4().hex[:12]}"
    )
    result = await asyncio.to_thread(
        run_coro, _chat_turn(a2a_user, ctx, text, trace_name="a2a-zalo-turn")
    )
    if result.get("status") != "success":
        return _rpc_error(rid, -32603, result.get("error", "agent error"))
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": rid,
        "result": {
            "kind": "message",
            "messageId": f"msg-{uuid.uuid4()}",
            "contextId": ctx,
            "role": "agent",
            "parts": [{"kind": "text", "text": str(result.get("response") or "")}],
        },
    })


def _missing_identity(user_id: str, session_id: str) -> bool:
    """True when the user or session id is missing (AgentBase docs: do NOT fall back to a
    default, the memory path must return a clear error when the headers are absent)."""
    return not (user_id or "").strip() or not (session_id or "").strip()


@app.entrypoint
def handler(payload: dict, context: RequestContext) -> dict:
    if payload.get("op") == "whoami":
        if not DEBUG_OPS:
            return {
                "status": "error",
                "error": "whoami is disabled. Set DEBUG_OPS=1 (only while setting up policies) and restart the runtime.",
            }
        return {"status": "success", "agent": "zalo-restaurant-bot", **agent_mod.whoami()}
    user_id = _get_user_id(context)
    if _missing_identity(user_id, context.session_id):
        # The SDK maps GreenNodeRequestError(status_code=400) to HTTP 400 (no default fallback)
        raise GreenNodeRequestError(MISSING_HEADERS_MSG, status_code=400)
    message = payload.get("message") or payload.get("input")
    if not isinstance(message, str) or not message.strip():
        raise GreenNodeRequestError(
            "The request body needs a non-empty text in 'message' (or 'input').", status_code=400
        )
    return run_coro(_chat_turn(user_id, context.session_id, message))


@app.ping
def health_check() -> PingStatus:
    return PingStatus.HEALTHY


# ---------- Zalo webhook ----------
def _process_zalo_message(ev: dict) -> None:
    """Answer one Zalo message (runs on a ZALO_MAX_WORKERS pool thread, in order per chat)."""
    request_id = uuid.uuid4().hex[:8]
    chat_id = ev["chat_id"]
    reply = ""
    result: dict = {}
    try:
        result = run_coro(
            _chat_turn(ev["sender_id"], f"zalo-{chat_id}", ev["text"], guest_name=ev["display_name"])
        )
        reply = str(result.get("response") or "").strip()
        if not reply:
            logger.error("[%s] turn failed for chat %s: %s", request_id, chat_id,
                         result.get("error") or "empty reply")
    except Exception:
        logger.exception("[%s] webhook processing failed (chat_id=%s)", request_id, chat_id)
    # A failed turn never reaches the guest as an error text: it gets a generic apology.
    sent = zalo.send_message(chat_id, reply or GUEST_APOLOGY)
    logger.info(
        "[%s] replied to %s | sent=%s | memories=%d | result=%s",
        request_id, chat_id, bool(sent.get("ok")), len(result.get("memories_used") or []), str(sent)[:150],
    )


_zalo_dispatcher = zalo.ChatDispatcher(_process_zalo_message, max_workers=ZALO_MAX_WORKERS)

if zalo.zalo_configured() and not zalo.webhook_secret_configured():
    logger.error(
        "ZALO_BOT_TOKEN is set but ZALO_WEBHOOK_SECRET is not: /webhook/zalo answers 503 until "
        "the secret is configured (use the same value in setWebhook secret_token)"
    )


async def _webhook_get(request: Request) -> JSONResponse:
    # Echo the challenge if Zalo asks to verify the webhook
    challenge = request.query_params.get("challenge") or request.query_params.get("webhook_challenge")
    if challenge:
        return JSONResponse({"challenge": challenge})
    return JSONResponse({"status": "ok", "zalo_configured": zalo.zalo_configured()})


async def _webhook_post(request: Request) -> JSONResponse:
    if not zalo.zalo_configured():
        return JSONResponse({"status": "disabled", "reason": "ZALO_BOT_TOKEN is not set"}, status_code=503)
    if not zalo.webhook_secret_configured():
        logger.error("webhook refused: ZALO_WEBHOOK_SECRET is not set while ZALO_BOT_TOKEN is")
        return JSONResponse(
            {"status": "error", "error": "ZALO_WEBHOOK_SECRET is not configured"}, status_code=503
        )
    # Verify the secret from the Zalo Bot Platform BEFORE reading or parsing the body.
    if not zalo.webhook_secret_ok(request.headers.get("X-Bot-Api-Secret-Token", "")):
        return JSONResponse({"status": "denied"}, status_code=403)

    # From here on always answer 200: a non-2xx makes Zalo retry the same event.
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("body is not a JSON object")
    except Exception:
        logger.info("webhook ignored: body is not a JSON object")
        return JSONResponse({"status": "ignored", "reason": "invalid json"})

    try:
        ev = zalo.parse_webhook(payload)
        name = zalo.event_name(payload)
    except Exception as e:
        logger.warning("webhook parse error: %s", e)
        return JSONResponse({"status": "ignored", "reason": "parse error"})
    if not ev:
        # Not a text message with content (image/sticker/voice/empty text): nothing to answer.
        logger.info("webhook ignored: event=%r is not a non-empty text message", name)
        return JSONResponse({"message": "Success"})
    if zalo.is_duplicate(ev["message_id"]):
        logger.info("webhook ignored: message %s already received (Zalo retry)", ev["message_id"])
        return JSONResponse({"message": "Success"})

    # ACK 200 to Zalo IMMEDIATELY: the LLM turn (3-10 s) runs on the worker pool, so Zalo does
    # not time out or retry. Messages of one chat are answered in the order they arrived.
    _zalo_dispatcher.submit(ev["chat_id"], ev)
    return JSONResponse({"message": "Success", "accepted": True})


app.add_route("/webhook/zalo", _webhook_get, methods=["GET"])
app.add_route("/webhook/zalo", _webhook_post, methods=["POST"])


# ---------- REST helpers for the simulator ----------
# The memory, MCP and Zalo helpers below block (they wait on the persistent event loop or do
# HTTP), so the async routes run them with asyncio.to_thread and never stall the event loop.
async def _api_info(request: Request) -> JSONResponse:
    info = {"agent": "zalo-restaurant-bot", "auth_required": bool(AGENT_API_KEY)}
    if AGENT_API_KEY and not _key_matches(request.headers.get(API_KEY_HEADER, "")):
        # Without the key only the minimum the UI needs to ask for it: no memory id or MCP URL.
        return JSONResponse(info)
    return JSONResponse(
        {
            **info,
            "memory_id": MEMORY_ID,
            "mcp_url": MCP_RESTAURANT_URL,
            "llm_model": LLM_MODEL,
            "zalo_configured": zalo.zalo_configured(),
            "zalo_bot": await asyncio.to_thread(zalo.bot_name),
        }
    )


def _memory_groups(actor: str) -> list[dict]:
    if not memory_tools.MEMORY_STRATEGY_ID:
        return []
    group = {"strategy_id": memory_tools.MEMORY_STRATEGY_ID, "strategy": "customer-profile"}
    try:
        return [{**group, "records": memory_tools.browse_group_sync(actor)}]
    except Exception as e:
        return [{**group, "error": str(e)[:200], "records": []}]


async def _api_memory(request: Request) -> JSONResponse:
    actor = request.query_params.get("actor", "")
    if not actor:
        return JSONResponse({"error": "missing ?actor=<userId>"}, status_code=400)
    groups = await asyncio.to_thread(_memory_groups, actor)
    return JSONResponse({"actor": actor, "groups": groups})


def _conversation_events(actor: str, session: str) -> list[dict]:
    raw = memory_tools.list_events_sync(actor, session)

    def _f(r, k, d=""):
        v = r.get(k, d) if isinstance(r, dict) else getattr(r, k, d)
        return v if v is not None else d

    # Only conversational events (the binary langgraph checkpoints are filtered out)
    events = []
    for ev in raw:
        payload = _f(ev, "payload", None)
        if payload is None or _f(payload, "type", "") != "conversational":
            continue
        events.append(
            {
                "role": _f(payload, "role", "user") or "user",
                "message": _f(payload, "message", ""),
                "createdAt": str(_f(ev, "event_timestamp") or _f(ev, "eventTimestamp") or _f(ev, "created_at")),
            }
        )
    events.reverse()  # the API returns newest first -> reverse to oldest first
    return events


async def _api_history(request: Request) -> JSONResponse:
    actor = request.query_params.get("actor", "")
    session = request.query_params.get("session", "")
    if not actor or not session:
        return JSONResponse({"error": "missing ?actor= and &session="}, status_code=400)
    try:
        events = await asyncio.to_thread(_conversation_events, actor, session)
        return JSONResponse({"actor": actor, "session": session, "events": events})
    except Exception as e:
        return JSONResponse({"actor": actor, "session": session, "events": [], "error": str(e)[:200]})


async def _api_actors(request: Request) -> JSONResponse:
    try:
        return JSONResponse({"actors": await asyncio.to_thread(memory_tools.list_actors_sync)})
    except Exception as e:
        return JSONResponse({"actors": [], "error": str(e)[:200]})


def _guest_bookings(actor: str) -> dict:
    """Call the MCP tool list_bookings for one guest directly (no LLM)."""
    st, body = mcp_request(
        MCP_RESTAURANT_URL, "tools/call", {"name": "list_bookings", "arguments": {"guest_id": actor}}
    )
    if st != 200 or not isinstance(body, dict):
        return {"bookings": [], "error": str(body)[:200]}
    result = body.get("result") or {}
    texts = [
        item.get("text", "")
        for item in result.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    if result.get("isError") or "error" in body:
        return {"bookings": [], "error": ("\n".join(texts) or str(body.get("error")))[:200]}
    data = result.get("structuredContent") or (json.loads("\n".join(texts)) if texts else {})
    return {"bookings": data.get("bookings", []), "truncated": bool(data.get("truncated"))}


async def _api_bookings(request: Request) -> JSONResponse:
    """The simulator's booking panel: the selected guest's upcoming bookings."""
    actor = request.query_params.get("actor", "")
    if not actor:
        return JSONResponse({"error": "missing ?actor=<userId>"}, status_code=400)
    try:
        return JSONResponse(await asyncio.to_thread(_guest_bookings, actor))
    except Exception as e:
        return JSONResponse({"bookings": [], "error": str(e)[:200]})


# ── /ready: deep health check (memory + gateway + LLM, plus Zalo for information) for ops ──
def _check_memory() -> dict:
    try:
        memory_tools.list_actors_sync()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


def _check_gateway() -> dict:
    try:
        tools = agent_mod.get_mcp_tools()
        return {"ok": bool(tools), "tools": len(tools)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


def _check_zalo() -> dict:
    """Informational: a Zalo problem (no egress, bad token) does not make the agent not ready."""
    if not zalo.zalo_configured():
        return {"configured": False}
    bot = zalo.bot_name()
    return {"configured": True, "ok": bool(bot), "bot": bot}


async def _ready(request: Request) -> JSONResponse:
    memory, gateway, zalo_check = await asyncio.gather(
        asyncio.to_thread(_check_memory),
        asyncio.to_thread(_check_gateway),
        asyncio.to_thread(_check_zalo),
    )
    checks = {
        "memory": memory,
        "gateway": gateway,
        "llm": {"ok": bool(LLM_API_KEY), "model": LLM_MODEL},
        "zalo": zalo_check,
    }
    ok = memory["ok"] and gateway["ok"] and checks["llm"]["ok"]
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503)


app.add_route("/ready", _ready, methods=["GET"])
app.add_route("/api/info", _api_info, methods=["GET"])
app.add_route("/api/memory", _api_memory, methods=["GET"])
app.add_route("/api/history", _api_history, methods=["GET"])
app.add_route("/api/actors", _api_actors, methods=["GET"])
app.add_route("/api/bookings", _api_bookings, methods=["GET"])

# SERVE_UI=false -> do not serve the frontend (Zalo-first mode: guests only use Zalo)
SERVE_UI = os.getenv("SERVE_UI", "true").strip().lower() not in ("false", "0", "no")


async def _root(request: Request) -> JSONResponse:
    if SERVE_UI:
        return JSONResponse({"service": "zalo-restaurant-bot", "ui": "served at /index.html"})
    return JSONResponse(
        {
            "service": "zalo-restaurant-bot",
            "ui": "disabled (SERVE_UI=false): guests use Zalo",
            "zalo_configured": zalo.zalo_configured(),
        }
    )


app.add_route("/", _root, methods=["GET"])
app.add_route("/.well-known/agent-card.json", _agent_card_route, methods=["GET"])
app.add_route("/a2a", _a2a_route, methods=["POST"])
app.add_middleware(ApiKeyMiddleware)
if SERVE_UI:
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="ui")


if __name__ == "__main__":
    app.run(port=8080, host="0.0.0.0")
