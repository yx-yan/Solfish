from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta

import httpx2
import pytest
from conftest import FakeClient, message, text, tool_use

from solfish.channels.telegram import BotApi, Sparker, TelegramBot, split_message
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
    bot.spark(now=datetime(2026, 11, 9, 11, 0))  # at work, knowing nothing: nothing to say
    assert "don't know you" in tg.sent()[-1]["text"]

    bot.handle(say("/interests"))
    assert tg.sent()[-1]["text"] == "No interests yet."
    manager.store.db.execute("INSERT INTO interests (topic) VALUES ('MLX')")
    manager.store.db.commit()
    bot.handle(say("/spark"))
    assert tg.sent()[-1]["text"] == "Still on MLX?"
    sid = bot.session
    assert manager.store.get(sid)["channel"] == "telegram"

    bot.handle(say("yes, 4-bit works"))
    wait_idle(manager, sid)
    sent = manager.client.requests[-1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"] == [{"type": "text", "text": "Still on MLX?"}]


def test_sparker_sends_at_planned_times_and_backs_off(settings):
    settings = replace(settings, sparks_per_day=(3, 3), spark_hours=(9, 23))
    manager, bot, tg = make(settings, *(message(text(f"q{i}")) for i in range(3)))
    sparker = Sparker(bot, random.Random(7))
    manager.store.db.executescript("INSERT INTO interests (topic) VALUES ('MLX'), ('Rust');")
    day = datetime(2026, 11, 14, 8, 0)  # a Saturday
    assert not sparker.tick(day)  # plans the day, nothing due yet
    first, second, third = sparker.plan
    assert sparker.tick(first) and tg.sent()[-1]["text"] == "q0"
    # Missed times collapse into one, and an ignored spark doubles the wait.
    later = first + timedelta(minutes=80)
    sparker.plan = [first + timedelta(minutes=50), later]
    assert not sparker.tick(later) and sparker.plan == []
    assert len(tg.sent()) == 1

    sparker.plan = [first + timedelta(minutes=91)]
    assert sparker.tick(first + timedelta(minutes=91)) and tg.sent()[-1]["text"] == "q1"
    assert not sparker.tick(datetime(2026, 11, 15, 8, 0))  # a new day, a new plan
    assert 0 < len(sparker.plan) <= 3


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


def test_review_runs_test_builds_without_asking_and_you_reply_in_it(settings):
    manager, bot, tg = make(
        settings,
        message(tool_use("build_resume", {"name": "x", "tex": "y"}), stop="tool_use"),
        message(text("Proposed: add trawl.")),
        message(text("Kept the dissertation.")),
    )
    bot.handle(say("/review"))
    sid = bot.session
    wait_idle(manager, sid)
    assert "Résumé review" in tg.sent()[0]["text"]
    assert not [m for m in tg.sent() if "reply_markup" in m]  # no approval buttons
    first = manager.client.requests[0]["messages"][0]["content"]
    assert "Weekly résumé review" in first and "days=7" in first
    result = manager.client.requests[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and "declined" not in json.dumps(result)

    bot.handle(say("keep the dissertation section"))
    wait_idle(manager, sid)
    assert bot.session == sid and len(manager.client.requests[-1]["messages"]) == 5


def test_preapproval_stays_with_its_session(settings):
    manager, *_ = make(settings)
    review = manager.create("telegram", preapproved=frozenset({"build_resume"}))
    other = manager.create("telegram")
    approve_review = manager._agent(review)[0].approve
    assert approve_review("build_resume", {})
    assert manager._preapproved.get(other) is None


def test_reviewer_runs_weekly_and_catches_up(settings):
    from solfish.channels.telegram import Reviewer

    manager, bot, tg = make(settings, *(message(text(f"r{i}")) for i in range(3)))
    started = []
    bot.review = lambda days=7: started.append(days) or "s"
    reviewer = Reviewer(bot, (6, 20, 0))  # Sundays 20:00
    sat = datetime(2026, 11, 14, 12, 0)
    assert reviewer.slot(sat) == datetime(2026, 11, 8, 20, 0)
    assert not reviewer.tick(sat)  # first start: wait for the next Sunday
    assert not reviewer.tick(datetime(2026, 11, 15, 19, 59))
    assert reviewer.tick(datetime(2026, 11, 15, 20, 0)) and started == [7]
    assert not reviewer.tick(datetime(2026, 11, 15, 20, 1))  # once per slot
    # The server was down for two Sundays: one review, looking back over the gap
    assert reviewer.tick(datetime(2026, 11, 30, 9, 0)) and started == [7, 15]
    # Mid-conversation it waits
    bot.session = manager.create("telegram")
    manager._running[bot.session] = threading.current_thread()
    assert not reviewer.tick(datetime(2026, 12, 6, 20, 0))
    del manager._running[bot.session]
    assert reviewer.tick(datetime(2026, 12, 6, 20, 5)) and started[-1] == 7


def test_review_at_setting(monkeypatch):
    from solfish.config import _weekly

    monkeypatch.setenv("SOLFISH_REVIEW_AT", "Sun 20:00")
    assert _weekly("SOLFISH_REVIEW_AT") == (6, 20, 0)
    monkeypatch.setenv("SOLFISH_REVIEW_AT", "monday 9")
    assert _weekly("SOLFISH_REVIEW_AT") == (0, 9, 0)
    monkeypatch.setenv("SOLFISH_REVIEW_AT", "")
    assert _weekly("SOLFISH_REVIEW_AT") is None
    monkeypatch.setenv("SOLFISH_REVIEW_AT", "someday")
    with pytest.raises(ValueError):
        _weekly("SOLFISH_REVIEW_AT")
