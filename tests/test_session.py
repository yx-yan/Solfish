from __future__ import annotations

import threading

import pytest
from conftest import FakeClient, message, text, tool_use

from solfish.app import build_agent
from solfish.session import Approvals, Busy, SessionManager, SessionStore, to_jsonable
from solfish.skills import open_db


@pytest.fixture
def store(settings) -> SessionStore:
    return SessionStore(open_db(settings))


def test_restored_session_continues_the_same_history(settings, store):
    store.create("s1", "cli")
    client = FakeClient(
        message({"type": "thinking", "thinking": "need the time", "signature": "sig"},
                tool_use("current_time", {}), stop="tool_use"),
        message(text("It's late.")),
    )
    agent, _ = build_agent(settings, client=client, trace=False)
    agent.run("what time is it?")
    store.sync("s1", agent.messages)
    store.sync("s1", agent.messages)  # idempotent: only the tail is ever written

    later = FakeClient(message(text("Still late.")))
    restored, _ = build_agent(settings, client=later, trace=False, messages=store.load("s1"))
    restored.run("and now?")
    before = [{"role": m["role"], "content": to_jsonable(m["content"])} for m in agent.messages]
    assert later.requests[0]["messages"][: len(before)] == before  # thinking + signature intact
    assert store.get("s1")["title"] == "what time is it?"


def test_dangling_call_in_a_restored_session_is_closed(settings, store):
    store.create("s1", "cli")
    store.sync("s1", [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": message(tool_use("current_time", {}), stop="tool_use").content},
    ])
    agent, _ = build_agent(settings, client=FakeClient(message(text("ok"))), trace=False,
                           messages=store.load("s1"))
    agent.run("still there?")
    assert agent.messages[2]["content"][0]["tool_use_id"] == "toolu_1"


def test_cancel_stops_before_the_next_tool_batch(settings):
    client = FakeClient(
        message(tool_use("write_file", {"path": "x.txt", "content": "hi"}), stop="tool_use"),
        message(text("ok")),
    )
    agent, _ = build_agent(settings, client=client, trace=False, approve=lambda n, a: True)
    agent.listeners.append(lambda kind, data: kind == "llm_call" and agent.cancel.set())
    assert agent.run("write x").stop == "interrupted"
    assert not (settings.workspace / "x.txt").exists()

    agent.listeners.pop()
    assert agent.run("never mind").stop == "end_turn"
    assert agent.messages[2]["content"][0]["is_error"]  # the stopped call got an answer


def test_approvals_allow_timeout_and_unknown():
    approvals = Approvals(timeout_s=5)
    seen = []

    def notify(kind, **data):
        seen.append(kind)
        if kind == "approval_request":
            threading.Thread(target=approvals.resolve, args=(data["id"], True)).start()

    assert approvals.request("s1", "write_file", {}, notify)
    assert seen == ["approval_request", "approval_resolved"]
    assert not Approvals(timeout_s=0.01).request("s1", "write_file", {}, lambda *a, **k: None)
    assert not approvals.resolve("nope", True)


def test_manager_runs_turns_and_broadcasts(settings):
    client = FakeClient(
        message(tool_use("write_file", {"path": "x.txt", "content": "hi"}), stop="tool_use"),
        message(text("Wrote it.")),
    )
    manager = SessionManager(settings, client=client, trace=False)
    events = []

    def listen(session_id, kind, data):
        events.append((session_id, kind))
        if kind == "approval_request":  # another channel answers
            manager.approvals.resolve(data["id"], True)

    manager.subscribe(listen)
    sid = manager.create("web")
    manager.send(sid, "write x").join(timeout=5)

    kinds = [k for s, k in events if s == sid]
    assert kinds[0] == "user_message" and kinds[-1] == "turn_end"
    assert "approval_request" in kinds
    assert (settings.workspace / "x.txt").read_text() == "hi"
    assert len(manager.store.load(sid)) == 4  # user, tool call, tool result, answer
    assert manager.totals(sid).tool_calls == 1


def test_manager_one_turn_at_a_time_and_errors_are_events(settings):
    manager = SessionManager(settings, client=FakeClient(), trace=False, approval_timeout_s=5)
    errors = []
    manager.subscribe(lambda s, kind, data: kind == "error" and errors.append(data["message"]))
    sid = manager.create("web")

    gate = threading.Event()
    agent, _ = manager._agent(sid)
    agent.listeners.insert(0, lambda kind, data: kind == "user_message" and gate.wait(5))
    t = manager.send(sid, "hi")
    with pytest.raises(Busy):
        manager.send(sid, "again")
    gate.set()
    t.join(timeout=5)

    assert errors and "IndexError" in errors[0]  # the fake client had nothing scripted
    assert not manager.busy(sid)
    with pytest.raises(KeyError):
        manager.send("no-such-session", "hi")
