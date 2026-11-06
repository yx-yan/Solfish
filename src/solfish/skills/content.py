"""Content skill: draft, schedule and publish posts for the user's channels.

Posts move through draft -> scheduled -> publishing -> published (or failed). Publishing goes
through a `Publisher`, one per platform. Most Chinese platforms (小红书, 抖音, 公众号, B站) offer
no posting API to individuals, so their default publisher is the outbox: it writes a ready-to-post
package (text + media) to the workspace and the post waits in `handoff` until the user has
pressed publish themselves. A platform with an API gets a real publisher by adding one function
to PUBLISHERS.

Approval happens when a post is scheduled or published by hand, and shows the post as stored.
Editing a scheduled post sends it back to draft, so what goes out is what was approved.
`solfish publish-due` then publishes whatever is due without the model in the loop, so it can run
from cron.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..config import Settings
from ..tools import ToolError, tool
from .base import Context, Skill, slugify
from .core import resolve_in
from .publishers import blog, linkedin, x, xiaohongshu, zhihu
from .publishers.base import Delivery, MaybePublished

Platform = Literal[
    "xiaohongshu", "douyin", "bilibili", "wechat", "weibo", "x", "linkedin", "blog", "zhihu"
]
Status = Literal["draft", "scheduled", "publishing", "handoff", "published", "failed"]

# (title limit, body limit) in characters, checked when a draft is saved.
LIMITS: dict[str, tuple[int | None, int | None]] = {
    "xiaohongshu": (20, 1000),
    "x": (None, 280),
    "linkedin": (None, 3000),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    media TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    cover TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    scheduled_at TEXT,
    published_at TEXT,
    location TEXT,
    error TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

# A post left in `publishing` this long is not a run in progress: it crashed mid-way.
STUCK_AFTER = "-10 minutes"


# Takes a post row and publishes it. Raises on failure. Gets the db to keep things like tokens.
Publisher = Callable[[Settings, sqlite3.Connection, sqlite3.Row], Delivery]


def migrate(db: sqlite3.Connection) -> None:
    """Create the table, and add columns a database from an older version doesn't have yet."""
    db.executescript(SCHEMA)
    have = {r["name"] for r in db.execute("PRAGMA table_info(posts)")}
    for column in ("tags", "cover"):
        if column not in have:
            db.execute(f"ALTER TABLE posts ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    db.commit()


def outbox(settings: Settings, db: sqlite3.Connection, post: sqlite3.Row) -> Delivery:
    """Write the post and its media to outbox/<platform>/<id>-<title>/ for manual upload."""
    name = "-".join(filter(None, [str(post["id"]), slugify(post["title"])]))
    d = settings.workspace / "outbox" / post["platform"] / name
    d.mkdir(parents=True, exist_ok=True)
    text = f"# {post['title']}\n\n{post['body']}\n" if post["title"] else f"{post['body']}\n"
    if post["tags"]:
        text += "\n" + " ".join(f"#{t}" for t in post["tags"].split("\n")) + "\n"
    (d / "post.md").write_text(text, encoding="utf-8")
    for rel in filter(None, [post["cover"], *post["media"].split("\n")]):
        shutil.copy(resolve_in(settings.workspace, rel), d)
    return Delivery(str(d.relative_to(settings.workspace)), final=False)


def outbox_for_browser(settings: Settings, db: sqlite3.Connection, post: sqlite3.Row) -> Delivery:
    """The outbox, plus the command that fills the platform's page from it."""
    module = BROWSER[post["platform"]]
    module.check(settings, post)
    step = f"run `solfish handoff {post['id']}`, {module.NEXT_STEP}"
    return replace(outbox(settings, db, post), next_step=step)


# Modules with check() and fill(page, settings, post), for `solfish handoff`. They never press
# publish.
BROWSER = {"xiaohongshu": xiaohongshu, "zhihu": zhihu}
# Anything missing uses the outbox.
PUBLISHERS: dict[str, Publisher] = {
    "x": x.publish,
    "linkedin": linkedin.publish,
    "blog": blog.publish,
    "xiaohongshu": outbox_for_browser,
    "zhihu": outbox_for_browser,
}


def publish(settings: Settings, db: sqlite3.Connection, post_id: int) -> Delivery:
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if post is None:
        raise ToolError(f"No post #{post_id}. Use list_posts to find the id.")
    if post["status"] == "published":
        raise ToolError(f"Post #{post_id} is already published at {post['location']}.")
    if post["status"] == "handoff":
        raise ToolError(f"Post #{post_id} is waiting for the user to publish it: {post['location']}.")
    # Claim the post before touching the platform. Overlapping cron runs can't both get it, and
    # a crash leaves it in `publishing` for the user to check rather than posting it twice.
    claim = db.execute(
        "UPDATE posts SET status = 'publishing', updated_at = datetime('now', 'localtime') "
        "WHERE id = ? AND status IN ('draft', 'scheduled', 'failed')", (post_id,),
    )
    db.commit()
    if claim.rowcount == 0:
        raise ToolError(
            f"Post #{post_id} is already being published. If it's stuck, check the platform, "
            "then use mark_published or reset_post."
        )
    try:
        delivery = PUBLISHERS.get(post["platform"], outbox)(settings, db, post)
    except MaybePublished as e:
        raise ToolError(
            f"Post #{post_id} may have gone out ({e}). Don't retry: ask the user to check the "
            "platform, then use confirm_published or reset_post."
        ) from e
    except Exception as e:
        db.execute(
            "UPDATE posts SET status = 'failed', error = ?, updated_at = datetime('now', 'localtime') "
            "WHERE id = ?", (f"{type(e).__name__}: {e}", post_id),
        )
        db.commit()
        raise ToolError(f"Publishing #{post_id} failed: {e}") from e
    if delivery.final:
        mark_published(db, post_id, delivery.location)
    else:
        db.execute(
            "UPDATE posts SET status = 'handoff', location = ?, error = NULL, "
            "updated_at = datetime('now', 'localtime') WHERE id = ?", (delivery.location, post_id),
        )
        db.commit()
    return delivery


def mark_published(db: sqlite3.Connection, post_id: int, location: str | None = None) -> None:
    """Record that a post is live. `location` keeps what's stored when the user has no link."""
    cur = db.execute(
        "UPDATE posts SET status = 'published', location = coalesce(?, location), error = NULL, "
        "published_at = datetime('now', 'localtime'), updated_at = datetime('now', 'localtime') "
        "WHERE id = ? AND status IN ('publishing', 'handoff')", (location, post_id),
    )
    db.commit()
    if cur.rowcount == 0:
        raise ToolError(f"Post #{post_id} isn't waiting to be confirmed as published.")


def outcome(post_id: int, delivery: Delivery) -> str:
    if delivery.final:
        return f"#{post_id} published: {delivery.location}"
    line = f"#{post_id} ready for the user to publish: {delivery.location}"
    return f"{line} ({delivery.next_step})" if delivery.next_step else line


def publish_due(settings: Settings, db: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    """Publish every scheduled post whose time has come. Returns one line per post."""
    migrate(db)
    now = now or datetime.now()
    due = db.execute(
        "SELECT id FROM posts WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at",
        (now.isoformat(timespec="minutes"),),
    ).fetchall()
    lines = []
    for r in due:
        try:
            lines.append(outcome(r["id"], publish(settings, db, r["id"])))
        except ToolError as e:
            lines.append(f"#{r['id']} {e}")
    return lines


def stuck_posts(db: sqlite3.Connection) -> list[str]:
    """Posts a crash left half-published. Only a person can tell whether they went out."""
    migrate(db)
    rows = db.execute(
        "SELECT id, platform FROM posts WHERE status = 'publishing' "
        "AND updated_at < datetime('now', 'localtime', ?)", (STUCK_AFTER,),
    ).fetchall()
    return [f"#{r['id']} stuck publishing to {r['platform']}: check whether it went out" for r in rows]


def clean_tags(tags: list[str]) -> str:
    return "\n".join(filter(None, (t.strip().lstrip("#") for t in tags)))


def check_limits(platform: str, title: str, body: str) -> None:
    title_max, body_max = LIMITS.get(platform, (None, None))
    length = x.x_length(body) if platform == "x" else len(body)
    if title_max and len(title) > title_max:
        raise ToolError(f"{platform} titles are at most {title_max} characters; this one has {len(title)}.")
    if body_max and length > body_max:
        note = " (Chinese characters and emoji count as 2, a link as 23)" if platform == "x" else ""
        raise ToolError(f"{platform} posts are at most {body_max} characters{note}; this one has {length}.")


class DraftArgs(BaseModel):
    platform: Platform
    title: str = Field(default="", description="Post title, where the platform has one.")
    body: str = Field(description="Post text, written in the platform's style.")
    media: list[str] = Field(
        default_factory=list, description="Workspace paths of images or videos to attach."
    )
    tags: list[str] = Field(default_factory=list, description="Topics, without the #.")
    cover: str = Field(default="", description="Workspace path of the cover image, if any.")


class EditArgs(BaseModel):
    id: int
    title: str | None = None
    body: str | None = None
    media: list[str] | None = None
    tags: list[str] | None = None
    cover: str | None = None


class ListArgs(BaseModel):
    status: Status | Literal["all"] = "all"
    platform: Platform | None = None


class ScheduleArgs(BaseModel):
    id: int
    at: datetime = Field(description="Local time to publish, e.g. 2026-11-01T20:00.")


class IdArg(BaseModel):
    id: int


class MarkArgs(BaseModel):
    id: int
    url: str | None = Field(default=None, description="Link to the live post, if the user gave one.")


def load(ctx: Context) -> Skill:
    settings, db = ctx.settings, ctx.db
    migrate(db)

    def checked_media(paths: list[str]) -> str:
        for rel in paths:
            if not resolve_in(settings.workspace, rel).is_file():
                raise ToolError(f"No media file at {rel!r} in the workspace.")
        return "\n".join(paths)

    def checked_cover(path: str) -> str:
        return checked_media([path]) if path else ""

    def show(post_id: int) -> str:
        """The stored post, for the approval prompt: what the user is really agreeing to."""
        post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if post is None:
            raise ToolError(f"No post #{post_id}.")
        lines = [f"#{post_id} on {post['platform']}"]
        if post["title"]:
            lines.append(post["title"])
        lines.append(post["body"])
        if post["tags"]:
            lines.append(" ".join(f"#{t}" for t in post["tags"].split("\n")))
        if post["cover"]:
            lines.append(f"cover: {post['cover']}")
        if post["media"]:
            lines.append("media: " + ", ".join(post["media"].split("\n")))
        if post["platform"] == "x" and x.has_link(post["body"]):
            lines.append("Note: X charges about 13x more for a post with a link.")
        return "\n".join(lines)

    @tool(DraftArgs)
    def draft_post(args: DraftArgs) -> str:
        """Save a new post draft for one platform. Draft once per platform when cross-posting."""
        check_limits(args.platform, args.title, args.body)
        cur = db.execute(
            "INSERT INTO posts (platform, title, body, media, tags, cover) VALUES (?, ?, ?, ?, ?, ?)",
            (args.platform, args.title, args.body, checked_media(args.media),
             clean_tags(args.tags), checked_cover(args.cover)),
        )
        db.commit()
        return f"Saved draft #{cur.lastrowid} for {args.platform}."

    @tool(EditArgs)
    def edit_post(args: EditArgs) -> str:
        """Change a post's title, text, media, tags or cover. A scheduled post goes back to
        draft, so it has to be scheduled again."""
        post = db.execute("SELECT * FROM posts WHERE id = ?", (args.id,)).fetchone()
        if post is None:
            raise ToolError(f"No post #{args.id}.")
        if post["status"] == "published":
            raise ToolError(f"Post #{args.id} is already published; edit it on the platform.")
        if post["status"] in ("publishing", "handoff"):
            raise ToolError(f"Post #{args.id} has left Solfish; reset_post it first to change it.")
        title = post["title"] if args.title is None else args.title
        body = post["body"] if args.body is None else args.body
        check_limits(post["platform"], title, body)
        media = post["media"] if args.media is None else checked_media(args.media)
        tags = post["tags"] if args.tags is None else clean_tags(args.tags)
        cover = post["cover"] if args.cover is None else checked_cover(args.cover)
        unscheduled = post["status"] == "scheduled"
        db.execute(
            "UPDATE posts SET title = ?, body = ?, media = ?, tags = ?, cover = ?, status = ?, "
            "scheduled_at = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (title, body, media, tags, cover, "draft" if unscheduled else post["status"],
             None if unscheduled else post["scheduled_at"], args.id),
        )
        db.commit()
        if unscheduled:
            return f"Updated post #{args.id}. It's a draft again; schedule it to approve the new text."
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

    @tool(ScheduleArgs, needs_approval=True,
          preview=lambda a: f"Publish at {a.at:%Y-%m-%d %H:%M}\n{show(a.id)}")
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

    @tool(IdArg, needs_approval=True, preview=lambda a: f"Publish now\n{show(a.id)}")
    def publish_post(args: IdArg) -> str:
        """Publish a post now."""
        return outcome(args.id, publish(settings, db, args.id))

    @tool(MarkArgs)
    def confirm_published(args: MarkArgs) -> str:
        """Record that the user has published a post themselves, or that a stuck post did go out."""
        mark_published(db, args.id, args.url)
        return f"Post #{args.id} is marked as published."

    @tool(IdArg)
    def reset_post(args: IdArg) -> str:
        """Send a failed, stuck or handed-off post back to draft, once the user has said it did
        not go out."""
        cur = db.execute(
            "UPDATE posts SET status = 'draft', scheduled_at = NULL, location = NULL, error = NULL, "
            "updated_at = datetime('now', 'localtime') "
            "WHERE id = ? AND status IN ('failed', 'publishing', 'handoff')", (args.id,),
        )
        db.commit()
        if cur.rowcount == 0:
            raise ToolError(f"Post #{args.id} isn't failed, stuck or handed off.")
        return f"Post #{args.id} is a draft again."

    return Skill(
        name="content",
        instructions=(
            "You run the user's social media: write posts, keep drafts, schedule and publish. "
            "Write natively for each platform rather than pasting one text everywhere: 小红书 "
            "wants a short hooky title, emoji-light paragraphs and hashtags at the end; X is "
            "terse; LinkedIn is professional and first-person; 公众号, 知乎 and blog posts can be "
            "long, and the blog keeps the full version. Given one piece for several platforms, "
            "save one draft per platform, each rewritten for it. Show the user a draft before "
            "scheduling it. Platforms without an API publish to the workspace outbox and the "
            "post waits in `handoff`: pass on the folder or command you get back, and once the "
            "user says it's live, call confirm_published. 小红书 posts need at least one image. "
            "X charges far more for posts with a link, so only add one the user wants. A post in "
            "`publishing` for a while has crashed: ask the user whether it went out, then call "
            "confirm_published or reset_post."
        ),
        tools=[draft_post, edit_post, list_posts, schedule_post, publish_post, confirm_published,
               reset_post],
    )
