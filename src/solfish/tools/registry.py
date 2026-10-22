"""Holds the tools available to an agent and executes calls safely.

A tool call never raises out of here: bad arguments, unknown tools and crashes all come back
as error results, so the model sees what went wrong and can try again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .base import Tool, ToolError


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, t: Tool) -> None:
        if t.name in self._tools:
            raise ValueError(f"duplicate tool name: {t.name}")
        self._tools[t.name] = t

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __len__(self) -> int:
        return len(self._tools)

    def definitions(self) -> list[dict[str, Any]]:
        # Sorted so the tools block is byte-identical across requests (prompt-cache friendly).
        return [self._tools[n].definition() for n in sorted(self._tools)]

    def execute(self, name: str, raw_input: dict[str, Any]) -> ToolResult:
        t = self._tools.get(name)
        if t is None:
            return ToolResult(f"Unknown tool {name!r}. Available: {', '.join(sorted(self._tools))}", True)
        try:
            args = t.params.model_validate(raw_input)
        except ValidationError as e:
            return ToolResult(f"Invalid arguments for {name}: {e}", True)
        try:
            return ToolResult(t.fn(args))
        except ToolError as e:
            return ToolResult(str(e), True)
        except Exception as e:  # a bug in a tool must not kill the whole agent run
            return ToolResult(f"{name} crashed: {type(e).__name__}: {e}", True)
