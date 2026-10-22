"""Long-term memory: short notes that persist across sessions.

The most recent notes are shown to the model at the start of each session; older ones are
reachable through `recall`. Keyword search is deliberately simple for now (see docs/design.md).
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, Field

from ..tools import ToolError, tool
from .base import Context, Skill

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


class RememberArgs(BaseModel):
    content: str = Field(description="One self-contained fact, written so it makes sense later.")


class RecallArgs(BaseModel):
    query: str = Field(description="Keywords to search for. Notes matching any keyword are returned.")


class ForgetArgs(BaseModel):
    id: int = Field(description="Id of the memory to delete, as shown by recall.")


def recent_memories(db: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    db.executescript(SCHEMA)
    return db.execute(
        "SELECT id, content, created_at FROM memories ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def load(ctx: Context) -> Skill:
    db = ctx.db
    db.executescript(SCHEMA)

    @tool(RememberArgs)
    def remember(args: RememberArgs) -> str:
        """Save a durable fact about the user or their world (preferences, goals, people, decisions).
        Don't save things that only matter to the current conversation."""
        cur = db.execute("INSERT INTO memories (content) VALUES (?)", (args.content.strip(),))
        db.commit()
        return f"Saved memory #{cur.lastrowid}."

    @tool(RecallArgs)
    def recall(args: RecallArgs) -> str:
        """Search long-term memory by keyword."""
        words = [w for w in args.query.split() if w]
        if not words:
            raise ToolError("Give at least one keyword.")
        where = " OR ".join("content LIKE ?" for _ in words)
        rows = db.execute(
            f"SELECT id, content, created_at FROM memories WHERE {where} ORDER BY id DESC LIMIT 20",
            [f"%{w}%" for w in words],
        ).fetchall()
        return "\n".join(f"#{r['id']} ({r['created_at']}): {r['content']}" for r in rows) or "No matches."

    @tool(ForgetArgs, needs_approval=True)
    def forget(args: ForgetArgs) -> str:
        """Delete a memory that is wrong or outdated."""
        cur = db.execute("DELETE FROM memories WHERE id = ?", (args.id,))
        db.commit()
        if cur.rowcount == 0:
            raise ToolError(f"No memory #{args.id}.")
        return f"Deleted memory #{args.id}."

    return Skill(
        name="memory",
        instructions=(
            "You have long-term memory. When the user tells you something worth keeping, save it "
            "with remember. If a remembered fact turns out to be wrong, forget it and save the "
            "correction."
        ),
        tools=[remember, recall, forget],
    )
