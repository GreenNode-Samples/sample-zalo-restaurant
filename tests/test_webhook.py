"""POST /webhook/zalo end to end (fake agent turn, fake Zalo sender): secret, parsing, dedupe,
ordering, daily sessions, empty messages, failure path. No network."""
import asyncio
import logging
import queue
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import agent
import memory_tools
import zalo

SECRET = "s3cret-webhook-value"
HEADERS = {"X-Bot-Api-Secret-Token": SECRET}


def msg(text="Cho em xem menu", mid="m-1", chat="chat-1", user="user-1", name="Hung"):
    return {
        "event_name": "message.text.received",
        "message": {
            "message_id": mid,
            "text": text,
            "chat": {"id": chat, "chat_type": "PRIVATE"},
            "from": {"id": user, "display_name": name},
        },
    }


@pytest.fixture()
def hook(monkeypatch):
    """A configured bot whose agent turn and Zalo sender are fakes that record what they get."""
    import main

    monkeypatch.setattr(zalo, "ZALO_BOT_TOKEN", "123:fake-token")
    monkeypatch.setattr(zalo, "ZALO_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(zalo, "_seen", zalo.SeenCache())
    monkeypatch.setattr(main, "AGENT_API_KEY", "")
    clock = {"now": datetime(2030, 3, 4, 23, 59, tzinfo=agent.TZ_VN)}
    monkeypatch.setattr(agent, "now_vn", lambda: clock["now"])

    class Hook:
        def __init__(self):
            self.client = TestClient(main.app, raise_server_exceptions=False)
            self.clock = clock
            self.turns = []                 # (text, actor, session, guest_name) of every agent turn
            self.saved = []                 # (actor, session, user_text, bot_text) of the history writer
            self.sent = queue.Queue()       # (chat_id, text) of every Zalo message sent
            self.delays = {}                # message text -> seconds the fake turn takes
            self.fail = set()               # message texts whose turn raises

        def post(self, payload, headers=HEADERS):
            return self.client.post("/webhook/zalo", json=payload, headers=headers)

        def replies(self, n, timeout=5.0):
            return [self.sent.get(timeout=timeout) for _ in range(n)]

    h = Hook()

    async def fake_turn(text, user_id, session_id, *, guest_name="", callbacks=None):
        h.turns.append((text, user_id, session_id, guest_name))
        await asyncio.sleep(h.delays.get(text, 0))
        if text in h.fail:
            raise RuntimeError("secret internal failure: http://10.0.0.5/boom")
        return agent.TurnResult(f"re: {text}", [])

    async def fake_events(user_id, session_id, user_text, bot_text):
        h.saved.append((user_id, session_id, user_text, bot_text))

    def fake_send(chat_id, text):
        h.sent.put((chat_id, text))
        return {"ok": True, "parts": 1}

    monkeypatch.setattr(agent, "run_turn", fake_turn)
    monkeypatch.setattr(memory_tools, "add_chat_events", fake_events)
    monkeypatch.setattr(zalo, "send_message", fake_send)
    return h


# ----------------------------- secret -----------------------------


def test_webhook_refused_without_a_secret_when_a_token_is_set(hook, monkeypatch, caplog):
    monkeypatch.setattr(zalo, "ZALO_WEBHOOK_SECRET", "")
    with caplog.at_level(logging.ERROR, logger="zalo-restaurant-bot"):
        r = hook.post(msg(), headers={})
        r2 = hook.post(msg(), headers={"X-Bot-Api-Secret-Token": ""})
    assert r.status_code == r2.status_code == 503
    assert "ZALO_WEBHOOK_SECRET" in caplog.text
    assert hook.turns == []  # nothing was processed: it is not open "in dev mode"


def test_webhook_disabled_without_a_token(hook, monkeypatch):
    monkeypatch.setattr(zalo, "ZALO_BOT_TOKEN", "")
    assert hook.post(msg()).status_code == 503
    assert hook.turns == []


@pytest.mark.parametrize("headers", [{}, {"X-Bot-Api-Secret-Token": "wrong"}, {"X-Bot-Api-Secret-Token": ""}])
def test_wrong_or_missing_secret_is_403(hook, headers):
    assert hook.post(msg(), headers=headers).status_code == 403
    assert hook.turns == []


def test_secret_is_verified_before_the_body_is_parsed(hook, monkeypatch):
    import main

    parsed = []
    real_parse = zalo.parse_webhook
    monkeypatch.setattr(zalo, "parse_webhook", lambda p: parsed.append(p) or real_parse(p))
    # a broken body with a wrong secret: 403, not "200 ignored invalid json" (the body was never read)
    r = hook.client.post("/webhook/zalo", content=b"{not json", headers={"X-Bot-Api-Secret-Token": "wrong"})
    assert r.status_code == 403 and r.json() == {"status": "denied"}
    assert parsed == []

    async def body_must_not_be_read(self):
        raise AssertionError("request.json() was called before the secret was verified")

    monkeypatch.setattr(main.Request, "json", body_must_not_be_read)
    assert hook.post(msg(), headers={}).status_code == 403


def test_a_broken_body_with_the_right_secret_still_gets_200(hook):
    r = hook.client.post("/webhook/zalo", content=b"{not json", headers=HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    r = hook.client.post("/webhook/zalo", json=["not", "an", "object"], headers=HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert hook.turns == []


# ----------------------------- happy path, dedupe, ignored events -----------------------------


def test_message_is_answered_with_the_guest_name(hook):
    r = hook.post(msg("Đặt bàn 4 người", name="Hung", user="user-7", chat="chat-7"))
    assert r.status_code == 200 and r.json() == {"message": "Success", "accepted": True}
    assert hook.replies(1) == [("chat-7", "re: Đặt bàn 4 người")]
    # actor = the Zalo sender, session = one thread per chat and day, the display name reaches the agent
    assert hook.turns == [("Đặt bàn 4 người", "user-7", "zalo-chat-7-20300304", "Hung")]
    assert hook.saved == [("user-7", "zalo-chat-7-20300304", "Đặt bàn 4 người", "re: Đặt bàn 4 người")]


def test_retried_message_is_processed_once(hook):
    assert hook.post(msg(mid="dup-1")).json()["accepted"] is True
    hook.replies(1)
    again = hook.post(msg(mid="dup-1"))
    assert again.status_code == 200 and again.json() == {"message": "Success"}
    assert len(hook.turns) == 1


def test_retry_while_the_first_turn_is_still_running_is_dropped(hook):
    hook.delays["slow"] = 0.3
    hook.post(msg("slow", mid="dup-2"))
    assert hook.post(msg("slow", mid="dup-2")).json() == {"message": "Success"}
    hook.replies(1)
    assert len(hook.turns) == 1


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_empty_message_is_ignored_not_turned_into_hello(hook, caplog, text):
    with caplog.at_level(logging.INFO, logger="zalo-restaurant-bot"):
        r = hook.post(msg(text))
    assert r.status_code == 200 and r.json() == {"message": "Success"}
    assert "webhook ignored" in caplog.text and "message.text.received" in caplog.text
    assert hook.turns == [] and hook.sent.empty()


def test_non_text_events_are_ignored_without_logging_their_content(hook, caplog):
    sticker = {"event_name": "message.sticker.received", "message": {"text": "private words"}}
    with caplog.at_level(logging.INFO, logger="zalo-restaurant-bot"):
        r = hook.post(sticker)
    assert r.status_code == 200 and hook.turns == []
    assert "message.sticker.received" in caplog.text and "private words" not in caplog.text


# ----------------------------- ordering -----------------------------


def test_two_quick_messages_from_one_chat_are_answered_in_order(hook):
    hook.delays["first"] = 0.3  # the first turn is the slow one
    hook.post(msg("first", mid="o-1"))
    hook.post(msg("second", mid="o-2"))
    assert hook.replies(2) == [("chat-1", "re: first"), ("chat-1", "re: second")]
    assert [t[0] for t in hook.turns] == ["first", "second"]


def test_a_slow_chat_does_not_block_another_chat(hook):
    hook.delays["slow"] = 0.5
    hook.post(msg("slow", mid="c-1", chat="chat-A", user="A"))
    hook.post(msg("fast", mid="c-2", chat="chat-B", user="B"))
    assert hook.replies(2) == [("chat-B", "re: fast"), ("chat-A", "re: slow")]


# ----------------------------- failure path -----------------------------


def test_a_failed_turn_sends_a_generic_apology_and_logs_a_request_id(hook, caplog):
    import main

    hook.fail.add("boom")
    with caplog.at_level(logging.ERROR, logger="zalo-restaurant-bot"):
        hook.post(msg("boom"))
        ((chat_id, text),) = hook.replies(1)
    assert chat_id == "chat-1" and text == main.GUEST_APOLOGY
    assert "secret internal failure" not in text and "RuntimeError" not in text
    assert re.search(r"\[[0-9a-f]{8}\] .*chat-1", caplog.text)  # the log line carries a request id
    assert "secret internal failure" in caplog.text  # the details stay in the server log
    assert hook.saved == []  # a failed turn is not recorded as a conversation


def test_the_chat_goes_on_after_a_failed_turn(hook):
    hook.fail.add("boom")
    hook.post(msg("boom", mid="f-1"))
    hook.post(msg("fine", mid="f-2"))
    assert [text for _, text in hook.replies(2)][1] == "re: fine"


# ----------------------------- sessions rotate daily -----------------------------


def test_the_session_id_has_the_vietnam_date(hook):
    import main

    assert main._zalo_session_id("chat-1") == "zalo-chat-1-20300304"
    hook.clock["now"] = datetime(2030, 3, 5, 0, 1, tzinfo=agent.TZ_VN)  # just after midnight in Vietnam
    assert main._zalo_session_id("chat-1") == "zalo-chat-1-20300305"
    hook.clock["now"] = datetime(2030, 3, 4, 17, 30, tzinfo=agent.TZ_VN).astimezone(UTC)
    assert main._zalo_session_id("chat-1") == "zalo-chat-1-20300304"  # the date follows Vietnam, not the clock's zone


def test_a_chat_gets_a_fresh_session_the_next_day_and_the_same_actor(hook):
    hook.post(msg("late", mid="d-1"))
    hook.replies(1)
    hook.clock["now"] = datetime(2030, 3, 5, 0, 2, tzinfo=agent.TZ_VN)
    hook.post(msg("early", mid="d-2"))
    hook.replies(1)
    assert [(t[1], t[2]) for t in hook.turns] == [
        ("user-1", "zalo-chat-1-20300304"), ("user-1", "zalo-chat-1-20300305"),
    ]  # new session, same actor: long-term memory carries the guest across days


@pytest.mark.parametrize("sender,chat", [("bad id", "chat-1"), ("user-1", "chat/1"), ("x" * 200, "chat-1"), ("user-1", "c" * 120)])
def test_unusual_ids_are_ignored_not_turned_into_memory_namespaces(hook, sender, chat):
    r = hook.post(msg(user=sender, chat=chat))
    assert r.status_code == 200 and r.json() == {"message": "Success"}
    assert hook.turns == [] and hook.sent.empty()


# ----------------------------- recording the conversation -----------------------------


def test_recording_chat_events_failure_does_not_fail_the_turn(monkeypatch, caplog):
    """The history writer is best effort: a memory outage must not turn a good reply into an apology."""
    async def memory_down(**kwargs):
        raise RuntimeError("memory service unavailable")

    import main

    async def fake_turn(text, user_id, session_id, *, guest_name="", callbacks=None):
        return agent.TurnResult("Xin chào Hung!", [])

    monkeypatch.setattr(agent, "run_turn", fake_turn)
    monkeypatch.setattr(memory_tools, "_client", SimpleNamespace(create_event_async=memory_down))
    with caplog.at_level(logging.WARNING, logger="memory-tools"):
        result = asyncio.run(memory_tools.arun_coro(main._turn_job("t", [], "hi", "u1", "zalo-c1-20300304", "Hung")))
    assert result.reply == "Xin chào Hung!"
    assert "could not save conversation events" in caplog.text
