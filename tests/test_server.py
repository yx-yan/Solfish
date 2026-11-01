from __future__ import annotations

import asyncio
import threading
import time

import pytest
from conftest import FakeClient, message, text, tool_use
from fastapi.testclient import TestClient

from solfish.server import COOKIE, EventHub, create_app
from solfish.session import SessionManager

TOKEN = "test-token"


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def make(settings, *responses):
    manager = SessionManager(settings, client=FakeClient(*responses), trace=False, approval_timeout_s=5)
    web = TestClient(create_app(manager, TOKEN))
    return manager, web


def test_everything_needs_the_token(settings):
    _, web = make(settings)
    assert web.get("/api/sessions").status_code == 401
    assert web.get("/").status_code == 401
    assert web.get("/?token=wrong").status_code == 401

    r = web.get(f"/?token={TOKEN}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"  # token leaves the address bar
    assert web.cookies.get(COOKIE) == TOKEN
    page = web.get("/")
    assert page.status_code == 200 and "img-src 'self'" in page.headers["content-security-policy"]
    assert web.get("/api/sessions").status_code == 200

    bearer = TestClient(web.app, headers={"Authorization": f"Bearer {TOKEN}"})
    assert bearer.get("/api/sessions").status_code == 200


def test_a_turn_with_an_approval_from_the_page(settings):
    manager, web = make(
        settings,
        message(tool_use("write_file", {"path": "notes/x.txt", "content": "hi"}), stop="tool_use"),
        message(text("Wrote **it**.")),
    )
    web.cookies.set(COOKIE, TOKEN)
    sid = web.post("/api/sessions").json()["id"]
    assert web.post(f"/api/sessions/{sid}/messages", json={"text": "write x"}).status_code == 202

    wait_for(lambda: web.get("/api/approvals").json())
    approval = web.get("/api/approvals").json()[0]
    assert approval["session"] == sid and approval["name"] == "write_file"
    assert web.post(f"/api/sessions/{sid}/messages", json={"text": "again"}).status_code == 409
    assert web.post(f"/api/approvals/{approval['id']}", json={"allow": True}).json() == {"ok": True}
    wait_for(lambda: not manager.busy(sid))

    s = web.get(f"/api/sessions/{sid}").json()
    assert [i["kind"] for i in s["items"]] == ["user", "tool_call", "tool_result", "assistant"]
    assert s["title"] == "write x" and not s["busy"] and not s["pending"]
    assert web.post(f"/api/approvals/{approval['id']}", json={"allow": True}).status_code == 404

    assert [f["path"] for f in web.get("/api/files").json()] == ["notes/x.txt"]
    assert web.get("/api/files/notes/x.txt").text == "hi"


def test_files_stay_inside_the_workspace_and_html_downloads(settings):
    _, web = make(settings)
    web.cookies.set(COOKIE, TOKEN)
    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "page.html").write_text("<script>alert(1)</script>")
    (settings.workspace / "cv.pdf").write_bytes(b"%PDF-1.4")

    assert web.get("/api/files/..%2Fsolfish.db").status_code == 404
    assert web.get("/api/files/../solfish.db").status_code == 404
    assert web.get("/api/files/page.html").headers["content-disposition"].startswith("attachment")
    assert web.get("/api/files/cv.pdf").headers["content-disposition"].startswith("inline")
    assert web.get("/api/sessions/nope").status_code == 404


def test_event_hub_carries_events_from_agent_threads():
    hub = EventHub()

    async def main():
        hub.loop = asyncio.get_running_loop()
        stream = hub.stream()
        assert (await anext(stream)).startswith(":")
        threading.Thread(target=hub, args=("s1", "tool_call", {"name": "当前时间"})).start()
        chunk = await asyncio.wait_for(anext(stream), 5)
        await stream.aclose()
        return chunk

    chunk = asyncio.run(main())
    assert chunk.startswith("data: ") and '"session": "s1"' in chunk and "当前时间" in chunk
    assert not hub.queues  # a closed connection unsubscribes


def test_closing_the_hub_ends_open_streams():
    hub = EventHub()

    async def main():
        hub.loop = asyncio.get_running_loop()
        stream = hub.stream()
        await anext(stream)
        hub.close()  # what Ctrl-C does
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), 5)

    asyncio.run(main())
    assert not hub.queues


@pytest.mark.parametrize("path", ["/static/app.js", "/static/style.css"])
def test_static_assets_are_served(settings, path):
    _, web = make(settings)
    assert web.get(path).status_code == 200
