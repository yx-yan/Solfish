"""Content skill: draft, schedule and publish posts for the user's channels.

Posts move through draft -> scheduled -> published (or failed). Publishing goes through a
`Publisher`, one per platform. Most Chinese platforms (小红书, 抖音, 公众号, B站) offer no
posting API to individuals, so their default publisher is the outbox: it writes a ready-to-post
package (text + media) to the workspace for the user to upload. A platform with an API gets a
real publisher by adding one function to PUBLISHERS.

Approval happens when a post is scheduled or published by hand. `solfish publish-due` then
publishes whatever is due without the model in the loop, so it can run from cron.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..config import Settings
from ..tools import ToolError, tool
from .base import Context, Skill, slugify
from .core import resolve_in

Platform = Literal["xiaohongshu", "douyin", "bilibili", "wechat", "weibo", "x", "linkedin", "blog"]
Status = Literal["draft", "scheduled", "published", "failed"]

# (title limit, body limit) in characters, checked when a draft is saved.
LIMITS: dict[str, tuple[int | None, int | None]] = {
    "xiaohongshu": (20, 1000),
    "x": (None, 280),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    media TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    scheduled_at TEXT,
    published_at TEXT,
    location TEXT,
    error TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

# Takes a post row, publishes it, returns where it went (a URL or a path). Raises on failure.
Publisher = Callable[[Settings, sqlite3.Row], str]


def outbox(settings: Settings, post: sqlite3.Row) -> str:
    """Write the post and its media to outbox/<platform>/<id>-<title>/ for manual upload."""
    name = "-".join(filter(None, [str(post["id"]), slugify(post["title"])]))
    d = settings.workspace / "outbox" / post["platform"] / name
    d.mkdir(parents=True, exist_ok=True)
    text = f"# {post['title']}\n\n{post['body']}\n" if post["title"] else f"{post['body']}\n"
    (d / "post.md").write_text(text, encoding="utf-8")
    for rel in filter(None, post["media"].split("\n")):
        shutil.copy(resolve_in(settings.workspace, rel), d)
    return str(d.relative_to(settings.workspace))


PUBLISHERS: dict[str, Publisher] = {}  # platform -> publisher; anything missing uses the outbox


def publish(settings: Settings, db: sqlite3.Connection, post_id: int) -> str:
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if post is None:
        raise ToolError(f"No post #{post_id}. Use list_posts to find the id.")
    if post["status"] == "published":
        raise ToolError(f"Post #{post_id} is already published at {post['location']}.")
    try:
        location = PUBLISHERS.get(post["platform"], outbox)(settings, post)
    except Exception as e:
        db.execute(
            "UPDATE posts SET status = 'failed', error = ?, updated_at = datetime('now', 'localtime') "
            "WHERE id = ?", (f"{type(e).__name__}: {e}", post_id),
        )
        db.commit()
        raise ToolError(f"Publishing #{post_id} failed: {e}") from e
    db.execute(
        "UPDATE posts SET status = 'published', location = ?, error = NULL, "
        "published_at = datetime('now', 'localtime'), updated_at = datetime('now', 'localtime') "
        "WHERE id = ?", (location, post_id),
    )
    db.commit()
    return location


def publish_due(settings: Settings, db: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    """Publish every scheduled post whose time has come. Returns one line per post."""
    db.executescript(SCHEMA)
    now = now or datetime.now()
    due = db.execute(
        "SELECT id FROM posts WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at",
        (now.isoformat(timespec="minutes"),),
    ).fetchall()
    lines = []
    for r in due:
        try:
            lines.append(f"#{r['id']} published: {publish(settings, db, r['id'])}")
        except ToolError as e:
            lines.append(f"#{r['id']} {e}")
    return lines


def check_limits(platform: str, title: str, body: str) -> None:
    title_max, body_max = LIMITS.get(platform, (None, None))
    if title_max and len(title) > title_max:
        raise ToolError(f"{platform} titles are at most {title_max} characters; this one has {len(title)}.")
    if body_max and len(body) > body_max:
        raise ToolError(f"{platform} posts are at most {body_max} characters; this one has {len(body)}.")


class DraftArgs(BaseModel):
    platform: Platform
    title: str = Field(default="", description="Post title, where the platform has one.")
    body: str = Field(description="Post text, written in the platform's style.")
    media: list[str] = Field(
        default_factory=list, description="Workspace paths of images or videos to attach."
    )


class EditArgs(BaseModel):
    id: int
    title: str | None = None
    body: str | None = None
    media: list[str] | None = None


class ListArgs(BaseModel):
    status: Status | Literal["all"] = "all"
    platform: Platform | None = None


class ScheduleArgs(BaseModel):
    id: int
    at: datetime = Field(description="Local time to publish, e.g. 2026-11-01T20:00.")


class IdArg(BaseModel):
    id: int


def load(ctx: Context) -> Skill:
    settings, db = ctx.settings, ctx.db
    db.executescript(SCHEMA)

    def checked_media(paths: list[str]) -> str:
        for rel in paths:
            if not resolve_in(settings.workspace, rel).is_file():
                raise ToolError(f"No media file at {rel!r} in the workspace.")
        return "\n".join(paths)

    @tool(DraftArgs)
    def draft_post(args: DraftArgs) -> str:
        """Save a new post draft for one platform. Draft once per platform when cross-posting."""
        check_limits(args.platform, args.title, args.body)
        cur = db.execute(
            "INSERT INTO posts (platform, title, body, media) VALUES (?, ?, ?, ?)",
            (args.platform, args.title, args.body, checked_media(args.media)),
        )
        db.commit()
        return f"Saved draft #{cur.lastrowid} for {args.platform}."

    @tool(EditArgs)
    def edit_post(args: EditArgs) -> str:
        """Change a draft or scheduled post's title, text or media."""
        post = db.execute("SELECT * FROM posts WHERE id = ?", (args.id,)).fetchone()
        if post is None:
            raise ToolError(f"No post #{args.id}.")
        if post["status"] == "published":
            raise ToolError(f"Post #{args.id} is already published; edit it on the platform.")
        title = post["title"] if args.title is None else args.title
        body = post["body"] if args.body is None else args.body
        check_limits(post["platform"], title, body)
        media = post["media"] if args.media is None else checked_media(args.media)
        db.execute(
            "UPDATE posts SET title = ?, body = ?, media = ?, updated_at = datetime('now', 'localtime') "
            "WHERE id = ?", (title, body, media, args.id),
        )
        db.commit()
        return f"Updated post #{args.id}."

    @tool(ListArgs)
    def list_posts(args: ListArgs) -> str:
        """List posts with their status and schedule, newest first."""
        where, params = [], []
        if args.status != "all":
            where.append("status = ?")
            params.append(args.status)
        if args.platform:
            where.append("platform = ?")
            params.append(args.platform)
        sql = "SELECT * FROM posts" + (f" WHERE {' AND '.join(where)}" if where else "")
        rows = db.execute(sql + " ORDER BY id DESC LIMIT 50", params).fetchall()
        lines = []
        for r in rows:
            line = f"#{r['id']} {r['platform']} | {r['status']} | {r['title'] or r['body'][:30]}"
            if r["scheduled_at"] and r["status"] == "scheduled":
                line += f" | at {r['scheduled_at']}"
            if r["location"]:
                line += f" | {r['location']}"
            if r["error"]:
                line += f" | error: {r['error']}"
            lines.append(line)
        return "\n".join(lines) or "No posts match."

    @tool(ScheduleArgs, needs_approval=True)
    def schedule_post(args: ScheduleArgs) -> str:
        """Schedule a draft to publish automatically at a given local time."""
        cur = db.execute(
            "UPDATE posts SET status = 'scheduled', scheduled_at = ?, "
            "updated_at = datetime('now', 'localtime') WHERE id = ? AND status IN ('draft', 'failed')",
            (args.at.isoformat(timespec="minutes"), args.id),
        )
        db.commit()
        if cur.rowcount == 0:
            raise ToolError(f"Post #{args.id} doesn't exist or isn't a draft.")
        return f"Post #{args.id} will publish at {args.at:%Y-%m-%d %H:%M}."

    @tool(IdArg, needs_approval=True)
    def publish_post(args: IdArg) -> str:
        """Publish a post now."""
        return f"Published #{args.id}: {publish(settings, db, args.id)}"

    return Skill(
        name="content",
        instructions=(
            "You run the user's social media: write posts, keep drafts, schedule and publish. "
            "Write natively for each platform rather than pasting one text everywhere: 小红书 "
            "wants a short hooky title, emoji-light paragraphs and hashtags at the end; X is "
            "terse; 公众号 and blog posts can be long. Show the user a draft before scheduling "
            "it. Platforms without an API publish to the workspace outbox; tell the user the "
            "folder to upload from."
        ),
        tools=[draft_post, edit_post, list_posts, schedule_post, publish_post],
    )
