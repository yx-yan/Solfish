"""Core skill: clock and a sandboxed workspace for reading and writing files."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from ..tools import ToolError, tool
from .base import Context, Skill

MAX_READ_CHARS = 50_000


class NoArgs(BaseModel):
    pass


class PathArg(BaseModel):
    path: str = Field(description="Path relative to the workspace root, e.g. 'notes/todo.md'.")


class WriteArgs(PathArg):
    content: str = Field(description="Full file content. Overwrites the file if it exists.")


def resolve_in(root: Path, rel: str) -> Path:
    """Resolve a user-supplied path, refusing anything that escapes the workspace."""
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ToolError(f"Path {rel!r} is outside the workspace.")
    return target


def load(ctx: Context) -> Skill:
    root = ctx.settings.workspace

    @tool(NoArgs)
    def current_time(_: NoArgs) -> str:
        """Get the current local date and time. Use it for anything date-relative."""
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z (%A)")

    @tool(PathArg)
    def list_files(args: PathArg) -> str:
        """List files under a workspace directory. Use '.' for the workspace root."""
        d = resolve_in(root, args.path)
        if not d.is_dir():
            raise ToolError(f"{args.path!r} is not a directory.")
        entries = sorted(str(p.relative_to(root)) + ("/" if p.is_dir() else "") for p in d.iterdir())
        return "\n".join(entries) or "(empty)"

    @tool(PathArg)
    def read_file(args: PathArg) -> str:
        """Read a UTF-8 text file from the workspace."""
        f = resolve_in(root, args.path)
        if not f.is_file():
            raise ToolError(f"No file at {args.path!r}.")
        text = f.read_text(encoding="utf-8")
        if len(text) > MAX_READ_CHARS:
            return text[:MAX_READ_CHARS] + f"\n\n[truncated: file has {len(text)} characters]"
        return text

    @tool(WriteArgs, needs_approval=True)
    def write_file(args: WriteArgs) -> str:
        """Create or overwrite a text file in the workspace."""
        f = resolve_in(root, args.path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(args.content, encoding="utf-8")
        return f"Wrote {len(args.content)} characters to {args.path}."

    return Skill(
        name="core",
        instructions=(
            "You have a private workspace directory for files. Paths are relative to it. "
            "You cannot see the clock unless you call current_time."
        ),
        tools=[current_time, list_files, read_file, write_file],
    )
