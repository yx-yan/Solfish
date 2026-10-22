"""Tool abstraction: a typed function the model can call.

Each tool declares its parameters as a Pydantic model. That one model gives us both the JSON
Schema sent to the API and validation of whatever the model sends back.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class ToolError(Exception):
    """An expected failure whose message the model should see and can recover from."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: type[BaseModel]
    fn: Callable[[Any], str]
    # Side-effecting tools (writes, deletes, anything outward-facing) ask the human first.
    needs_approval: bool = False

    def definition(self) -> dict[str, Any]:
        schema = self.params.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}


def tool(
    params: type[BaseModel], *, name: str | None = None, needs_approval: bool = False
) -> Callable[[Callable[[Any], str]], Tool]:
    """Turn a function into a Tool. The docstring becomes the description the model reads."""

    def wrap(fn: Callable[[Any], str]) -> Tool:
        doc = inspect.getdoc(fn)
        if not doc:
            raise ValueError(f"tool {fn.__name__!r} needs a docstring; the model reads it")
        return Tool(name or fn.__name__, doc, params, fn, needs_approval)

    return wrap
