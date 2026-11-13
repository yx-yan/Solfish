"""Sessions: conversations that outlive the process and can be reached from more than one channel.

    channel (CLI, web, Telegram, ...) -> SessionManager -> Agent, one per session, on a worker thread
                                      <- events, broadcast to every subscriber

The history is stored the way the loop builds it, append-only: after each turn only the new
messages are written. A restored session hands the same blocks back to the API (as dicts), so the
model sees exactly what it saw before and the provider's prefix cache still matches.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import anthropic

from .agent import Agent
from .app import DEFAULT_SKILLS, build_agent, new_client, new_session_id
from .config import Settings
from .skills import open_db
from .trace import Tracer

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_messages (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
"""

# session id, event kind, event data
Subscriber = Callable[[str, str, dict[str, Any]], None]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def to_jsonable(content: Any) -> Any:
    """A message's content as plain JSON: SDK blocks become the dicts the API accepts back."""
    if isinstance(content, str):
        return content
    return [b if isinstance(b, dict) else b.model_dump(mode="json", exclude_none=True) for b in content]


class SessionStore:
    """Sessions and their messages in SQLite. Safe to share between threads."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._lock = threading.Lock()
        db.executescript(SCHEMA)

    def create(self, session_id: str, channel: str) -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO sessions (id, channel, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, channel, _now(), _now()),
            )

    def get(self, session_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()

    def load(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT role, content FROM session_messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]

    def sync(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Write the messages not stored yet. The history only grows, so that's the tail."""
        with self._lock, self.db:
            (stored,) = self.db.execute(
                "SELECT COUNT(*) FROM session_messages WHERE session_id = ?", (session_id,)
            ).fetchone()
            new = messages[stored:]
            if not new:
                return
            self.db.executemany(
                "INSERT INTO session_messages (session_id, seq, role, content) VALUES (?, ?, ?, ?)",
                [
                    (session_id, stored + i, m["role"],
                     json.dumps(to_jsonable(m["content"]), ensure_ascii=False))
                    for i, m in enumerate(new)
                ],
            )
            title = next((m["content"] for m in messages
                          if m["role"] == "user" and isinstance(m["content"], str)), "")
            self.db.execute(
                "UPDATE sessions SET updated_at = ?, title = CASE WHEN title = '' THEN ? ELSE title END "
                "WHERE id = ?",
                (_now(), " ".join(title.split())[:60], session_id),
            )


@dataclass
class Approval:
    id: str
    session_id: str
    name: str
    input: dict[str, Any]
    allow: bool = False
    decided: threading.Event = field(default_factory=threading.Event, repr=False)


class Approvals:
    """Approval requests any channel can answer. The agent's thread waits on the answer."""

    def __init__(self, timeout_s: float = 600.0) -> None:
        self.timeout_s = timeout_s
        self._pending: dict[str, Approval] = {}
        self._lock = threading.Lock()

    def request(self, session_id: str, name: str, args: dict[str, Any],
                notify: Callable[..., None]) -> bool:
        a = Approval(uuid.uuid4().hex[:8], session_id, name, args)
        with self._lock:
            self._pending[a.id] = a
        notify("approval_request", id=a.id, name=name, input=args)
        a.decided.wait(self.timeout_s)  # no answer in time counts as no
        with self._lock:
            del self._pending[a.id]
            allow = a.allow
        notify("approval_resolved", id=a.id, allow=allow)
        return allow

    def resolve(self, approval_id: str, allow: bool) -> bool:
        """Answer a pending request. False if it's unknown or already answered."""
        with self._lock:
            a = self._pending.get(approval_id)
            if a is None or a.decided.is_set():
                return False
            a.allow = allow
            a.decided.set()
            return True

    def pending(self, session_id: str | None = None) -> list[Approval]:
        with self._lock:
            return [a for a in self._pending.values()
                    if session_id is None or a.session_id == session_id]


class Busy(Exception):
    """The session is already running a turn."""


class SessionManager:
    """Owns the live agents, runs their turns off the caller's thread and fans out the events."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: anthropic.Anthropic | None = None,
        skills: tuple[str, ...] = DEFAULT_SKILLS,
        approval_timeout_s: float = 600.0,
        trace: bool = True,
    ) -> None:
        self.settings = settings
        self.client = client
        self.skills = skills
        self.trace = trace
        self.store = SessionStore(open_db(settings))
        self.approvals = Approvals(approval_timeout_s)
        self._agents: dict[str, tuple[Agent, Tracer]] = {}
        # Tools a session may run without asking, e.g. test builds in a résumé review. In memory
        # only: after a restart those tools ask again, which errs on the safe side.
        self._preapproved: dict[str, frozenset[str]] = {}
        self._running: dict[str, threading.Thread] = {}
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        """Receive every session's events, on the agent's thread. Returns an unsubscribe."""
        self._subscribers.append(fn)
        return lambda: self._subscribers.remove(fn)

    def _broadcast(self, session_id: str, kind: str, data: dict[str, Any]) -> None:
        for fn in list(self._subscribers):
            fn(session_id, kind, data)

    def create(self, channel: str, preapproved: frozenset[str] = frozenset()) -> str:
        session_id = new_session_id()
        self.store.create(session_id, channel)
        if preapproved:
            self._preapproved[session_id] = preapproved
        return session_id

    def model_client(self) -> anthropic.Anthropic:
        """The client for one-off model calls outside a session's loop."""
        if self.client is None:
            self.client = new_client(self.settings)
        return self.client

    def _agent(self, session_id: str) -> tuple[Agent, Tracer]:
        if session_id not in self._agents:
            if self.store.get(session_id) is None:
                raise KeyError(session_id)

            def notify(kind: str, **data: Any) -> None:
                self._broadcast(session_id, kind, data)

            self._agents[session_id] = build_agent(
                self.settings,
                client=self.client,
                skills=self.skills,
                session_id=session_id,
                messages=self.store.load(session_id),
                approve=lambda name, args: (name in self._preapproved.get(session_id, ())
                                            or self.approvals.request(session_id, name, args, notify)),
                listeners=[lambda kind, data: self._broadcast(session_id, kind, data)],
                trace=self.trace,
            )
        return self._agents[session_id]

    def history(self, session_id: str) -> list[dict[str, Any]]:
        """The full history as JSON, including a turn still in progress. KeyError if unknown."""
        if session_id in self._agents:
            messages = list(self._agents[session_id][0].messages)
            return [{"role": m["role"], "content": to_jsonable(m["content"])} for m in messages]
        if self.store.get(session_id) is None:
            raise KeyError(session_id)
        return self.store.load(session_id)

    def totals(self, session_id: str):
        return self._agent(session_id)[1].totals

    def busy(self, session_id: str) -> bool:
        return session_id in self._running

    def send(self, session_id: str, text: str) -> threading.Thread:
        """Start a turn in the background. Raises Busy or KeyError (unknown session)."""
        with self._lock:
            if self.busy(session_id):
                raise Busy(session_id)
            agent, _ = self._agent(session_id)
            t = threading.Thread(target=self._turn, args=(session_id, agent, text), daemon=True)
            self._running[session_id] = t
        t.start()
        return t

    def _turn(self, session_id: str, agent: Agent, text: str) -> None:
        try:
            agent.run(text)
        except Exception as e:  # the thread has no caller to raise to, so channels hear about it
            self._broadcast(session_id, "error", {"message": _describe(e)})
        finally:
            self.store.sync(session_id, agent.messages)
            with self._lock:
                del self._running[session_id]

    def cancel(self, session_id: str) -> None:
        """Stop at the next tool boundary, and decline anything waiting for approval."""
        if session_id in self._agents:
            self._agents[session_id][0].cancel.set()
        for a in self.approvals.pending(session_id):
            self.approvals.resolve(a.id, False)


def _describe(e: Exception) -> str:
    if isinstance(e, anthropic.AuthenticationError):
        return "No valid credentials. Set DEEPSEEK_API_KEY."
    if isinstance(e, anthropic.APIConnectionError):
        return "Network error talking to the model API."
    if isinstance(e, anthropic.APIStatusError):
        return f"API error {e.status_code}: {e.message}"
    return f"{type(e).__name__}: {e}"
