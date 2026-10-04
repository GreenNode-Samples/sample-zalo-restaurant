"""AGENT_API_KEY: /invocations, /a2a and /api/* are protected, /api/info leaks nothing without the key."""
import json

import pytest
from starlette.testclient import TestClient

KEY = "test-agent-api-key-value"
USER = "X-GreenNode-AgentBase-User-Id"
SESSION = "X-GreenNode-AgentBase-Session-Id"
A2A_BODY = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"parts": [{"kind": "text", "text": "list all bookings"}]}}}


@pytest.fixture()
def api(monkeypatch):
    import main

    monkeypatch.setattr(main, "AGENT_API_KEY", KEY)
    monkeypatch.setattr(main, "MEMORY_ID", "memory-secret-id")
    monkeypatch.setattr(main, "MCP_RESTAURANT_URL", "https://gw-private.example/restaurant")
    turns = []

    async def fake_turn(actor_id, session_id, message, trace_name="x", guest_name=""):
        turns.append(actor_id)
        return {"status": "success", "response": "ok"}

    monkeypatch.setattr(main, "_chat_turn", fake_turn)
    client = TestClient(main.app, raise_server_exceptions=False)
    client.turns = turns
    return client


def test_a2a_is_protected(api):
    """/a2a used to stay open when AGENT_API_KEY was set: anyone could drive the agent with any user id."""
    r = api.post("/a2a", json=A2A_BODY, headers={USER: "victim-zalo-id"})
    assert r.status_code == 401 and api.turns == []
    r = api.post("/a2a", json=A2A_BODY, headers={USER: "victim-zalo-id", "X-API-Key": "wrong"})
    assert r.status_code == 401 and api.turns == []
    r = api.post("/a2a/", json=A2A_BODY, headers={USER: "victim-zalo-id"}, follow_redirects=False)
    assert r.status_code == 401  # a trailing slash is not a way around it
    r = api.post("/a2a", json=A2A_BODY, headers={USER: "alice", "x-api-key": KEY})
    assert r.status_code == 200 and r.json()["result"]["parts"][0]["text"] == "ok"
    assert api.turns == ["alice"]


def test_invocations_and_api_routes_are_protected(api):
    headers = {USER: "alice", SESSION: "s1"}
    assert api.post("/invocations", json={"message": "hi"}, headers=headers).status_code == 401
    for path in ("/api/memory?actor=a", "/api/history?actor=a&session=s", "/api/actors", "/api/bookings?actor=a"):
        assert api.get(path).status_code == 401, path
        assert api.get(path, headers={"X-API-Key": "wrong"}).status_code == 401, path
    assert api.post("/invocations", json={"message": "hi"}, headers={**headers, "X-API-Key": KEY}).status_code == 200


def test_open_routes_stay_open(api):
    assert api.get("/health").status_code == 200
    assert api.get("/").status_code == 200
    assert api.get("/.well-known/agent-card.json").status_code == 200
    assert api.get("/webhook/zalo").status_code == 200       # Zalo cannot send an API key
    assert api.post("/webhook/zalo", json={}).status_code != 401   # it has its own secret


def test_api_info_leaks_nothing_without_the_key(api):
    for headers in ({}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}):
        r = api.get("/api/info", headers=headers)
        assert r.status_code == 200
        assert r.json() == {"agent": "zalo-restaurant-bot", "auth_required": True}
        assert "memory-secret-id" not in r.text and "gw-private" not in r.text
    full = api.get("/api/info", headers={"X-API-Key": KEY}).json()
    assert full["memory_id"] == "memory-secret-id" and full["mcp_url"] == "https://gw-private.example/restaurant"
    assert full["auth_required"] is True and "llm_model" in full and "zalo_configured" in full


def test_api_info_is_open_when_no_key_is_configured(api, monkeypatch):
    import main

    monkeypatch.setattr(main, "AGENT_API_KEY", "")
    info = api.get("/api/info").json()
    assert info["auth_required"] is False and info["memory_id"] == "memory-secret-id"
    # and nothing else asks for a key: /a2a gets as far as its own header check
    assert api.post("/a2a", json=A2A_BODY).status_code == 400


def test_the_key_is_compared_in_constant_time(api, monkeypatch):
    import main

    calls = []
    real = main.secrets.compare_digest
    monkeypatch.setattr(main.secrets, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    api.get("/api/actors", headers={"X-API-Key": "wrong"})
    api.get("/api/info", headers={"X-API-Key": KEY})
    assert calls == [(b"wrong", KEY.encode()), (KEY.encode(), KEY.encode())]
    non_ascii = {"X-API-Key": "bí".encode("latin-1")}
    assert api.get("/api/actors", headers=non_ascii).status_code == 401  # non-ASCII must not 500


def test_agent_card_advertises_the_key_only_when_one_is_set(api, monkeypatch):
    import main

    card = api.get("/.well-known/agent-card.json").json()
    assert card["securitySchemes"] == {"apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}
    assert card["security"] == [{"apiKey": []}]
    monkeypatch.setattr(main, "AGENT_API_KEY", "")
    card = api.get("/.well-known/agent-card.json").json()
    assert "securitySchemes" not in card and "security" not in card
    assert json.dumps(card)  # still serialisable
