"""Unit tests for zalo.py: webhook parsing, secret check, dedupe, splitting, sending, getMe, per-chat ordering."""
import json
import re
import threading
import time

import httpx
import pytest

import zalo


@pytest.fixture(autouse=True)
def zalo_env(monkeypatch):
    """A configured bot with a fresh dedupe cache and getMe cache for every test."""
    monkeypatch.setattr(zalo, "ZALO_WEBHOOK_SECRET", "unit-test-secret")
    monkeypatch.setattr(zalo, "ZALO_BOT_TOKEN", "123:fake-token")
    monkeypatch.setattr(zalo, "_seen", zalo.SeenCache())
    monkeypatch.setattr(zalo, "_me_cache", None)


REAL_CLIENT = httpx.Client


def _mock_zalo(monkeypatch, handler):
    """Route every httpx.Client created inside zalo.py to `handler` (no real network)."""

    def factory(**kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(zalo.httpx, "Client", factory)


# ----------------------------- parse_webhook -----------------------------


def test_parse_webhook_top_level_shape():
    """Zalo really sends event_name/message at the TOP level (not inside result)."""
    payload = {
        "event_name": "message.text.received",
        "message": {
            "date": 1790849894761,
            "chat": {"chat_type": "PRIVATE", "id": "chat-123"},
            "text": "Cho em xem menu",
            "message_id": "msg-1",
            "from": {"id": "user-1", "display_name": "Hung", "is_bot": False},
        },
    }
    assert zalo.parse_webhook(payload) == {
        "message_id": "msg-1",
        "sender_id": "user-1",
        "chat_id": "chat-123",
        "display_name": "Hung",
        "text": "Cho em xem menu",
    }
    assert zalo.event_name(payload) == "message.text.received"


def test_parse_webhook_nested_result_shape():
    """The Zalo docs show the nested-in-result shape: both are supported."""
    payload = {
        "ok": True,
        "result": {
            "event_name": "message.text.received",
            "message": {
                "chat": {"id": "chat-9"},
                "text": "hi",
                "message_id": "msg-2",
                "from": {"id": "user-9"},
            },
        },
    }
    ev = zalo.parse_webhook(payload)
    assert ev["chat_id"] == "chat-9" and ev["text"] == "hi"
    assert ev["display_name"] == ""  # no name known: nothing is invented


def test_parse_webhook_ignores_non_text_events():
    assert zalo.parse_webhook({"event_name": "webhook.test", "message": {"text": "ping"}}) is None
    assert zalo.parse_webhook({"event_name": "message.sticker.received", "message": {}}) is None
    assert zalo.parse_webhook({}) is None


@pytest.mark.parametrize("text", ["", "   ", "\n\t", None])
def test_parse_webhook_ignores_empty_text(text):
    payload = {"event_name": "message.text.received",
               "message": {"chat": {"id": "c"}, "from": {"id": "u"}, "text": text}}
    assert zalo.parse_webhook(payload) is None


def test_parse_webhook_needs_an_identifiable_sender():
    payload = {"event_name": "message.text.received", "message": {"text": "hi", "message_id": "m"}}
    assert zalo.parse_webhook(payload) is None  # no chat id and no sender id: no memory actor to use


def test_parse_webhook_falls_back_between_sender_and_chat_id():
    only_chat = {"event_name": "message.text.received", "message": {"chat": {"id": "c1"}, "text": "hi"}}
    ev = zalo.parse_webhook(only_chat)
    assert ev["sender_id"] == "c1" and ev["chat_id"] == "c1"


# ----------------------------- secret -----------------------------


def test_webhook_secret_ok():
    assert zalo.webhook_secret_ok("unit-test-secret") is True
    assert zalo.webhook_secret_ok("wrong") is False
    assert zalo.webhook_secret_ok("") is False
    assert zalo.webhook_secret_ok("bí-mật-không-ascii") is False  # must not raise on non-ASCII


def test_webhook_secret_fails_closed_without_a_secret(monkeypatch):
    monkeypatch.setattr(zalo, "ZALO_WEBHOOK_SECRET", "")
    assert zalo.webhook_secret_configured() is False
    assert zalo.webhook_secret_ok("") is False
    assert zalo.webhook_secret_ok("anything") is False


def test_webhook_secret_uses_compare_digest(monkeypatch):
    calls = []
    real = zalo.secrets.compare_digest
    monkeypatch.setattr(zalo.secrets, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    assert zalo.webhook_secret_ok("unit-test-secret") is True
    assert calls == [(b"unit-test-secret", b"unit-test-secret")]


# ----------------------------- dedupe -----------------------------


def test_is_duplicate_marks_atomically():
    assert zalo.is_duplicate("m1") is False
    assert zalo.is_duplicate("m1") is True
    assert zalo.is_duplicate("m2") is False


def test_message_without_id_is_never_a_duplicate():
    assert zalo.is_duplicate("") is False
    assert zalo.is_duplicate("") is False


def test_seen_cache_entries_expire():
    now = [100.0]
    cache = zalo.SeenCache(ttl=60, max_size=100, clock=lambda: now[0])
    assert cache.check_and_add("a") is False
    now[0] = 159.0
    assert cache.check_and_add("a") is True
    now[0] = 161.0  # 61 s after the first sighting: expired
    assert cache.check_and_add("a") is False


def test_seen_cache_is_bounded_and_keeps_the_newest():
    cache = zalo.SeenCache(ttl=1000, max_size=3)
    for key in ("a", "b", "c", "d"):
        cache.check_and_add(key)
    assert cache.check_and_add("d") is True and cache.check_and_add("c") is True
    assert cache.check_and_add("a") is False  # "a" was dropped, not the whole cache cleared
    assert cache.check_and_add("b") is False


def test_seen_cache_does_not_forget_everything_at_1000_entries():
    cache = zalo.SeenCache()
    for i in range(1500):
        cache.check_and_add(f"m{i}")
    assert cache.check_and_add("m1499") is True and cache.check_and_add("m1000") is True


# ----------------------------- split_message -----------------------------


def test_short_text_is_a_single_part():
    assert zalo.split_message("ngắn gọn") == ["ngắn gọn"]
    assert zalo.split_message("x" * 2000) == ["x" * 2000]
    assert zalo.split_message("") == [] and zalo.split_message("  \n ") == []


def test_split_never_exceeds_the_limit_and_keeps_all_words():
    para = "đoạn văn. " * 30  # about 300 characters per paragraph
    text = "\n\n".join([para.strip()] * 12)
    parts = zalo.split_message(text)
    assert len(parts) > 1 and all(0 < len(p) <= 2000 for p in parts)
    assert all(p.endswith("văn.") for p in parts)  # cut at paragraph/sentence ends, never mid-sentence
    assert " ".join(" ".join(parts).split()) == " ".join(text.split())  # nothing lost, nothing added


def test_split_prefers_paragraph_boundaries():
    text = "a" * 1960 + "\n\n" + "b" * 500
    assert zalo.split_message(text) == ["a" * 1960, "b" * 500]


def test_split_the_note_case_from_the_old_fit_zalo():
    """The old code appended a ~60 character note after cutting at limit-30 and went over 2,000."""
    text = ("x" * 1980 + "\n\n") + "y" * 100
    assert all(len(p) <= 2000 for p in zalo.split_message(text))


def test_split_hard_cuts_text_without_any_boundary():
    parts = zalo.split_message("z" * 4500)
    assert [len(p) for p in parts] == [2000, 2000, 500]


def test_split_with_emoji_and_custom_limit():
    parts = zalo.split_message("😀" * 25, limit=10)
    assert all(len(p) <= 10 for p in parts) and "".join(parts) == "😀" * 25


# ----------------------------- send_message -----------------------------


def test_send_message_sends_the_parts_in_order(monkeypatch):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    _mock_zalo(monkeypatch, handler)
    text = "\n\n".join(f"paragraph {i} " + "w" * 900 for i in range(5))
    result = zalo.send_message("chat-1", text)
    assert result == {"ok": True, "parts": len(sent)} and len(sent) >= 3
    assert all(body["chat_id"] == "chat-1" and body["parse_mode"] == "markdown" for body in sent)
    assert all(len(body["text"]) <= 2000 for body in sent)
    assert re.findall(r"paragraph (\d)", " ".join(body["text"] for body in sent)) == list("01234")  # order kept
    assert "tiếp" not in " ".join(body["text"] for body in sent)  # the old "reply 'tiếp'" note is gone


def test_send_message_stops_at_the_first_failure(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, text="boom") if len(calls) == 2 else httpx.Response(200, json={"ok": True})

    _mock_zalo(monkeypatch, handler)
    result = zalo.send_message("c", "\n\n".join("q" * 1500 for _ in range(4)))
    assert result["ok"] is False and result["sent"] == 1 and result["parts"] == 4
    assert len(calls) == 2


def test_send_message_survives_network_errors_and_bad_json(monkeypatch):
    def down(request):
        raise httpx.ConnectError("no route", request=request)

    _mock_zalo(monkeypatch, down)
    down_result = zalo.send_message("c", "hi")
    assert down_result["ok"] is False and down_result["sent"] == 0 and "ConnectError" in down_result["error"]
    _mock_zalo(monkeypatch, lambda request: httpx.Response(200, text="not json"))
    assert zalo.send_message("c", "hi")["ok"] is False
    assert zalo.send_message("c", "   ")["ok"] is False  # nothing to send


# ----------------------------- get_me -----------------------------


def test_get_me_makes_no_call_without_a_token(monkeypatch):
    monkeypatch.setattr(zalo, "ZALO_BOT_TOKEN", "")

    def boom(request):
        raise AssertionError("no HTTP call is allowed without a token")

    _mock_zalo(monkeypatch, boom)
    assert zalo.get_me() == {} and zalo.bot_name() == ""


def test_get_me_failure_is_swallowed_and_does_not_leak_the_token(monkeypatch, caplog):
    def down(request):
        raise httpx.ConnectError("no route", request=request)

    _mock_zalo(monkeypatch, down)
    with caplog.at_level("WARNING"):
        assert zalo.get_me() == {} and zalo.bot_name() == ""
    assert "ConnectError" in caplog.text and "fake-token" not in caplog.text


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, text="<html>"),
                                      httpx.Response(200, json={"ok": False}), httpx.Response(200, json=[1])])
def test_get_me_bad_answers_give_an_empty_result(monkeypatch, response):
    _mock_zalo(monkeypatch, lambda request: response)
    assert zalo.get_me() == {} and zalo.bot_name() == ""


def test_get_me_caches_success_and_retries_failure_soon(monkeypatch):
    calls = []
    down = [True]

    def handler(request):
        calls.append(request.url.path)
        if down[0]:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"display_name": "Quán Ngon"}})

    def expire_cache():
        monkeypatch.setattr(zalo, "_me_cache", (time.monotonic() - 1, zalo._me_cache[1]))

    _mock_zalo(monkeypatch, handler)
    assert zalo.bot_name() == ""                       # Zalo is down
    assert zalo.bot_name() == "" and len(calls) == 1   # the failure is cached briefly: no hammering
    assert zalo.ME_RETRY_SECONDS - 1 < zalo._me_cache[0] - time.monotonic() <= zalo.ME_RETRY_SECONDS
    expire_cache()
    down[0] = False
    assert zalo.bot_name() == "Quán Ngon" and len(calls) == 2   # recovered: a failure does not stick
    assert calls[0].endswith("/getMe")
    assert zalo.bot_name() == "Quán Ngon" and len(calls) == 2   # a success is cached ...
    assert zalo.ME_TTL_SECONDS - 1 < zalo._me_cache[0] - time.monotonic() <= zalo.ME_TTL_SECONDS
    expire_cache()
    assert zalo.bot_name() == "Quán Ngon" and len(calls) == 3   # ... until the TTL runs out


# ----------------------------- ChatDispatcher -----------------------------


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_dispatcher_keeps_per_chat_order_and_loses_nothing():
    done = []

    def handler(item):
        if item["n"] == 1:
            time.sleep(0.2)  # the first message is slow: the second must still wait for it
        done.append(item["n"])

    dispatcher = zalo.ChatDispatcher(handler, max_workers=4)
    for n in (1, 2, 3):
        dispatcher.submit("chat-A", {"n": n})
    assert _wait(lambda: len(done) == 3)
    assert done == [1, 2, 3]
    assert _wait(lambda: not dispatcher._queues)  # nothing is left behind for a finished chat


def test_dispatcher_runs_different_chats_concurrently_but_bounded():
    lock = threading.Lock()
    running = {"now": 0, "max": 0, "done": 0}
    gate = threading.Event()

    def handler(item):
        with lock:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
        gate.wait(timeout=5)
        with lock:
            running["now"] -= 1
            running["done"] += 1

    dispatcher = zalo.ChatDispatcher(handler, max_workers=2)
    for i in range(6):
        dispatcher.submit(f"chat-{i}", {})
    assert _wait(lambda: running["now"] == 2)
    time.sleep(0.1)
    assert running["max"] == 2  # never more than max_workers at once (not one thread per message)
    gate.set()
    assert _wait(lambda: running["done"] == 6)


def test_dispatcher_survives_a_failing_handler(caplog):
    done = []

    def handler(item):
        if item["n"] == 1:
            raise RuntimeError("boom")
        done.append(item["n"])

    dispatcher = zalo.ChatDispatcher(handler, max_workers=1)
    dispatcher.submit("c", {"n": 1})
    dispatcher.submit("c", {"n": 2})
    assert _wait(lambda: done == [2])
    assert "handler failed" in caplog.text
