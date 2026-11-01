"""The agent loop, written by hand on the Anthropic Messages API (served by DeepSeek by default).

    user message -> model -> (tool calls -> tool results -> model)* -> final answer

The loop is append-only: it never edits earlier turns, only appends the model's full response
(thinking blocks included) and the tool results. That keeps the prompt cache warm and keeps
thinking blocks valid across turns.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anthropic

from .config import Settings
from .tools import ToolRegistry, ToolResult
from .trace import cost_usd, usage_dict

Listener = Callable[[str, dict[str, Any]], None]
Approver = Callable[[str, dict[str, Any]], bool]

MAX_RESULT_CHARS = 20_000


@dataclass
class TurnResult:
    text: str
    steps: int
    # end_turn | refusal | max_tokens | step_limit | interrupted
    stop: str


class Agent:
    def __init__(
        self,
        client: anthropic.Anthropic,
        tools: ToolRegistry,
        system: list[dict[str, Any]],
        settings: Settings,
        *,
        approve: Approver | None = None,
        listeners: list[Listener] | None = None,
    ) -> None:
        self.client = client
        self.tools = tools
        self.system = system
        self.settings = settings
        self.approve = approve or (lambda name, args: False)
        self.listeners = listeners or []
        self.messages: list[dict[str, Any]] = []
        # Set from another thread to stop the turn before its next batch of tool calls.
        self.cancel = threading.Event()

    def emit(self, kind: str, **data: Any) -> None:
        for listen in self.listeners:
            listen(kind, data)

    def run(self, user_input: str) -> TurnResult:
        """Handle one user message, calling tools until the model gives a final answer."""
        self.cancel.clear()
        self.emit("user_message", text=user_input)
        result = self._run(user_input)
        self.emit("turn_end", text=result.text, steps=result.steps, stop=result.stop)
        return result

    def _run(self, user_input: str) -> TurnResult:
        self._close_dangling_tool_calls()
        self.messages.append({"role": "user", "content": user_input})
        for step in range(1, self.settings.max_steps + 1):
            response = self._call_model(step)
            self.messages.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if block.type == "thinking" and block.thinking:
                    self.emit("thinking", step=step, text=block.thinking)
                elif block.type == "fallback":
                    self.emit("fallback", step=step, to=block.to.model)

            match response.stop_reason:
                case "tool_use":
                    if self.cancel.is_set():
                        # The unanswered calls are closed at the start of the next turn.
                        return TurnResult(_text(response) or "(Stopped.)", step, "interrupted")
                    calls = [b for b in response.content if b.type == "tool_use"]
                    # All results go back in one user message, in call order.
                    results = [self._run_tool(step, c) for c in calls]
                    self.messages.append({"role": "user", "content": results})
                case "pause_turn":
                    continue  # server paused a long turn; re-sending the history resumes it
                case "refusal":
                    return TurnResult(_text(response) or "(Declined to answer.)", step, "refusal")
                case "max_tokens":
                    text = _text(response) + "\n\n[Response hit the max_tokens limit.]"
                    return TurnResult(text, step, "max_tokens")
                case _:
                    return TurnResult(_text(response), step, "end_turn")

        return TurnResult(
            f"Stopped after {self.settings.max_steps} steps without finishing. "
            "Ask me to continue, or narrow the task.",
            self.settings.max_steps,
            "step_limit",
        )

    def _close_dangling_tool_calls(self) -> None:
        """If the last turn was interrupted mid-tool-call, answer those calls so the history
        stays valid (every tool_use needs a tool_result) without rewriting anything."""
        if not self.messages or self.messages[-1]["role"] != "assistant":
            return
        pending = [b for b in self.messages[-1]["content"] if _field(b, "type") == "tool_use"]
        if pending:
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": _field(b, "id"),
                 "content": "Error: Interrupted by the user.", "is_error": True}
                for b in pending
            ]})

    def _call_model(self, step: int) -> Any:
        s = self.settings
        kwargs: dict[str, Any] = dict(
            model=s.model,
            max_tokens=s.max_tokens,
            system=self.system,
            tools=self.tools.definitions(),
            messages=self.messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": s.effort},
            # Auto-cache the whole prefix: each loop step re-reads the previous step from cache.
            cache_control={"type": "ephemeral"},
        )
        started = time.monotonic()
        if s.fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
            response = self.client.beta.messages.create(**kwargs)
        else:
            response = self.client.messages.create(**kwargs)
        usage = usage_dict(response.usage)
        self.emit(
            "llm_call",
            step=step,
            model=response.model,
            stop_reason=response.stop_reason,
            usage=usage,
            cost_usd=cost_usd(s.model, usage),
            latency_s=round(time.monotonic() - started, 2),
        )
        return response

    def _run_tool(self, step: int, call: Any) -> dict[str, Any]:
        args = call.input if isinstance(call.input, dict) else {}
        self.emit("tool_call", step=step, id=call.id, name=call.name, input=args)
        started = time.monotonic()

        t = self.tools.get(call.name)
        if t is not None and t.needs_approval and not self.approve(call.name, args):
            result = ToolResult("The user declined this action. Ask them how to proceed.", True)
        else:
            result = self.tools.execute(call.name, args)

        content = result.content
        if len(content) > MAX_RESULT_CHARS:
            content = content[:MAX_RESULT_CHARS] + f"\n[truncated from {len(result.content)} chars]"
        self.emit(
            "tool_result",
            step=step,
            id=call.id,
            name=call.name,
            is_error=result.is_error,
            output=content,
            duration_s=round(time.monotonic() - started, 3),
        )
        # DeepSeek ignores is_error, so the content itself has to say it failed.
        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": f"Error: {content}" if result.is_error else content,
            "is_error": result.is_error,
        }


def _field(block: Any, name: str) -> Any:
    """Blocks are SDK objects in a live session and plain dicts in one restored from disk."""
    return block[name] if isinstance(block, dict) else getattr(block, name)


def _text(response: Any) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


def format_args(args: dict[str, Any]) -> str:
    return json.dumps(args, ensure_ascii=False)
