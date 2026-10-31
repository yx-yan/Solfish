from __future__ import annotations

import os

from conftest import FakeClient, message, text, tool_use

from solfish.app import build_agent
from solfish.config import Settings, load_dotenv


def make(settings, client, **kw):
    agent, tracer = build_agent(settings, client=client, trace=False, **kw)
    return agent, tracer


def test_plain_answer(settings):
    client = FakeClient(message(text("Hello!")))
    agent, _ = make(settings, client)
    result = agent.run("hi")
    assert (result.text, result.stop, result.steps) == ("Hello!", "end_turn", 1)


def test_tool_loop_executes_and_feeds_back_results(settings):
    client = FakeClient(
        message(tool_use("add_application", {"company": "Acme", "role": "ML Engineer"}),
                stop="tool_use"),
        message(text("Tracked it.")),
    )
    agent, tracer = make(settings, client)
    result = agent.run("track Acme ML Engineer")

    assert result.text == "Tracked it." and result.steps == 2
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "toolu_1"
    assert not tool_result["is_error"]
    assert "Acme" in tool_result["content"]
    assert tracer.totals.llm_calls == 2 and tracer.totals.tool_calls == 1


def test_parallel_tool_calls_return_in_one_message(settings):
    client = FakeClient(
        message(tool_use("current_time", {}, id="a"), tool_use("list_files", {"path": "."}, id="b"),
                stop="tool_use"),
        message(text("done")),
    )
    agent, _ = make(settings, client)
    agent.run("go")
    results = client.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


def test_bad_arguments_become_error_result_not_crash(settings):
    client = FakeClient(
        message(tool_use("add_application", {"company": "Acme"}), stop="tool_use"),
        message(text("Sorry, which role?")),
    )
    agent, _ = make(settings, client)
    agent.run("track Acme")
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] and "role" in tool_result["content"]
    assert tool_result["content"].startswith("Error: ")  # DeepSeek ignores is_error


def test_side_effecting_tool_respects_approval(settings):
    client = FakeClient(
        message(tool_use("write_file", {"path": "x.txt", "content": "hi"}), stop="tool_use"),
        message(text("ok")),
    )
    agent, _ = make(settings, client, approve=lambda name, args: False)
    agent.run("write x")
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] and "declined" in tool_result["content"]
    assert not (settings.workspace / "x.txt").exists()


def test_step_limit(settings):
    loop = [message(tool_use("current_time", {}, id=f"t{i}"), stop="tool_use") for i in range(5)]
    agent, _ = make(settings, FakeClient(*loop))
    result = agent.run("loop forever")
    assert result.stop == "step_limit" and result.steps == settings.max_steps


def test_refusal(settings):
    agent, _ = make(settings, FakeClient(message(stop="refusal")))
    assert agent.run("...").stop == "refusal"


def test_history_is_append_only_across_turns(settings):
    client = FakeClient(message(text("one")), message(text("two")))
    agent, _ = make(settings, client)
    agent.run("first")
    agent.run("second")
    first, second = client.requests[0]["messages"], client.requests[1]["messages"]
    assert second[: len(first)] == first  # earlier turns are never rewritten


def test_interrupted_tool_call_is_closed_before_next_turn(settings):
    agent, _ = make(settings, FakeClient(message(text("ok"))))
    agent.messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": message(tool_use("current_time", {}), stop="tool_use").content},
    ]
    agent.run("still there?")
    closing = agent.messages[2]["content"][0]
    assert closing["tool_use_id"] == "toolu_1" and closing["is_error"]


def test_request_shape(settings):
    client = FakeClient(message(text("hi")))
    agent, _ = make(settings, client)
    agent.run("hi")
    req = client.requests[0]
    assert req["model"] == "deepseek-flash"
    assert req["thinking"]["type"] == "adaptive"
    assert "fallbacks" not in req
    names = [t["name"] for t in req["tools"]]
    assert names == sorted(names)  # deterministic order keeps the prompt cache valid


def test_fallbacks_only_when_enabled(tmp_path):
    settings = Settings(home=tmp_path / "home", max_steps=5, fallbacks=True)
    client = FakeClient(message(text("hi")))
    agent, _ = make(settings, client)
    agent.run("hi")
    assert client.requests[0]["fallbacks"] == "default"


def test_load_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nA_KEY=from-file\nexport B_KEY="quoted"\nC_KEY=\nD_KEY=x=y\nSHELL_KEY=file\n')
    monkeypatch.setattr(os, "environ", {"SHELL_KEY": "shell"})  # restored after the test
    load_dotenv(env)
    assert os.environ["A_KEY"] == "from-file" and os.environ["B_KEY"] == "quoted"
    assert "C_KEY" not in os.environ  # empty means unset
    assert os.environ["D_KEY"] == "x=y"
    assert os.environ["SHELL_KEY"] == "shell"  # the shell wins
