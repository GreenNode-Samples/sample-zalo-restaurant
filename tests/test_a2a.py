"""A2A protocol (agent card + JSON-RPC message/send): unit tests, no network."""

# ── agent card (discovery: GET /.well-known/agent-card.json) ──
def test_a2a_card_shape():
    import main

    card = main._a2a_card()
    assert card["name"] == "zalo-restaurant-bot"
    assert card["protocolVersion"] == "0.3.0"
    assert card["preferredTransport"] == "JSONRPC"
    assert card["url"].endswith("/a2a")
    caps = card["capabilities"]
    assert caps["streaming"] is False  # only message/send is supported
    assert card["defaultInputModes"] == ["text/plain"]
    assert card["defaultOutputModes"] == ["text/plain"]


def test_a2a_card_skills():
    import main

    card = main._a2a_card()
    ids = [s["id"] for s in card["skills"]]
    assert "restaurant-consultation" in ids
    for s in card["skills"]:
        assert s["name"] and s["description"] and s.get("tags")


# ── _a2a_text: extract the text from message.parts ──
def test_a2a_text_kind_text():
    import main

    params = {"message": {"parts": [
        {"kind": "text", "text": "đặt bàn "},
        {"kind": "text", "text": "4 người"},
    ]}}
    assert main._a2a_text(params) == "đặt bàn 4 người"


def test_a2a_text_legacy_part():
    import main

    params = {"message": {"parts": [{"text": "legacy"}]}}
    assert main._a2a_text(params) == "legacy"


def test_a2a_text_ignores_non_text():
    import main

    params = {"message": {"parts": [
        {"kind": "file", "file": {"bytes": "AA=="}},
        {"kind": "text", "text": "ok"},
    ]}}
    assert main._a2a_text(params) == "ok"


def test_a2a_text_empty():
    import main

    assert main._a2a_text({}) == ""
    assert main._a2a_text(None) == ""
    assert main._a2a_text({"message": {}}) == ""
    assert main._a2a_text({"message": {"parts": []}}) == ""


# ── LangFuse v4 helpers: tracing is off when env vars are missing (the turn runs normally) ──
def test_lf_helpers_off_without_env(monkeypatch):
    import main

    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
        monkeypatch.delenv(k, raising=False)
    assert main._lf_tracing() is False
    with main._lf_scope("t", "u", "s", ["x"]):
        pass  # nullcontext -> does not raise
    assert main._lf_callback() is None
