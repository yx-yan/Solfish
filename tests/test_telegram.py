from __future__ import annotations

import json
import threading
import time

import httpx2
from conftest import FakeClient, message, text, tool_use

from solfish.channels.telegram import BotApi, TelegramBot, split_message
from solfish.session import SessionManager

ME = 42


class FakeTelegram:
    """Answers Bot API calls and records them as (method, params)."""

    def __init__(self, updates=()):
        self.calls: list[tuple[str, dict]] = []
        self.updates = list(updates)
        self.next_id = 100

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        is_json = request.headers.get("content-type", "").startswith("application/json")
        params = json.loads(request.content) if is_json else {}
        self.calls.append((method, params))
        result: object = True
        if method in ("sendMessage", "editMessageText"):
            self.next_id += 1
            result = {"message_id": params.get("message_id", self.next_id)}
        elif method == "getUpdates":
            result, self.updates = self.updates, []
        return httpx2.Response(200, json={"ok": True, "result": result})

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


def make(settings, *responses, chat_id=ME):
    manager = SessionManager(settings, client=FakeClient(*responses), trace=False, approval_timeout_s=5)
    tg = FakeTelegram()
    bot = TelegramBot(manager, BotApi("TOKEN", transport=httpx2.MockTransport(tg)), chat_id,
                      background=False)
    return manager, bot, tg


def say(text, chat=ME):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def press(data, chat=ME):
    return {"update_id": 2, "callback_query": {"id": "cb", "data": data,
                                               "message": {"chat": {"id": chat}}}}


def wait_idle(manager, sid):
    deadline = time.monotonic() + 5
    while manager.busy(sid):
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_unconfigured_bot_only_tells_you_your_chat_id(settings):
    manager, bot, tg = make(settings, chat_id=None)
    bot.handle(say("hi", chat=7))
    assert tg.sent()[0]["chat_id"] == 7 and "SOLFISH_TELEGRAM_CHAT_ID=7" in tg.sent()[0]["text"]
    assert not manager.store.recent()


def test_strangers_get_silence(settings):
    manager, bot, tg = make(settings)
    bot.handle(say("delete everything", chat=666))
    bot.handle(press("a:x:1", chat=666))
    assert tg.calls == [] and not manager.store.recent()


def test_a_turn_from_the_phone(settings):
    manager, bot, tg = make(
        settings,
        message(tool_use("current_time", {}), stop="tool_use"),
        message(text("It's " + "very " * 1200 + "late.")),
    )
    bot.handle(say("what time is it?"))
    sid = bot.session
    wait_idle(manager, sid)

    assert manager.store.get(sid)["channel"] == "telegram"
    progress = tg.sent("editMessageText")
    assert "→ current_time" in progress[0]["text"]
    assert progress[-1]["text"].startswith("✓ 2 steps")
    answer = [p["text"] for p in tg.sent()][1:]  # after "Working…"
    assert len(answer) == 2 and "".join(answer).endswith("late.")  # split under Telegram's limit


def test_approval_buttons(settings):
    manager, bot, tg = make(
        settings,
        message(tool_use("write_file", {"path": "x.txt", "content": "hi"}), stop="tool_use"),
        message(text("Done.")),
    )
    bot.handle(say("write x"))
    deadline = time.monotonic() + 5
    while not manager.approvals.pending():
        assert time.monotonic() < deadline
        time.sleep(0.01)

    ask = next(p for p in tg.sent() if "reply_markup" in p)
    allow = ask["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.handle(press(allow))
    wait_idle(manager, bot.session)

    assert (settings.workspace / "x.txt").read_text() == "hi"
    assert tg.sent("answerCallbackQuery")[0]["text"] == "Allowed"
    assert any(p["text"] == "write_file: ✓ allowed" for p in tg.sent("editMessageText"))
    bot.handle(press(allow))  # a second tap
    assert tg.sent("answerCallbackQuery")[1]["text"].startswith("Already answered")


def test_approvals_from_other_sessions_reach_the_phone(settings):
    manager, bot, tg = make(settings)
    web = manager.create("web")
    manager.store.sync(web, [{"role": "user", "content": "write the cover letter"}])
    threading.Thread(target=manager.approvals.request,
                     args=(web, "send_email", {"to": "hr@acme.com"},
                           lambda kind, **d: bot.on_event(web, kind, d))).start()
    deadline = time.monotonic() + 5
    while not tg.sent():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert "write the cover letter" in tg.sent()[0]["text"]  # says which session is asking
    manager.cancel(web)


def test_commands(settings):
    manager, bot, tg = make(settings, message(text("hi")))
    bot.handle(say("/stop"))
    assert tg.sent()[-1]["text"] == "Nothing is running."
    bot.handle(say("hello"))
    first = bot.session
    wait_idle(manager, first)

    bot.handle(say("/new"))
    assert bot.session is None
    bot.handle(say("/sessions"))
    button = tg.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"].startswith("hello")
    bot.handle(press(button["callback_data"]))
    assert bot.session == first

    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "cv.pdf").write_bytes(b"%PDF")
    bot.handle(say("/files"))
    bot.handle(press(tg.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]))
    assert [m for m, _ in tg.calls].count("sendDocument") == 1
    bot.handle(say("/whatever"))
    assert "/new" in tg.sent()[-1]["text"]


def test_spark_opens_a_session_you_reply_in(settings):
    manager, bot, tg = make(settings, message(text("Still on MLX?")), message(text("Nice.")))
    bot.handle(say("/spark"))
    assert "know what you like" in tg.sent()[-1]["text"]

    bot.handle(say("/interests"))
    assert tg.sent()[-1]["text"] == "No interests yet."
    manager.store.db.execute("INSERT INTO interests (topic) VALUES ('MLX')")
    bot.handle(say("/spark"))
    assert tg.sent()[-1]["text"] == "Still on MLX?"
    sid = bot.session
    assert manager.store.get(sid)["channel"] == "telegram"

    bot.handle(say("yes, 4-bit works"))
    wait_idle(manager, sid)
    sent = manager.client.requests[-1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"] == [{"type": "text", "text": "Still on MLX?"}]


def test_poll_reads_updates_and_advances_the_offset(settings):
    manager, bot, tg = make(settings)
    tg.updates = [say("/help")]
    stop = threading.Event()
    original = tg.__call__

    def stop_on_second_poll(request):
        if request.url.path.endswith("getUpdates") and any(m == "getUpdates" for m, _ in tg.calls):
            stop.set()
        return original(request)

    bot.api.http = httpx2.Client(base_url="https://api.telegram.org/botTOKEN/",
                                 transport=httpx2.MockTransport(stop_on_second_poll))
    bot.poll(stop)
    polls = [p for m, p in tg.calls if m == "getUpdates"]
    assert polls[1]["offset"] == 2 and "/new" in tg.sent()[0]["text"]


def test_split_message():
    assert split_message("short") == ["short"]
    parts = split_message("line\n" * 2000, limit=4000)
    assert all(len(p) <= 4000 for p in parts) and "".join(p + "\n" for p in parts).count("line") == 2000
    assert split_message("x" * 9000) == ["x" * 4000, "x" * 4000, "x" * 1000]
