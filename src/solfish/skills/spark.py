"""Conversations Solfish starts itself: it keeps a list of the user's interests, picks one, and
opens a new session with a short message about it.

    interests -> pick (weight x how long since the last spark) -> one model call writes the opener
              -> a seeded session: [user: "[spark] <topic>", assistant: <opener>]

The seed starts with a user message so the history stays valid and append-only (the user's reply
is simply the next turn). Interests are added by hand for now; mining them from chats, projects
and feeds comes later (see docs/design.md).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ..config import Settings
from ..tools import ToolError, tool
from .base import Context, Skill
from .memory import recent_memories

if TYPE_CHECKING:
    from ..session import SessionStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS interests (
    id INTEGER PRIMARY KEY,
    topic TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    weight REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'muted')),
    last_sparked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS sparks (
    id INTEGER PRIMARY KEY,
    interest_id INTEGER NOT NULL REFERENCES interests(id),
    session_id TEXT NOT NULL,
    opener TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
"""

# An interest sparked this long ago counts as fully fresh again.
FRESH_AFTER_H = 72

OPENER_PROMPT = """\
You are Solfish, the user's personal agent, and you are starting a conversation with them \
unprompted. Write the opening message about the topic below.

- At most three sentences, about one specific point, not the topic in general.
- End with one question that is easy and fun to answer.
- If the note says why the user cares, connect to that; use what you remember about them.
- No greeting, no lists, no "as an AI". Never invent facts, links or events.
- Write in the language the topic note is written in."""


class AddInterestArgs(BaseModel):
    topic: str = Field(description="Short name of the interest, e.g. 'MLX quantization'.")
    evidence: str = Field("", description="One line on why: what the user said or did that shows it.")


class NoArgs(BaseModel):
    pass


class InterestIdArgs(BaseModel):
    id: int = Field(description="Id of the interest, as shown by list_interests.")


def describe(db: sqlite3.Connection) -> str:
    db.executescript(SCHEMA)
    rows = db.execute(
        "SELECT * FROM interests WHERE status = 'active' ORDER BY weight DESC, id"
    ).fetchall()
    return "\n".join(
        f"#{r['id']} {r['topic']} (weight {r['weight']:g}"
        + (f", last sparked {r['last_sparked_at']}" if r["last_sparked_at"] else "") + ")"
        + (f": {r['evidence']}" if r["evidence"] else "")
        for r in rows
    ) or "No interests yet."


def pick(db: sqlite3.Connection, now: datetime) -> sqlite3.Row | None:
    """The active interest with the highest weight x freshness; never-sparked ones are fresh."""
    def score(r: sqlite3.Row) -> float:
        if r["last_sparked_at"] is None:
            return r["weight"]
        hours = (now - datetime.fromisoformat(r["last_sparked_at"])).total_seconds() / 3600
        return r["weight"] * min(1.0, max(0.0, hours) / FRESH_AFTER_H)

    rows = db.execute("SELECT * FROM interests WHERE status = 'active' ORDER BY id").fetchall()
    best = max(rows, key=score, default=None)
    return best if best is not None and score(best) > 0 else None


def write_opener(client: Any, settings: Settings, db: sqlite3.Connection, interest: sqlite3.Row) -> str:
    """One model call, no tools: the opening message for this interest."""
    system = OPENER_PROMPT
    notes = recent_memories(db)
    if notes:
        system += "\n\nWhat you remember about the user:\n" + "\n".join(f"- {r['content']}" for r in notes)
    note = interest["topic"] + (f"\nWhy: {interest['evidence']}" if interest["evidence"] else "")
    response = client.messages.create(
        model=settings.model,
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": f"Topic note:\n{note}"}],
    )
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise ToolError("The model returned an empty opener.")
    return text


def seed_message(interest: sqlite3.Row) -> str:
    """The user turn a sparked session starts with; it also becomes the session's title."""
    return f"[spark] {interest['topic']}" + (f"\n{interest['evidence']}" if interest["evidence"] else "")


def start(store: SessionStore, client: Any, settings: Settings, channel: str,
          now: datetime | None = None) -> tuple[str, str] | None:
    """Pick an interest, write an opener and open a session with it: (session id, opener).
    None when there is nothing to talk about."""
    now = now or datetime.now()
    db = store.db
    db.executescript(SCHEMA)
    interest = pick(db, now)
    if interest is None:
        return None
    from ..app import new_session_id  # app imports the skills package

    opener = write_opener(client, settings, db, interest)
    session_id = new_session_id()
    store.create(session_id, channel)
    store.sync(session_id, [
        {"role": "user", "content": seed_message(interest)},
        # Blocks, not a bare string: that's the shape the loop stores assistant turns in.
        {"role": "assistant", "content": [{"type": "text", "text": opener}]},
    ])
    stamp = now.isoformat(timespec="seconds")
    with db:
        db.execute("UPDATE interests SET last_sparked_at = ? WHERE id = ?", (stamp, interest["id"]))
        db.execute(
            "INSERT INTO sparks (interest_id, session_id, opener, sent_at) VALUES (?, ?, ?, ?)",
            (interest["id"], session_id, opener, stamp),
        )
    return session_id, opener


def load(ctx: Context) -> Skill:
    db = ctx.db
    db.executescript(SCHEMA)

    @tool(AddInterestArgs)
    def add_interest(args: AddInterestArgs) -> str:
        """Note a topic the user enjoys talking about. Solfish uses these to start conversations."""
        topic = " ".join(args.topic.split())
        if not topic:
            raise ToolError("The topic is empty.")
        row = db.execute(
            "SELECT id, status FROM interests WHERE lower(topic) = lower(?)", (topic,)
        ).fetchone()
        if row is not None:
            with db:
                db.execute("UPDATE interests SET weight = weight + 0.5, status = 'active' WHERE id = ?",
                           (row["id"],))
            return f"Interest #{row['id']} already existed; gave it more weight."
        with db:
            cur = db.execute("INSERT INTO interests (topic, evidence) VALUES (?, ?)",
                             (topic, args.evidence.strip()))
        return f"Saved interest #{cur.lastrowid}."

    @tool(NoArgs)
    def list_interests(_: NoArgs) -> str:
        """The user's active interests, strongest first."""
        return describe(db)

    @tool(InterestIdArgs, needs_approval=True)
    def mute_interest(args: InterestIdArgs) -> str:
        """Stop starting conversations about an interest (the user is tired of it)."""
        with db:
            cur = db.execute("UPDATE interests SET status = 'muted' WHERE id = ?", (args.id,))
        if cur.rowcount == 0:
            raise ToolError(f"No interest #{args.id}.")
        return f"Muted interest #{args.id}."

    return Skill(
        name="spark",
        instructions=(
            "You keep a list of the user's interests and use it to start conversations yourself. "
            "When the user shows lasting enthusiasm for a topic, or says they want to try "
            "something, save it with add_interest. If they say they're tired of a topic, mute it. "
            "A session that starts with '[spark] <topic>' was opened by you: your message after it "
            "was sent to the user unprompted, so carry on from it naturally."
        ),
        tools=[add_interest, list_interests, mute_interest],
    )
