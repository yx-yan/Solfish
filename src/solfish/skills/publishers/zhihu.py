"""知乎 handoff: fill the column editor in a visible browser, then stop.

Same rules as the 小红书 handoff: there's no posting API, it only runs while the user is there,
and it never presses publish. The body goes in as a real paste, which replaces the clipboard, so
知乎 can offer to convert the Markdown. Body images, topics and the column are left to the user.

The selectors come from an undated Selenium script, so expect to update this block.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ...config import Settings
from ...tools import ToolError
from ..core import resolve_in

WRITE_URL = "https://zhuanlan.zhihu.com/write"
TITLE = 'textarea[placeholder*="请输入标题"]'
BODY = ".DraftEditor-editorContainer .public-DraftEditor-content"
COVER_INPUT = "input.UploadPicture-input"
LOGIN_TIMEOUT_MS = 300_000  # the title box appears once the user has logged in
NEXT_STEP = "check the page, add images and topics, and press publish"


def check(settings: Settings, post: sqlite3.Row) -> None:
    if not post["title"]:
        raise ToolError("A 知乎 article needs a title.")


def fill(page: Any, settings: Settings, post: sqlite3.Row) -> None:
    page.goto(WRITE_URL)
    page.wait_for_selector(TITLE, timeout=LOGIN_TIMEOUT_MS)
    page.locator(TITLE).fill(post["title"])
    if post["cover"]:
        page.locator(COVER_INPUT).set_input_files(str(resolve_in(settings.workspace, post["cover"])))
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.evaluate("text => navigator.clipboard.writeText(text)", post["body"])
    page.locator(BODY).click()
    page.keyboard.press("ControlOrMeta+V")
