"""Test doubles: scripted chat model, in-memory AgentBase Memory API, MCP stubs."""
from __future__ import annotations

import json

from greennode_agentbase.memory.models import EventEntity, ListResponseEventEntity
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


def tool_call(call_id: str, name: str = "get_menu", **args) -> dict:
    return {"name": name, "args": args or {"category": call_id}, "id": call_id, "type": "tool_call"}


class ScriptedChatModel(BaseChatModel):
    """Plays back `script` (a list of AIMessage). The summarization middleware's call is
    answered separately and recorded in `summaries`. `seen` has the messages of every agent call."""

    script: list = []
    seen: list = []
    summaries: list = []
    fail_with: list = []  # exceptions raised by the next calls, in order

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _next(self, messages) -> AIMessage:
        if len(messages) == 1 and "Context Extraction Assistant" in str(messages[0].content):
            self.summaries.append(messages[0])
            return AIMessage("SUMMARY")
        self.seen.append(list(messages))
        if self.fail_with:
            raise self.fail_with.pop(0)
        return self.script.pop(0) if self.script else AIMessage("default answer")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        message = self._next(messages)
        if message.tool_calls:
            chunks = [
                {"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i}
                for i, c in enumerate(message.tool_calls)
            ]
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=chunks))
            return
        for word in str(message.content).split(" "):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=word + " "))
            if run_manager:
                run_manager.on_llm_new_token(word + " ", chunk=chunk)
            yield chunk


class FakeCheckpointApi:
    """Sync part of the Memory API used by the AgentBaseMemoryEvents checkpointer."""

    def __init__(self):
        self.events: list = []
        self.creates = 0

    def create_event(self, id, actorId, sessionId, request):  # noqa: A002 - SDK keyword names
        self.creates += 1
        self.events.append(EventEntity(id=str(len(self.events)), payload=request.payload.model_dump()))

    def list_events(self, id, actorId, sessionId, page, size):  # noqa: A002
        data = self.events[(page - 1) * size : page * size]
        return ListResponseEventEntity(listData=data, totalPage=max(1, -(-len(self.events) // size)))


class FakeSdk:
    def __init__(self):
        self.inserts: list = []
        self.searches: list = []
        self.insert_errors: list = []
        self.search_results = {}  # namespace -> records or an Exception

    async def insert_memory_records_directly_async(self, id, namespace, request):  # noqa: A002
        self.inserts.append((namespace, request.memory_records))
        if self.insert_errors:
            raise self.insert_errors.pop(0)

    async def search_memory_records_async(self, id, namespace, request):  # noqa: A002
        self.searches.append((namespace, request))
        result = self.search_results.get(namespace, [])
        if isinstance(result, Exception):
            raise result
        return result
