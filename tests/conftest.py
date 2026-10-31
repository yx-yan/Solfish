"""Test helpers: a scripted fake model client, so the agent loop is tested offline."""

from __future__ import annotations

from typing import Any

import pytest
from anthropic.types import Message

from solfish.config import Settings

USAGE = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0}


def message(*content: dict[str, Any], stop: str = "end_turn") -> Message:
    return Message.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": "deepseek-flash",
        "content": list(content), "stop_reason": stop, "stop_sequence": None, "usage": USAGE,
    })


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def tool_use(name: str, inp: dict[str, Any], id: str = "toolu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": id, "name": name, "input": inp}


class FakeClient:
    """Returns the scripted responses in order and records every request."""

    def __init__(self, *responses: Message) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = self
        self.messages = self

    def create(self, **kwargs: Any) -> Message:
        # Snapshot the history: the agent keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(home=tmp_path / "home", max_steps=5)
