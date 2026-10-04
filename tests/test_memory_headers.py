"""Memory headers: a missing X-GreenNode-AgentBase-User-Id / -Session-Id -> 400, NO default fallback.

AgentBase docs: "If your agent uses memory, validate that these headers are present and
return an error if missing. Do not fall back to default values."
(The Zalo webhook is not covered: its actor/session come from Zalo's sender_id/chat_id.)
"""
import pytest
from starlette.testclient import TestClient

USER = "X-GreenNode-AgentBase-User-Id"
SESSION = "X-GreenNode-AgentBase-Session-Id"


@pytest.fixture()
def client(monkeypatch):
    import main

    monkeypatch.setattr(main, "AGENT_API_KEY", "")

    def _boom(*a, **k):
        raise AssertionError("the agent must NOT run when the headers are missing")

    monkeypatch.setattr(main.agent_mod, "get_agent", _boom)
    return TestClient(main.app, raise_server_exceptions=False)


def test_missing_identity_helper():
    import main

    assert main._missing_identity("", "s")
    assert main._missing_identity("u", "")
    assert main._missing_identity(None, None)
    assert main._missing_identity("  ", "s")
    assert not main._missing_identity("u", "s")


@pytest.mark.parametrize(
    "headers",
    [{}, {USER: "alice"}, {SESSION: "s-1"}],
    ids=["none", "only-user", "only-session"],
)
def test_invocations_missing_headers_400(client, headers):
    r = client.post("/invocations", json={"message": "hi"}, headers=headers)
    assert r.status_code == 400
    assert "X-GreenNode-AgentBase-User-Id" in r.text


def test_a2a_requires_user_header_no_shared_actor(client):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text", "text": "xin chào"}]}},
    }
    r = client.post("/a2a", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32602


def test_a2a_uses_header_user_as_actor(client, monkeypatch):
    import main

    seen = {}

    async def _fake_turn(actor_id, session_id, message, trace_name="x"):
        seen.update(actor=actor_id, session=session_id)
        return {"status": "success", "response": "ok"}

    monkeypatch.setattr(main, "_chat_turn", _fake_turn)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text", "text": "xin chào"}]}},
    }
    r = client.post("/a2a", json=body, headers={USER: "alice", SESSION: "sess-9"})
    assert r.status_code == 200
    assert seen == {"actor": "alice", "session": "sess-9"}
