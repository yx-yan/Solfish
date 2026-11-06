"""LinkedIn publisher: posts to the member's own profile through the Posts API.

Self-serve apps get no refresh token, so the access token has to be renewed by hand every 60 days
(the app's OAuth token tool works). It needs the openid, profile and w_member_social scopes.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import httpx2

from ...config import Settings
from ...tools import ToolError
from ..core import resolve_in
from .base import Delivery, MaybePublished

API = "https://api.linkedin.com"
TIMEOUT_S = 60.0
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".gif"}
# LinkedIn's "little text format". Unescaped, these can cut a post short. A # that starts a word
# stays as it is, because that's how a hashtag is written.
RESERVED = re.compile(r"[\\|{}@\[\]()<>*_~]|#(?=\s|$)")


def escape(text: str) -> str:
    return RESERVED.sub(lambda m: "\\" + m.group(), text)


def _image(settings: Settings, post: sqlite3.Row) -> Path | None:
    rels = list(filter(None, [post["cover"], *post["media"].split("\n")]))
    if len(rels) > 1:
        raise ToolError("The LinkedIn publisher takes one image; this post has several.")
    if not rels:
        return None
    path = resolve_in(settings.workspace, rels[0])
    if path.suffix.lower() not in IMAGE_TYPES:
        raise ToolError(f"{path.name}: LinkedIn images must be one of {sorted(IMAGE_TYPES)}.")
    return path


def _check(response: httpx2.Response) -> httpx2.Response:
    if response.status_code == 401:
        raise ToolError("LinkedIn rejected the token. They last 60 days: make a new one.")
    if not response.is_success:
        raise ToolError(f"LinkedIn said {response.status_code}: {response.text[:300]}")
    return response


def publish(
    settings: Settings,
    db: sqlite3.Connection,
    post: sqlite3.Row,
    transport: httpx2.BaseTransport | None = None,
) -> Delivery:
    if not settings.linkedin_token:
        raise ToolError("LinkedIn isn't set up: set SOLFISH_LINKEDIN_TOKEN in .env.")
    image = _image(settings, post)
    text = escape(post["body"])
    if post["tags"]:
        text += "\n\n" + " ".join(f"#{t}" for t in post["tags"].split("\n"))
    bearer = {"Authorization": f"Bearer {settings.linkedin_token}"}
    rest = bearer | {
        "Linkedin-Version": settings.linkedin_version,
        "X-Restli-Protocol-Version": "2.0.0",
    }

    with httpx2.Client(transport=transport, timeout=TIMEOUT_S) as client:
        author = f"urn:li:person:{_check(client.get(f'{API}/v2/userinfo', headers=bearer)).json()['sub']}"
        body: dict[str, Any] = {
            "author": author,
            "commentary": text,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if image:
            r = client.post(
                f"{API}/rest/images?action=initializeUpload",
                headers=rest,
                json={"initializeUploadRequest": {"owner": author}},
            )
            upload = _check(r).json()["value"]
            _check(client.put(upload["uploadUrl"], headers=bearer, content=image.read_bytes()))
            body["content"] = {"media": {"id": upload["image"], "altText": post["title"]}}
        try:
            r = client.post(f"{API}/rest/posts", headers=rest, json=body)
        except httpx2.TimeoutException as e:
            raise MaybePublished(f"LinkedIn didn't answer in {TIMEOUT_S:.0f}s") from e
    return Delivery(f"https://www.linkedin.com/feed/update/{_check(r).headers['x-restli-id']}")
