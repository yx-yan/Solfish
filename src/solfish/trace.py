"""Observability: every model call and tool call is appended to a JSONL trace.

A trace answers "what did the agent do, and what did it cost?" for any session, and the eval
runner uses the same totals to report cost per task.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# USD per million tokens: (input, output, cache read, 5-minute cache write).
# DeepSeek: peak-hour prices (off-peak is half); its caching is automatic, so no write cost.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "deepseek-flash": (0.30, 1.20, 0.006, 0.0),
    "deepseek-v4-pro": (1.32, 3.96, 0.044, 0.0),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
}


def usage_dict(usage: Any) -> dict[str, int]:
    return {
        "input": usage.input_tokens or 0,
        "output": usage.output_tokens or 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", None) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", None) or 0,
    }


def cost_usd(model: str, u: dict[str, int]) -> float | None:
    p = PRICES.get(model)
    if p is None:
        return None
    return (
        u["input"] * p[0] + u["output"] * p[1] + u["cache_read"] * p[2] + u["cache_write"] * p[3]
    ) / 1_000_000


@dataclass
class Totals:
    llm_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tokens: dict[str, int] = field(
        default_factory=lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    )
    cost_usd: float = 0.0


class Tracer:
    """An agent event listener that writes JSONL and keeps running totals."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.totals = Totals()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, kind: str, data: dict[str, Any]) -> None:
        t = self.totals
        if kind == "llm_call":
            t.llm_calls += 1
            for k, v in data["usage"].items():
                t.tokens[k] += v
            t.cost_usd += data.get("cost_usd") or 0.0
        elif kind == "tool_result":
            t.tool_calls += 1
            t.tool_errors += data["is_error"]
        if self.path:
            record = {"ts": round(time.time(), 3), "kind": kind, **data}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
