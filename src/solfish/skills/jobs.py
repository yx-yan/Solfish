"""Job-hunting skill: track applications through the pipeline.

The first real use case for Solfish. Further tools (tailoring the LaTeX résumé to a JD,
interview prep) are on the roadmap in README.md.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from ..tools import ToolError, tool
from .base import Context, Skill

Status = Literal["saved", "applied", "oa", "interview", "offer", "rejected", "withdrawn"]
ACTIVE: tuple[str, ...] = ("saved", "applied", "oa", "interview", "offer")

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    url TEXT,
    notes TEXT NOT NULL DEFAULT '',
    applied_on TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


class AddArgs(BaseModel):
    company: str
    role: str
    status: Status = "saved"
    url: str | None = Field(default=None, description="Link to the job posting.")
    notes: str = ""
    applied_on: date | None = Field(
        default=None, description="Date the application was sent (YYYY-MM-DD). Omit if not sent."
    )


class UpdateArgs(BaseModel):
    id: int
    status: Status | None = None
    note: str | None = Field(default=None, description="Appended to the existing notes, dated.")


class ListArgs(BaseModel):
    status: Status | Literal["active", "all"] = Field(
        default="active", description="'active' means not rejected or withdrawn."
    )


def load(ctx: Context) -> Skill:
    db = ctx.db
    db.executescript(SCHEMA)

    @tool(AddArgs)
    def add_application(args: AddArgs) -> str:
        """Track a new job application or a job the user wants to apply to."""
        applied_on = args.applied_on or (date.today() if args.status != "saved" else None)
        cur = db.execute(
            "INSERT INTO applications (company, role, status, url, notes, applied_on) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (args.company, args.role, args.status, args.url, args.notes,
             applied_on.isoformat() if applied_on else None),
        )
        db.commit()
        return f"Tracked #{cur.lastrowid}: {args.role} at {args.company} ({args.status})."

    @tool(UpdateArgs)
    def update_application(args: UpdateArgs) -> str:
        """Change an application's status and/or add a note (e.g. interview scheduled)."""
        row = db.execute("SELECT * FROM applications WHERE id = ?", (args.id,)).fetchone()
        if row is None:
            raise ToolError(f"No application #{args.id}. Use list_applications to find the id.")
        if args.status is None and args.note is None:
            raise ToolError("Nothing to update: give a status, a note, or both.")
        status = args.status or row["status"]
        notes = row["notes"]
        if args.note:
            notes = f"{notes}\n[{date.today()}] {args.note}".strip()
        applied_on = row["applied_on"]
        if applied_on is None and status not in ("saved", "withdrawn"):
            applied_on = date.today().isoformat()
        db.execute(
            "UPDATE applications SET status = ?, notes = ?, applied_on = ?, "
            "updated_at = datetime('now', 'localtime') WHERE id = ?",
            (status, notes, applied_on, args.id),
        )
        db.commit()
        return f"Updated #{args.id} ({row['company']}): status {row['status']} -> {status}."

    @tool(ListArgs)
    def list_applications(args: ListArgs) -> str:
        """List tracked applications, newest activity first."""
        if args.status == "all":
            rows = db.execute("SELECT * FROM applications ORDER BY updated_at DESC").fetchall()
        elif args.status == "active":
            marks = ",".join("?" * len(ACTIVE))
            rows = db.execute(
                f"SELECT * FROM applications WHERE status IN ({marks}) ORDER BY updated_at DESC",
                ACTIVE,
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM applications WHERE status = ? ORDER BY updated_at DESC",
                (args.status,),
            ).fetchall()
        if not rows:
            return "No applications match."
        lines = []
        for r in rows:
            line = f"#{r['id']} {r['company']} | {r['role']} | {r['status']}"
            if r["applied_on"]:
                line += f" | applied {r['applied_on']}"
            if r["notes"]:
                line += f"\n    notes: {r['notes'].replace(chr(10), ' / ')}"
            lines.append(line)
        return "\n".join(lines)

    return Skill(
        name="jobs",
        instructions=(
            "You help the user run their job search. Keep the application tracker accurate: "
            "before adding an application, check whether it is already tracked."
        ),
        tools=[add_application, update_application, list_applications],
    )
