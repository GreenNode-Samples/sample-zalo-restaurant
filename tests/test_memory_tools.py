"""memory_tools: remember / recall, retry policy, listings, the persistent agent loop."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from greennode_agentbase.exceptions import GreenNodeRequestError

import memory_tools
from fakes import FakeSdk


def sdk_error(status=None, cause=None):
    """What the SDK raises: GreenNodeRequestError wrapping the httpx error."""
    err = GreenNodeRequestError(f"HTTP {status}" if status else "request failed", status_code=status, cause=cause)
    err.__cause__ = cause
    return err


CONNECT = httpx.ConnectError("refused")
READ_TIMEOUT = httpx.ReadTimeout("slow")


@pytest.fixture()
def sdk(monkeypatch):
    fake = FakeSdk()
    monkeypatch.setattr(memory_tools, "_client", fake)
    monkeypatch.setattr(memory_tools, "_RETRY_BASE_DELAY", 0)
    return fake


def call(tool, **args):
    config = {"configurable": {"actor_id": "alice"}}
    return asyncio.run(tool.ainvoke({"type": "tool_call", "id": "c1", "name": tool.name, "args": args}, config=config))


def test_field_reads_dicts_and_objects():
    assert memory_tools.field({"memory": "abc"}, "memory") == "abc"
    assert memory_tools.field({"memory": None}, "memory", "x") == "x"
    assert memory_tools.field(SimpleNamespace(memory="xyz"), "memory") == "xyz"
    assert memory_tools.field(SimpleNamespace(), "missing", "d") == "d"


def test_build_namespace_format():
    assert memory_tools.build_namespace("alice", "ltms-1") == "/strategies/ltms-1/actors/alice"
    assert memory_tools.build_namespace("bob") == "/strategies/ltms-cust-test/actors/bob"  # default strategy


PROFILE_NS = "/strategies/ltms-cust-test/actors/alice"
OTHER_NS = "/strategies/ltms-other-test/actors/alice"


def test_remember_writes_to_the_guest_profile_namespace(sdk):
    msg = call(memory_tools.remember, fact="  Allergic to peanuts ")
    assert sdk.inserts == [(PROFILE_NS, ["Allergic to peanuts"])]
    assert msg.artifact == ["Allergic to peanuts"]
    assert "Allergic to peanuts" in msg.content


def test_remember_does_not_retry_a_5xx_or_timeout(sdk):
    """A retried insert could store the same record twice."""
    for error in (sdk_error(503), sdk_error(cause=READ_TIMEOUT)):
        sdk.inserts.clear()
        sdk.insert_errors[:] = [error]
        with pytest.raises(GreenNodeRequestError):
            call(memory_tools.remember, fact="x")
        assert len(sdk.inserts) == 1


def test_remember_retries_connection_errors(sdk):
    sdk.insert_errors[:] = [sdk_error(cause=CONNECT), sdk_error(cause=CONNECT)]
    call(memory_tools.remember, fact="x")
    assert len(sdk.inserts) == 3  # honours attempts=3: two failures, then success


def test_remember_gives_up_after_the_configured_attempts(sdk):
    sdk.insert_errors[:] = [sdk_error(cause=CONNECT)] * 5
    with pytest.raises(GreenNodeRequestError):
        call(memory_tools.remember, fact="x")
    assert len(sdk.inserts) == 3


def test_remember_rejects_empty_and_oversized_facts(sdk):
    with pytest.raises(Exception, match="empty"):
        call(memory_tools.remember, fact="   ")
    with pytest.raises(Exception, match="too long"):
        call(memory_tools.remember, fact="x" * (memory_tools.MAX_FACT_CHARS + 1))
    assert sdk.inserts == []


def test_recall_searches_the_configured_strategy_with_threshold_and_top_k(sdk):
    sdk.search_results = {PROFILE_NS: [{"memory": "vegetarian", "score": 0.9}, {"memory": "birthday in May", "score": 0.4}]}
    msg = call(memory_tools.recall, query="preferences")
    assert [ns for ns, _ in sdk.searches] == [PROFILE_NS]
    for _, request in sdk.searches:
        assert request.limit == memory_tools.RECALL_LIMIT
        assert request.score_threshold == memory_tools.RECALL_MIN_SCORE
    assert msg.artifact == ["vegetarian", "birthday in May"]
    assert msg.content.splitlines()[0] == "- vegetarian (score: 0.90)"


def test_recall_merges_several_strategies_best_score_first(sdk, monkeypatch):
    """The strategy list is configuration: with more than one, results are merged and de-duplicated."""
    monkeypatch.setattr(memory_tools, "_recall_strategy_ids", lambda: ["ltms-cust-test", "ltms-other-test"])
    sdk.search_results = {
        PROFILE_NS: [{"memory": "vegetarian", "score": 0.9}, {"memory": "birthday in May", "score": 0.4}],
        OTHER_NS: [{"memory": "usual table T3", "score": 0.7}, {"memory": "vegetarian", "score": 0.5}],
    }
    msg = call(memory_tools.recall, query="preferences")
    assert {ns for ns, _ in sdk.searches} == {PROFILE_NS, OTHER_NS}
    assert msg.artifact == ["vegetarian", "usual table T3", "birthday in May"]


def test_the_strategy_list_is_the_configured_strategy(monkeypatch):
    assert memory_tools._recall_strategy_ids() == ["ltms-cust-test"]
    monkeypatch.setattr(memory_tools, "MEMORY_STRATEGY_ID", "")
    assert memory_tools._recall_strategy_ids() == []


def test_recall_with_no_hits(sdk):
    msg = call(memory_tools.recall, query="anything")
    assert msg.content == "No relevant memories found."
    assert msg.artifact == []


def test_recall_survives_one_failing_strategy(sdk, monkeypatch):
    monkeypatch.setattr(memory_tools, "_recall_strategy_ids", lambda: ["ltms-cust-test", "ltms-other-test"])
    sdk.search_results = {PROFILE_NS: sdk_error(400), OTHER_NS: [{"memory": "fact", "score": 0.8}]}
    assert call(memory_tools.recall, query="q").artifact == ["fact"]


def test_recall_fails_when_every_strategy_fails(sdk):
    sdk.search_results = {PROFILE_NS: sdk_error(400)}
    with pytest.raises(GreenNodeRequestError):
        call(memory_tools.recall, query="q")


def test_recall_retries_transient_search_errors(sdk):
    failures = [sdk_error(503), sdk_error(cause=READ_TIMEOUT)]
    original = sdk.search_memory_records_async

    async def flaky(id, namespace, request):  # noqa: A002
        if failures:
            sdk.searches.append((namespace, request))
            raise failures.pop(0)
        return await original(id, namespace, request)

    sdk.search_memory_records_async = flaky
    sdk.search_results = {PROFILE_NS: [{"memory": "ok", "score": 0.9}]}
    assert call(memory_tools.recall, query="q").artifact == ["ok"]
    assert len(sdk.searches) == 3


def test_a_tool_without_actor_id_refuses_to_run(sdk):
    with pytest.raises(RuntimeError, match="actor_id"):
        asyncio.run(memory_tools.remember.ainvoke({"fact": "x"}))
    assert sdk.inserts == []


def test_with_retry_classification():
    server_error, timeout, connect = sdk_error(500), sdk_error(cause=READ_TIMEOUT), sdk_error(cause=CONNECT)
    assert memory_tools._is_transient(server_error) and memory_tools._is_transient(timeout)
    assert not memory_tools._is_transient(sdk_error(404))
    assert memory_tools._is_connect_failure(connect)
    assert not memory_tools._is_connect_failure(server_error)
    assert not memory_tools._is_connect_failure(timeout)


# --- listings ----------------------------------------------------------------------------

def page(items, total_page):
    return SimpleNamespace(list_data=items, total_page=total_page)


def event(role, message, ts, type_="conversational"):
    payload = SimpleNamespace(type=type_, role=role, message=message, binary_data=None)
    return SimpleNamespace(payload=payload, event_timestamp=ts)


def test_history_pages_past_checkpoint_blobs(monkeypatch):
    """Binary checkpoint events must not starve the conversation messages."""
    blobs = [event(None, None, f"2026-01-01T00:00:{i:02d}", type_="binary") for i in range(100)]
    chat = [event("assistant", "answer", "2026-01-01T00:01:01"), event("user", "question", "2026-01-01T00:01:00")]
    pages = {1: page(blobs, 2), 2: page(chat, 2)}  # newest first, as the API lists them

    async def list_events_async(id, actorId, sessionId, page, size):  # noqa: A002
        return pages[page]

    monkeypatch.setattr(memory_tools, "_client", SimpleNamespace(list_events_async=list_events_async))
    messages = asyncio.run(memory_tools.list_conversation("alice", "s1", limit=50))
    assert [(m["role"], m["message"]) for m in messages] == [("user", "question"), ("assistant", "answer")]


def test_history_keeps_only_the_newest_messages(monkeypatch):
    events = [event("user", f"m{i}", f"2026-01-01T00:00:{i:02d}") for i in range(9, -1, -1)]

    async def list_events_async(id, actorId, sessionId, page, size):  # noqa: A002
        return SimpleNamespace(list_data=events, total_page=1)

    monkeypatch.setattr(memory_tools, "_client", SimpleNamespace(list_events_async=list_events_async))
    messages = asyncio.run(memory_tools.list_conversation("alice", "s1", limit=3))
    assert [m["message"] for m in messages] == ["m7", "m8", "m9"]


def test_actor_listing_pages_through_everything(monkeypatch):
    actors = {1: page([SimpleNamespace(actor_id="a1")], 2), 2: page([SimpleNamespace(actor_id="a2")], 2)}

    async def list_actors_async(id, page, size):  # noqa: A002
        return actors[page]

    async def list_sessions_async(id, actorId, page, size):  # noqa: A002
        if actorId == "a2":
            raise RuntimeError("boom")
        return SimpleNamespace(list_data=[SimpleNamespace(session_id="s2"), SimpleNamespace(session_id="s1")], total_page=1)

    client = SimpleNamespace(list_actors_async=list_actors_async, list_sessions_async=list_sessions_async)
    monkeypatch.setattr(memory_tools, "_client", client)
    assert asyncio.run(memory_tools.list_actors()) == [
        {"actorId": "a1", "sessions": ["s1", "s2"]},
        {"actorId": "a2", "sessions": []},  # one failing actor does not break the listing
    ]


def test_add_chat_events_never_raises(monkeypatch):
    async def create_event_async(**kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(memory_tools, "_client", SimpleNamespace(create_event_async=create_event_async))
    asyncio.run(memory_tools.add_chat_events("alice", "s1", "hi", "hello"))


# --- agent loop ----------------------------------------------------------------------------

def test_arun_coro_runs_on_the_agent_loop_without_blocking_the_caller():
    async def where():
        await asyncio.sleep(0)
        return asyncio.get_running_loop()

    async def main():
        ticks = []

        async def ticker():
            for _ in range(3):
                await asyncio.sleep(0.01)
                ticks.append(1)

        async def slow():
            await asyncio.sleep(0.1)
            return await where()

        loop, _ = await asyncio.gather(memory_tools.arun_coro(slow()), ticker())
        return loop, ticks

    loop, ticks = asyncio.run(main())
    assert loop is memory_tools.agent_loop()
    assert len(ticks) == 3  # the caller's loop stayed responsive


def test_stream_on_loop_relays_items_and_errors():
    async def produce(emit):
        emit("a")
        await asyncio.sleep(0)
        emit("b")

    async def fail(emit):
        emit("x")
        raise ValueError("boom")

    async def collect(job):
        out = []
        async for item in memory_tools.stream_on_loop(job):
            out.append(item)
        return out

    assert asyncio.run(collect(produce)) == ["a", "b"]

    async def failing():
        out = []
        with pytest.raises(ValueError, match="boom"):
            async for item in memory_tools.stream_on_loop(fail):
                out.append(item)
        return out

    assert asyncio.run(failing()) == ["x"]


def test_stream_on_loop_cancels_the_producer_when_the_consumer_stops():
    started = threading.Event()
    cancelled = threading.Event()

    async def endless(emit):
        started.set()
        try:
            while True:
                emit("tick")
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def consume_one():
        async for _ in memory_tools.stream_on_loop(endless):
            break

    asyncio.run(consume_one())
    assert started.wait(2) and cancelled.wait(2)
