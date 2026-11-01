"""Wires settings, skills, memory and the agent loop together."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Any

import anthropic

from .agent import Agent, Approver, Listener
from .config import Settings
from .skills import SKILLS, Context, Skill, open_db
from .skills.memory import recent_memories
from .tools import ToolRegistry
from .trace import Tracer

DEFAULT_SKILLS = ("core", "memory", "jobs", "resume", "content", "research", "forms")

PERSONA = """\
You are Solfish, a personal agent working for one person: the user. You act through tools; \
use them instead of guessing about the user's data, and say so plainly when no tool can do \
what was asked. Reply in the language the user writes in, and keep replies short unless asked \
for detail."""


def build_system(skills: list[Skill], db: sqlite3.Connection) -> list[dict[str, Any]]:
    """System prompt: a stable, cached block first, then the per-session memory snapshot."""
    stable = PERSONA + "".join(f"\n\n# Skill: {s.name}\n{s.instructions}" for s in skills)
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}
    ]
    if any(s.name == "memory" for s in skills):
        rows = recent_memories(db)
        if rows:
            notes = "\n".join(f"- #{r['id']} {r['content']}" for r in rows)
            blocks.append({"type": "text", "text": f"What you remember (newest first):\n{notes}"})
    return blocks


def new_session_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def build_agent(
    settings: Settings | None = None,
    *,
    client: anthropic.Anthropic | None = None,
    skills: tuple[str, ...] = DEFAULT_SKILLS,
    approve: Approver | None = None,
    listeners: list[Listener] | None = None,
    trace: bool = True,
    session_id: str | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> tuple[Agent, Tracer]:
    """`session_id` names the trace file; `messages` restores a stored session's history."""
    settings = settings or Settings()
    ctx = Context(settings=settings, db=open_db(settings))

    unknown = set(skills) - SKILLS.keys()
    if unknown:
        raise ValueError(f"unknown skills: {', '.join(sorted(unknown))}")
    loaded = [SKILLS[name](ctx) for name in skills]
    registry = ToolRegistry([t for s in loaded for t in s.tools])

    session = session_id or new_session_id()
    tracer = Tracer(settings.traces_dir / f"{session}.jsonl" if trace else None)
    agent = Agent(
        client or anthropic.Anthropic(base_url=settings.base_url, api_key=settings.api_key),
        registry,
        build_system(loaded, ctx.db),
        settings,
        approve=approve,
        listeners=[tracer, *(listeners or [])],
    )
    agent.messages = list(messages or [])
    return agent, tracer
