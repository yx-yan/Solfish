from __future__ import annotations

import pytest

from solfish.skills import SKILLS, Context, open_db
from solfish.tools import ToolRegistry


@pytest.fixture
def tools(settings) -> ToolRegistry:
    ctx = Context(settings=settings, db=open_db(settings))
    return ToolRegistry([t for load in SKILLS.values() for t in load(ctx).tools])


def test_every_tool_has_a_valid_definition(tools):
    for d in tools.definitions():
        assert d["description"] and d["input_schema"]["type"] == "object"


def test_unknown_tool(tools):
    r = tools.execute("nope", {})
    assert r.is_error and "Available" in r.content


def test_workspace_sandbox(tools, settings):
    assert tools.execute("write_file", {"path": "a/b.txt", "content": "hi"}).is_error is False
    assert tools.execute("read_file", {"path": "a/b.txt"}).content == "hi"
    escape = tools.execute("read_file", {"path": "../solfish.db"})
    assert escape.is_error and "outside" in escape.content


def test_memory_roundtrip(tools):
    tools.execute("remember", {"content": "Prefers Python over Java"})
    assert "Python" in tools.execute("recall", {"query": "python"}).content
    assert tools.execute("forget", {"id": 1}).is_error is False
    assert tools.execute("recall", {"query": "python"}).content == "No matches."


def test_job_pipeline(tools):
    tools.execute("add_application", {"company": "Acme", "role": "AI Engineer", "status": "applied"})
    tools.execute("add_application", {"company": "Globex", "role": "SWE"})
    r = tools.execute("update_application", {"id": 1, "status": "interview", "note": "Round 1 Fri"})
    assert "applied -> interview" in r.content

    active = tools.execute("list_applications", {}).content
    assert "Acme" in active and "Round 1 Fri" in active and "Globex" in active

    tools.execute("update_application", {"id": 2, "status": "rejected"})
    assert "Globex" not in tools.execute("list_applications", {}).content
    assert "Globex" in tools.execute("list_applications", {"status": "rejected"}).content


def test_job_status_is_validated(tools):
    r = tools.execute("add_application", {"company": "A", "role": "B", "status": "ghosted"})
    assert r.is_error


def test_update_missing_application(tools):
    r = tools.execute("update_application", {"id": 99, "status": "offer"})
    assert r.is_error and "list_applications" in r.content
