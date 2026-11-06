"""小红书 handoff: fill the creator page in a visible browser, then stop.

There is no posting API for individuals, so this works on the page the user would use by hand.
It runs only while the user is there, one post at a time, and never presses publish: nothing in
this module touches that button, and a test keeps it that way. Anything unexpected (expired login,
a verification page, a changed layout) stops it, with no retry.

The selectors come from the page as used by xpzouying/xiaohongshu-mcp. When the site changes
them, this block is the only thing to update.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from ...config import Settings
from ...tools import ToolError
from ..core import resolve_in

PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
UPLOAD_AREA = "div.upload-content"
TAB = "div.creator-tab"
FIRST_FILE_INPUT = ".upload-input"
NEXT_FILE_INPUT = 'input[type="file"]'
IMAGE_PREVIEW = ".img-preview-area .pr"
TITLE = "div.d-input input"
BODY = (
    'div[role="textbox"][contenteditable="true"], '
    'div.tiptap[contenteditable="true"], div.ql-editor'
)
TOPIC_SUGGESTION = "#creator-editor-topic-container .item"
TOPIC_NAME = ".name"

NEXT_STEP = "check the page and press publish"
LOGIN_TIMEOUT_MS = 300_000  # long enough to find the phone and scan the code
UPLOAD_TIMEOUT_MS = 60_000
TOPIC_WAIT_MS = 1500
MAX_TOPICS = 10
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}


def images(settings: Settings, post: sqlite3.Row) -> list[Path]:
    """The post's images, cover first. An image post without one can't be filled in."""
    rels = list(filter(None, [post["cover"], *post["media"].split("\n")]))
    if not rels:
        raise ToolError("A 小红书 post needs at least one image. Add a cover or media.")
    paths = [resolve_in(settings.workspace, rel) for rel in rels]
    for path in paths:
        if path.suffix.lower() not in IMAGE_TYPES:
            raise ToolError(f"{path.name}: the 小红书 handoff only uploads images for now.")
    return paths


def check(settings: Settings, post: sqlite3.Row) -> None:
    images(settings, post)


def _open_publish_page(page: Any) -> None:
    page.goto(PUBLISH_URL)
    if "login" in page.url:  # first run, or the session expired: the user scans the code
        page.wait_for_url(lambda url: "login" not in url, timeout=LOGIN_TIMEOUT_MS)
        page.goto(PUBLISH_URL)
    page.wait_for_selector(UPLOAD_AREA)


def _choose_image_tab(page: Any) -> None:
    """The page can keep a copy of the tab off screen, which still counts as visible."""
    tabs = page.locator(TAB).filter(has_text=re.compile(r"^\s*上传图文\s*$")).locator("visible=true")
    for i in range(tabs.count()):
        box = tabs.nth(i).bounding_box()
        if box and box["x"] >= 0 and box["y"] >= 0:
            tabs.nth(i).click()
            return
    raise ToolError("Couldn't find the 上传图文 tab on the page.")


def _upload(page: Any, paths: list[Path]) -> None:
    for i, path in enumerate(paths):
        page.locator(FIRST_FILE_INPUT if i == 0 else NEXT_FILE_INPUT).first.set_input_files(str(path))
        page.locator(IMAGE_PREVIEW).nth(i).wait_for(timeout=UPLOAD_TIMEOUT_MS)


def _write_body(page: Any, body: str) -> None:
    page.locator(BODY).first.click()
    lines = body.split("\n")
    for i, line in enumerate(lines):
        page.keyboard.insert_text(line)
        if i < len(lines) - 1:
            page.keyboard.press("Enter")


def _add_topics(page: Any, tags: list[str]) -> None:
    """Type each tag and pick the exact suggestion; with none, it stays as plain text."""
    page.keyboard.press("Enter")
    for tag in tags[:MAX_TOPICS]:
        page.keyboard.type(f"#{tag}")
        page.wait_for_timeout(TOPIC_WAIT_MS)
        name = re.compile(rf"^\s*#?{re.escape(tag)}\s*$", re.IGNORECASE)
        match = page.locator(TOPIC_SUGGESTION).filter(has=page.locator(TOPIC_NAME, has_text=name))
        if match.count():
            match.first.click()
        else:
            page.keyboard.type(" ")


def fill(page: Any, settings: Settings, post: sqlite3.Row) -> None:
    paths = images(settings, post)
    _open_publish_page(page)
    page.keyboard.press("Escape")  # new-feature popovers cover the tabs
    _choose_image_tab(page)
    _upload(page, paths)
    page.locator(TITLE).fill(post["title"])
    _write_body(page, post["body"])
    if post["tags"]:
        _add_topics(page, post["tags"].split("\n"))
