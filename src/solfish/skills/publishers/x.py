"""X publisher: raw REST with OAuth 1.0a user tokens, which don't expire, so cron can use them.

Images only. Costs real money per post: see https://docs.x.com/x-api/getting-started/pricing
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx2

from ...config import Settings, XAuth
from ...tools import ToolError
from ..core import resolve_in
from .base import Delivery, MaybePublished

API = "https://api.x.com/2"
TIMEOUT_S = 60.0
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}

# X counts in weighted characters: these code points are 1, everything else (CJK, emoji) is 2,
# and any URL is 23. From twitter-text's v3 config.
LIGHT = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
URL_LENGTH = 23
URL = re.compile(r"https?://\S+")
MAX_LENGTH = 280


def x_length(text: str) -> int:
    text, urls = URL.subn("", text)
    light = sum(any(lo <= ord(c) <= hi for lo, hi in LIGHT) for c in text)
    return urls * URL_LENGTH + light + 2 * (len(text) - light)


def has_link(text: str) -> bool:
    return URL.search(text) is not None


def _pct(value: str) -> str:
    return quote(value, safe="")


def oauth_header(
    auth: XAuth,
    method: str,
    url: str,
    params: dict[str, str] | None = None,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    """The Authorization header for one request. `params` are query or form parameters; JSON and
    multipart bodies aren't signed, so publishing passes none."""
    oauth = {
        "oauth_consumer_key": auth.api_key or "",
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": auth.access_token or "",
        "oauth_version": "1.0",
    }
    signed = sorted((_pct(k), _pct(v)) for k, v in {**oauth, **(params or {})}.items())
    base = "&".join([method, _pct(url), _pct("&".join(f"{k}={v}" for k, v in signed))])
    key = f"{_pct(auth.api_secret or '')}&{_pct(auth.access_secret or '')}"
    digest = hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode()
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


def _images(settings: Settings, post: sqlite3.Row) -> list[Path]:
    rels = list(filter(None, [post["cover"], *post["media"].split("\n")]))
    if len(rels) > MAX_IMAGES:
        raise ToolError(f"X takes at most {MAX_IMAGES} images; this post has {len(rels)}.")
    paths = [resolve_in(settings.workspace, rel) for rel in rels]
    for path in paths:
        if path.suffix.lower() not in IMAGE_TYPES:
            raise ToolError(f"{path.name}: the X publisher only uploads images ({sorted(IMAGE_TYPES)}).")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ToolError(f"{path.name} is over X's 5 MB image limit.")
    return paths


def _check(response: httpx2.Response) -> dict[str, Any]:
    if response.is_success:
        return response.json()
    raise ToolError(f"X said {response.status_code}: {response.text[:300]}")


def publish(
    settings: Settings,
    db: sqlite3.Connection,
    post: sqlite3.Row,
    transport: httpx2.BaseTransport | None = None,
) -> Delivery:
    auth = settings.x
    if not auth.configured:
        raise ToolError("X isn't set up: set the SOLFISH_X_* variables in .env (see .env.example).")
    text = post["body"]
    if post["tags"]:
        text += "\n\n" + " ".join(f"#{t}" for t in post["tags"].split("\n"))
    if x_length(text) > MAX_LENGTH:  # the draft fits, but the tags pushed it over
        raise ToolError(f"With its tags this post is {x_length(text)} of {MAX_LENGTH} characters.")
    images = _images(settings, post)

    with httpx2.Client(transport=transport, timeout=TIMEOUT_S) as client:
        media_ids = []
        for path in images:
            url = f"{API}/media/upload"
            r = client.post(
                url,
                headers={"Authorization": oauth_header(auth, "POST", url)},
                data={"media_category": "tweet_image"},
                files={"media": (path.name, path.read_bytes())},
            )
            media_ids.append(_check(r)["data"]["id"])

        url = f"{API}/tweets"
        body: dict[str, Any] = {"text": text}
        if media_ids:
            body["media"] = {"media_ids": media_ids}
        try:
            r = client.post(url, headers={"Authorization": oauth_header(auth, "POST", url)}, json=body)
        except httpx2.TimeoutException as e:
            raise MaybePublished(f"X didn't answer in {TIMEOUT_S:.0f}s") from e
    return Delivery(f"https://x.com/i/status/{_check(r)['data']['id']}")
