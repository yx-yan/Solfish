"""Telegram as a channel: the same sessions and approvals, from the phone.

Long polling (`getUpdates`), so it works from a laptop at home with no public address. It talks
to the Bot API directly with httpx2, which the anthropic SDK already brings in; the protocol
Solfish needs is a handful of methods, not a framework. One chat id is answered and every other
update is dropped: this bot can write files and send email.

A turn shows up as one "working" message that is edited as tools run, then the answer. Approval
requests from any session arrive with Allow / Deny buttons, so something started in the browser
can be approved from the phone.
"""

from __future__ import annotations

import json
import queue
import random
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx2

from ..config import Settings
from ..session import Busy, SessionManager
from ..skills import open_db, spark

LIMIT = 4000  # Telegram's cap is 4096 UTF-16 units; this leaves room for emoji
MAX_PREVIEW = 3500  # of a post awaiting approval; leaves room for the rest of the message
HELP = """\
Send a message to talk to Solfish.
/new start a new session
/sessions switch to a recent one
/stop stop the current turn
/cost tokens and cost of this session
/files send a recent workspace file
/spark let Solfish pick something to talk about
/interests what it knows you like talking about"""


class TelegramError(Exception):
    pass


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """Cut a long reply into messages, at line breaks where possible."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return [*parts, text] if text else parts or [""]


def _preview(value: Any, limit: int) -> str:
    s = " ".join((value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


class BotApi:
    def __init__(self, token: str, transport: httpx2.BaseTransport | None = None) -> None:
        self.http = httpx2.Client(base_url=f"https://api.telegram.org/bot{token}/", timeout=40,
                                 transport=transport)

    def __call__(self, method: str, **params: Any) -> Any:
        try:
            r = self.http.post(method, json=params)
            body = r.json()
        except (httpx2.HTTPError, ValueError) as e:
            # Never str(e): the request URL contains the bot token.
            raise TelegramError(f"{method}: {type(e).__name__}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', r.status_code)}")
        return body["result"]

    def send_document(self, chat_id: int, name: str, data: bytes) -> None:
        try:
            r = self.http.post("sendDocument", data={"chat_id": str(chat_id)},
                               files={"document": (name, data)})
        except httpx2.HTTPError as e:
            raise TelegramError(f"sendDocument: {type(e).__name__}") from None
        if not r.json().get("ok"):
            raise TelegramError(f"sendDocument: {r.json().get('description')}")


class TelegramBot:
    def __init__(self, manager: SessionManager, api: BotApi, chat_id: int | None, *,
                 background: bool = True) -> None:
        self.manager = manager
        self.api = api
        self.chat_id = chat_id
        recent = [r for r in manager.store.recent(50) if r["channel"] == "telegram"]
        self.session: str | None = recent[0]["id"] if recent else None
        self._progress: dict[str, tuple[int, list[str]]] = {}  # session -> (message id, lines)
        self._approvals: dict[str, tuple[int, str]] = {}  # approval id -> (message id, tool name)
        self._files: list[str] = []
        # Rendering runs on its own thread, in order, so a slow API never holds up the agent.
        self._outbox: queue.Queue[Callable[[], None]] | None = queue.Queue() if background else None
        if self._outbox is not None:
            threading.Thread(target=self._drain, daemon=True).start()
        manager.subscribe(self.on_event)

    # ---------- plumbing ----------

    def _drain(self) -> None:
        assert self._outbox is not None
        while True:
            self._safely(self._outbox.get())

    def _later(self, fn: Callable[[], None]) -> None:
        if self._outbox is None:
            self._safely(fn)
        else:
            self._outbox.put(fn)

    @staticmethod
    def _safely(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:  # one failed message must not stop the bot
            print(f"telegram: {e}", file=sys.stderr)

    def send(self, text: str, **extra: Any) -> int:
        message_id = 0
        for part in split_message(text):
            message_id = self.api("sendMessage", chat_id=self.chat_id, text=part,
                                  link_preview_options={"is_disabled": True}, **extra)["message_id"]
        return message_id

    def edit(self, message_id: int, text: str, **extra: Any) -> None:
        self.api("editMessageText", chat_id=self.chat_id, message_id=message_id,
                 text=text[:LIMIT], **extra)

    def poll(self, stop: threading.Event) -> None:
        """Long-poll for updates until `stop` is set."""
        offset = None
        while not stop.is_set():
            try:
                updates = self.api("getUpdates", offset=offset, timeout=30,
                                   allowed_updates=["message", "callback_query"])
            except TelegramError as e:
                print(f"telegram: {e}; retrying", file=sys.stderr)
                stop.wait(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                self._safely(lambda u=u: self.handle(u))

    # ---------- updates from the user ----------

    def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            q = update["callback_query"]
            if q["message"]["chat"]["id"] == self.chat_id:
                self._on_button(q)
            return
        msg = update.get("message") or {}
        chat = msg.get("chat", {}).get("id")
        if chat is None or "text" not in msg:
            return
        if self.chat_id is None:
            # Not set up yet: say the id, do nothing else.
            self.api("sendMessage", chat_id=chat,
                     text=f"Your chat id is {chat}. Set SOLFISH_TELEGRAM_CHAT_ID={chat} and restart.")
            return
        if chat != self.chat_id:
            return  # anyone else gets silence
        text = msg["text"].strip()
        if text.startswith("/"):
            self._command(text.split()[0].split("@")[0])
        else:
            self._message(text)

    def _command(self, cmd: str) -> None:
        if cmd == "/new":
            self.session = None
            self.send("New session. Send a message to start.")
        elif cmd == "/sessions":
            rows = self.manager.store.recent(8)
            if not rows:
                self.send("No sessions yet.")
                return
            buttons = [[{"text": ("• " if r["id"] == self.session else "") + (r["title"] or "New chat")[:40]
                         + f"  ({r['channel']})", "callback_data": f"s:{r['id']}"}] for r in rows]
            self.send("Switch to:", reply_markup={"inline_keyboard": buttons})
        elif cmd == "/stop":
            if self.session and self.manager.busy(self.session):
                self.manager.cancel(self.session)
                self.send("Stopping before the next tool call.")
            else:
                self.send("Nothing is running.")
        elif cmd == "/cost":
            if not self.session:
                self.send("No session yet.")
                return
            t = self.manager.totals(self.session)
            self.send(f"{t.llm_calls} model calls, {t.tool_calls} tool calls, "
                      f"{t.tokens['input'] + t.tokens['cache_read']} tokens in, {t.tokens['output']} out, "
                      f"${t.cost_usd:.4f} (since the server started)")
        elif cmd == "/files":
            ws = self.manager.settings.workspace
            files = sorted((f for f in ws.rglob("*") if f.is_file()), key=lambda f: f.stat().st_mtime,
                           reverse=True)[:8] if ws.is_dir() else []
            self._files = [f.relative_to(ws).as_posix() for f in files]
            if not files:
                self.send("The workspace is empty.")
                return
            buttons = [[{"text": p[-50:], "callback_data": f"f:{i}"}] for i, p in enumerate(self._files)]
            self.send("Which file?", reply_markup={"inline_keyboard": buttons})
        elif cmd == "/spark":
            self._later(self.spark)  # one model call; off the polling thread
        elif cmd == "/interests":
            self.send(spark.describe(self.manager.store.db))
        else:
            self.send(HELP)

    def spark(self, *, scheduled: bool = False, now: datetime | None = None) -> bool:
        """Start a conversation Solfish picked, and make it the current session. A scheduled
        spark stays silent when it fails or has nothing to say; /spark tells you."""
        if not scheduled:
            self.api("sendChatAction", chat_id=self.chat_id, action="typing")
        try:
            started = spark.start(self.manager.store, self.manager.model_client(),
                                  self.manager.settings, "telegram", now=now)
        except Exception as e:
            if not scheduled:
                self.send(f"⚠️ Couldn't start a conversation: {e}")
            raise
        if started is None:
            if not scheduled:
                self.send("I don't know you well enough yet. Tell me what you're into, and I'll remember.")
            return False
        self.session, opener = started
        self.send(opener)
        return True

    def _message(self, text: str) -> None:
        if self.session is None:
            self.session = self.manager.create("telegram")
        try:
            self.manager.send(self.session, text)
        except Busy:
            self.send("Still working on the last message. /stop to interrupt it.")

    def _on_button(self, q: dict[str, Any]) -> None:
        kind, _, value = q.get("data", "").partition(":")
        note = ""
        if kind == "a":
            approval_id, _, allow = value.partition(":")
            ok = self.manager.approvals.resolve(approval_id, allow == "1")
            note = ("Allowed" if allow == "1" else "Declined") if ok else "Already answered or timed out."
        elif kind == "s" and self.manager.store.get(value) is not None:
            self.session = value
            self.send(f"Now in: {self.manager.store.get(value)['title'] or 'New chat'}")
        elif kind == "f" and value.isdigit() and int(value) < len(self._files):
            path = (self.manager.settings.workspace / self._files[int(value)]).resolve()
            if path.is_relative_to(self.manager.settings.workspace.resolve()) and path.is_file():
                self.api.send_document(self.chat_id, path.name, path.read_bytes())
        self.api("answerCallbackQuery", callback_query_id=q["id"], text=note)

    # ---------- events from the agent ----------

    def on_event(self, session_id: str, kind: str, data: dict[str, Any]) -> None:
        if self.chat_id is not None:
            self._later(lambda: self._render(session_id, kind, data))

    def _render(self, session_id: str, kind: str, data: dict[str, Any]) -> None:
        if kind == "approval_request":
            shown = data["input"].get("preview") or _preview(data["input"], 1500)
            shown = shown[:MAX_PREVIEW] + "…" if len(shown) > MAX_PREVIEW else shown
            where = "" if session_id == self.session else \
                f"\n(session: {self.manager.store.get(session_id)['title'] or session_id})"
            message_id = self.send(
                f"Allow {data['name']}?\n{shown}{where}",
                reply_markup={"inline_keyboard": [[
                    {"text": "✅ Allow", "callback_data": f"a:{data['id']}:1"},
                    {"text": "❌ Deny", "callback_data": f"a:{data['id']}:0"},
                ]]},
            )
            self._approvals[data["id"]] = (message_id, data["name"])
            return
        if kind == "approval_resolved":
            if data["id"] in self._approvals:
                message_id, name = self._approvals.pop(data["id"])
                self.edit(message_id, f"{name}: " + ("✓ allowed" if data["allow"] else "✗ declined"))
            return
        if session_id != self.session:
            return

        if kind == "user_message":
            self.api("sendChatAction", chat_id=self.chat_id, action="typing")
            self._progress[session_id] = (self.send("⏳ Working…"), [])
        elif kind in ("tool_call", "tool_result") and session_id in self._progress:
            message_id, lines = self._progress[session_id]
            if kind == "tool_call":
                lines.append(f"→ {data['name']} {_preview(data['input'], 60)}")
            elif data["is_error"]:
                lines[-1] += "  ✗"
            self.edit(message_id, "⏳ Working…\n" + "\n".join(lines[-12:]))
        elif kind == "turn_end":
            if session_id in self._progress:
                message_id, lines = self._progress.pop(session_id)
                done = "✓" if data["stop"] == "end_turn" else f"■ {data['stop']}"
                self.edit(message_id, f"{done} {data['steps']} steps\n" + "\n".join(lines[-12:]))
            self.send(data["text"] or "(no reply)")
        elif kind == "error":
            self._progress.pop(session_id, None)
            self.send(f"⚠️ {data['message']}")


class Sparker:
    """Texts the user first at random times: a fresh random plan every day, and each planned
    time sends a spark if `spark.may_send` agrees (it backs off while sparks go unanswered)."""

    def __init__(self, bot: TelegramBot, rng: random.Random | None = None) -> None:
        self.bot = bot
        self.rng = rng or random.Random()
        self.db = open_db(bot.manager.settings)
        spark.migrate(self.db)
        self.day = None
        self.plan: list[datetime] = []

    def tick(self, now: datetime) -> bool:
        """Send a spark if one is due. True if one was sent."""
        s = self.bot.manager.settings
        if self.day != now.date():
            self.day = now.date()
            self.plan = [t for t in spark.plan_day(now.date(), self.rng, s.sparks_per_day, s.spark_hours)
                         if t > now]
        if not self.plan or self.plan[0] > now:
            return False
        self.plan = [t for t in self.plan if t > now]  # missed times collapse into this one
        busy = self.bot.session is not None and self.bot.manager.busy(self.bot.session)
        if busy or not spark.may_send(self.db, now):
            return False
        return self.bot.spark(scheduled=True, now=now)

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(30):
            self.bot._safely(lambda: self.tick(datetime.now()))


def start(manager: SessionManager, settings: Settings) -> threading.Event | None:
    """Run the bot in the background if a token is configured. Set the returned event to stop it."""
    if not settings.telegram_token:
        return None
    bot = TelegramBot(manager, BotApi(settings.telegram_token), settings.telegram_chat_id)
    stop = threading.Event()
    threading.Thread(target=bot.poll, args=(stop,), daemon=True).start()
    if settings.telegram_chat_id is not None and settings.sparks_per_day[1] > 0:
        threading.Thread(target=Sparker(bot).run, args=(stop,), daemon=True).start()
    return stop


def notify(settings: Settings, text: str) -> None:
    """One message to the configured chat, for jobs that run without a server (cron)."""
    if settings.telegram_token and settings.telegram_chat_id:
        api = BotApi(settings.telegram_token)
        for part in split_message(text):
            api("sendMessage", chat_id=settings.telegram_chat_id, text=part)
