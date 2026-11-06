"""Blog publisher: adds a draft to yuxuanyan.com, to be published from its /admin.

Calls one token-gated database function through PostgREST. That function can only add a new
unpublished article, so the token on this machine can't change anything public. The slug comes
from the post id, which makes a retry fail on the taken slug rather than add a second draft.
"""

from __future__ import annotations

import re
import sqlite3

import httpx2

from ...config import Settings
from ...tools import ToolError
from .base import Delivery, MaybePublished

TIMEOUT_S = 30.0
CJK = re.compile(r"[㐀-鿿]")
TAG_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NEXT_STEP = "preview it in /admin, set its slug, excerpt and images, then press Save & publish"


def locale(post: sqlite3.Row) -> str:
    return "zh" if CJK.search(post["title"] + post["body"]) else "en"


def publish(
    settings: Settings,
    db: sqlite3.Connection,
    post: sqlite3.Row,
    transport: httpx2.BaseTransport | None = None,
) -> Delivery:
    blog = settings.blog
    if not blog.configured:
        raise ToolError("The blog isn't set up: set the SOLFISH_BLOG_* variables in .env.")
    if not post["title"]:
        raise ToolError("A blog post needs a title.")
    tags = [t.lower() for t in post["tags"].split("\n") if TAG_SLUG.match(t.lower())]
    with httpx2.Client(transport=transport, timeout=TIMEOUT_S) as client:
        try:
            r = client.post(
                f"{blog.supabase_url}/rest/v1/rpc/create_article_draft",
                headers={"apikey": blog.publishable_key or ""},
                json={
                    "p_token": blog.token,
                    "p_slug": f"solfish-{post['id']}",
                    "p_locale": locale(post),
                    "p_title": post["title"],
                    "p_body_md": post["body"],
                    "p_tags": tags,
                },
            )
        except httpx2.TimeoutException as e:
            raise MaybePublished(f"the blog didn't answer in {TIMEOUT_S:.0f}s") from e
    if r.status_code == 409:
        raise ToolError(f"The blog already has a draft with the slug solfish-{post['id']}.")
    if not r.is_success:
        raise ToolError(f"The blog said {r.status_code}: {r.text[:300]}")
    return Delivery(f"{blog.site}/admin/articles/{r.json()}", final=False, next_step=NEXT_STEP)
