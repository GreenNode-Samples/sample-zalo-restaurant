"""REST routes: /ready and /api/info survive Zalo being unreachable, /api/bookings is per guest,
and no route blocks the event loop."""
import asyncio
import time

import httpx
import pytest
from starlette.testclient import TestClient

import zalo


@pytest.fixture()
def client(monkeypatch):
    import main

    monkeypatch.setattr(main, "AGENT_API_KEY", "")
    monkeypatch.setattr(zalo, "_me_cache", None)
    monkeypatch.setattr(main.memory_tools, "list_actors_sync", lambda: [])
    monkeypatch.setattr(main.agent_mod, "get_mcp_tools", lambda: ["get_menu"])
    return TestClient(main.app, raise_server_exceptions=False)


def _zalo_down(monkeypatch):
    real_client = httpx.Client

    def handler(request):
        raise httpx.ConnectError("no egress", request=request)

    monkeypatch.setattr(zalo.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))


def _no_http(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("no HTTP call to Zalo is allowed without a token")

    monkeypatch.setattr(zalo.httpx, "Client", boom)


def test_ready_and_info_without_a_zalo_token_make_no_call(client, monkeypatch):
    monkeypatch.setattr(zalo, "ZALO_BOT_TOKEN", "")
    _no_http(monkeypatch)
    ready = client.get("/ready")
    assert ready.status_code == 200 and ready.json()["checks"]["zalo"] == {"configured": False}
    info = client.get("/api/info")
    assert info.status_code == 200 and info.json()["zalo_bot"] == "" and info.json()["zalo_configured"] is False


def test_zalo_unreachable_never_breaks_ready_or_info(client, monkeypatch):
    """getMe used to raise on a network error: /ready and /api/info answered 500."""
    monkeypatch.setattr(zalo, "ZALO_BOT_TOKEN", "123:fake-token")
    _zalo_down(monkeypatch)
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"  # Zalo is reported, it does not decide readiness
    assert ready.json()["checks"]["zalo"] == {"configured": True, "ok": False, "bot": ""}
    info = client.get("/api/info")
    assert info.status_code == 200
    assert info.json()["zalo_configured"] is True and info.json()["zalo_bot"] == ""


def test_ready_reports_the_bot_name_when_zalo_answers(client, monkeypatch):
    monkeypatch.setattr(zalo, "ZALO_BOT_TOKEN", "123:fake-token")
    real_client = httpx.Client
    handler = lambda request: httpx.Response(200, json={"ok": True, "result": {"display_name": "Quán Ngon Bot"}})  # noqa: E731
    monkeypatch.setattr(zalo.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    zalo_check = client.get("/ready").json()["checks"]["zalo"]
    assert zalo_check == {"configured": True, "ok": True, "bot": "Quán Ngon Bot"}


def test_ready_is_degraded_when_memory_or_gateway_fail(client, monkeypatch):
    import main

    monkeypatch.setattr(zalo, "ZALO_BOT_TOKEN", "")

    def memory_down():
        raise RuntimeError("memory unreachable")

    monkeypatch.setattr(main.memory_tools, "list_actors_sync", memory_down)
    r = client.get("/ready")
    assert r.status_code == 503 and r.json()["status"] == "degraded"
    assert r.json()["checks"]["memory"]["ok"] is False and r.json()["checks"]["gateway"]["ok"] is True
    monkeypatch.setattr(main.memory_tools, "list_actors_sync", lambda: [])
    monkeypatch.setattr(main.agent_mod, "get_mcp_tools", lambda: [])
    assert client.get("/ready").status_code == 503  # no gateway tools


# ----------------------------- /api/bookings -----------------------------


def test_bookings_are_listed_for_one_guest(client, monkeypatch):
    import main

    calls = []
    bookings = [{"id": "bk-1", "customer": "Hung", "date": "2026-10-17", "time": "19:00",
                 "party_size": 4, "table": "T3", "status": "CONFIRMED"}]

    def fake_mcp(url, method, params):
        calls.append((url, method, params))
        return 200, {"result": {"content": [{"type": "text", "text": "{}"}],
                                "structuredContent": {"bookings": bookings, "count": 1, "truncated": False}}}

    monkeypatch.setattr(main, "mcp_request", fake_mcp)
    r = client.get("/api/bookings", params={"actor": "zalo-111"})
    assert r.status_code == 200 and r.json() == {"bookings": bookings, "truncated": False}
    assert calls == [("https://gw.example/restaurant", "tools/call",
                      {"name": "list_bookings", "arguments": {"guest_id": "zalo-111"}})]


def test_bookings_need_an_actor_and_report_tool_errors(client, monkeypatch):
    import main

    assert client.get("/api/bookings").status_code == 400
    monkeypatch.setattr(main, "mcp_request", lambda *a: (200, {"result": {
        "isError": True, "content": [{"type": "text", "text": "Error executing tool list_bookings: boom"}]}}))
    r = client.get("/api/bookings", params={"actor": "g"})
    assert r.status_code == 200 and r.json()["bookings"] == [] and "boom" in r.json()["error"]
    monkeypatch.setattr(main, "mcp_request", lambda *a: (403, "denied"))
    assert client.get("/api/bookings", params={"actor": "g"}).json() == {"bookings": [], "error": "denied"}


def test_bookings_fall_back_to_the_text_content(client, monkeypatch):
    import main

    text = '{"bookings": [{"id": "bk-2"}], "truncated": true}'
    monkeypatch.setattr(main, "mcp_request", lambda *a: (200, {"result": {"content": [{"type": "text", "text": text}]}}))
    assert client.get("/api/bookings", params={"actor": "g"}).json() == {"bookings": [{"id": "bk-2"}], "truncated": True}


# ----------------------------- the event loop is never blocked -----------------------------

BLOCKING = [
    ("/api/memory?actor=a", "memory_tools", "browse_group_sync", lambda *a, **k: []),
    ("/api/history?actor=a&session=s", "memory_tools", "list_events_sync", lambda *a, **k: []),
    ("/api/actors", "memory_tools", "list_actors_sync", lambda *a, **k: []),
    ("/api/bookings?actor=a", None, "mcp_request", lambda *a, **k: (200, {"result": {"structuredContent": {}}})),
    ("/ready", "memory_tools", "list_actors_sync", lambda *a, **k: []),
    ("/api/info", "zalo", "bot_name", lambda *a, **k: ""),
]


@pytest.mark.parametrize("path,owner,name,result", BLOCKING, ids=[b[0].split("?")[0] for b in BLOCKING])
def test_routes_do_not_block_the_event_loop(monkeypatch, path, owner, name, result):
    import main

    monkeypatch.setattr(main, "AGENT_API_KEY", "")
    monkeypatch.setattr(main.memory_tools, "MEMORY_STRATEGY_ID", "ltms-test")
    monkeypatch.setattr(main.agent_mod, "get_mcp_tools", lambda: ["get_menu"])
    monkeypatch.setattr(zalo, "ZALO_BOT_TOKEN", "123:fake-token")

    def slow(*args, **kwargs):
        time.sleep(0.6)
        return result()

    target = getattr(main, owner) if owner else main
    monkeypatch.setattr(target, name, slow)
    if name != "bot_name":  # keep the Zalo lookup of /ready and /api/info fast and offline
        monkeypatch.setattr(zalo, "bot_name", lambda: "")

    async def scenario():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            started = time.monotonic()
            slow_call = asyncio.create_task(c.get(path))
            await asyncio.sleep(0.05)
            await c.get("/")  # a trivial request must not wait for the slow one
            quick = time.monotonic() - started
            response = await slow_call
            return quick, response.status_code

    quick, status = asyncio.run(scenario())
    assert status in (200, 503)
    assert quick < 0.4, f"the event loop was blocked for {quick:.2f}s by {path}"
