from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from solfish.config import Smtp
from solfish.skills import SKILLS, Context, content, open_db, research, resume
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


# --- resume ---------------------------------------------------------------------------------

@pytest.fixture
def resume_tools(settings, tmp_path, monkeypatch):
    repo = tmp_path / "resume"
    repo.mkdir()
    (repo / "resume-ntu-en.tex").write_text(r"\documentclass{resume-ntu}\name{Yan}", encoding="utf-8")
    (repo / "resume-ntu.cls").write_text("% class", encoding="utf-8")
    smtp = Smtp(host="smtp.test", port=465, user="me", password="pw", sender="me@test")
    s = replace(settings, resume_dir=repo, smtp=smtp)

    def fake_latex(workdir, filename):
        assert (workdir / "resume-ntu.cls").exists()  # built inside a copy of the repo
        stem = filename.removesuffix(".tex")
        pages = 2 if "LONG" in (workdir / filename).read_text() else 1
        (workdir / f"{stem}.pdf").write_bytes(b"%PDF")
        return f"Output written on {stem}.pdf ({pages} page{'s' * (pages > 1)}, 1234 bytes)."

    sent = []
    monkeypatch.setattr(resume, "run_latex", fake_latex)
    monkeypatch.setattr(resume, "send_mail", lambda smtp, msg: sent.append(msg))
    ctx = Context(settings=s, db=open_db(s))
    registry = ToolRegistry([t for name in ("jobs", "resume") for t in SKILLS[name](ctx).tools])
    return registry, s, sent


def test_resume_needs_config(tools):
    r = tools.execute("read_base_resume", {})
    assert r.is_error and "SOLFISH_RESUME_DIR" in r.content


def test_build_resume(resume_tools):
    tools, s, _ = resume_tools
    assert "documentclass" in tools.execute("read_base_resume", {}).content
    r = tools.execute("build_resume", {"name": "Acme AI Engineer", "tex": "short"})
    assert "resumes/acme-ai-engineer.pdf (1 page)" in r.content
    assert (s.workspace / "resumes" / "acme-ai-engineer.pdf").exists()
    assert "one page" in tools.execute("build_resume", {"name": "acme", "tex": "LONG"}).content


def test_send_application_marks_applied(resume_tools):
    tools, s, sent = resume_tools
    tools.execute("add_application", {"company": "Acme", "role": "AI Engineer"})
    tools.execute("build_resume", {"name": "acme", "tex": "short"})
    r = tools.execute("send_application", {
        "application_id": 1, "to": "jobs@acme.test", "subject": "Application",
        "body": "Hello", "attachments": ["resumes/acme.pdf"],
    })
    assert not r.is_error, r.content
    assert sent[0]["To"] == "jobs@acme.test" and len(list(sent[0].iter_attachments())) == 1
    listed = tools.execute("list_applications", {}).content
    assert "applied" in listed and "Emailed application" in listed


def test_send_application_refuses_escape(resume_tools):
    tools, _, sent = resume_tools
    tools.execute("add_application", {"company": "Acme", "role": "AI Engineer"})
    r = tools.execute("send_application", {
        "application_id": 1, "to": "a@b.test", "subject": "x", "body": "x",
        "attachments": ["../solfish.db"],
    })
    assert r.is_error and not sent


# --- content --------------------------------------------------------------------------------

def test_post_limits(tools):
    r = tools.execute("draft_post", {"platform": "xiaohongshu", "title": "长" * 21, "body": "x"})
    assert r.is_error and "20" in r.content
    assert tools.execute("draft_post", {"platform": "x", "body": "a" * 281}).is_error


def test_draft_publish_to_outbox(tools, settings):
    (settings.workspace / "cover.png").write_bytes(b"png")
    r = tools.execute("draft_post", {
        "platform": "xiaohongshu", "title": "盐鱼鲜的第一篇", "body": "正文", "media": ["cover.png"],
    })
    assert "#1" in r.content
    r = tools.execute("publish_post", {"id": 1})
    assert not r.is_error, r.content
    out = settings.workspace / "outbox" / "xiaohongshu" / "1-盐鱼鲜的第一篇"
    assert (out / "post.md").read_text(encoding="utf-8").startswith("# 盐鱼鲜的第一篇")
    assert (out / "cover.png").exists()
    assert tools.execute("publish_post", {"id": 1}).is_error  # no double publishing
    assert tools.execute("edit_post", {"id": 1, "body": "改"}).is_error


def test_scheduled_posts_publish_when_due(tools, settings):
    tools.execute("draft_post", {"platform": "x", "body": "early"})
    tools.execute("draft_post", {"platform": "x", "body": "later"})
    tools.execute("schedule_post", {"id": 1, "at": "2026-11-01T09:00"})
    tools.execute("schedule_post", {"id": 2, "at": "2026-11-02T09:00"})
    db = open_db(settings)
    lines = content.publish_due(settings, db, now=datetime(2026, 11, 1, 12, 0))
    assert len(lines) == 1 and lines[0].startswith("#1 published")
    assert "scheduled" in tools.execute("list_posts", {"status": "scheduled"}).content


def test_failed_publisher_is_recorded(tools, monkeypatch):
    def broken(settings, post):
        raise RuntimeError("rate limited")

    monkeypatch.setitem(content.PUBLISHERS, "x", broken)
    tools.execute("draft_post", {"platform": "x", "body": "hi"})
    assert tools.execute("publish_post", {"id": 1}).is_error
    assert "rate limited" in tools.execute("list_posts", {"status": "failed"}).content


# --- research -------------------------------------------------------------------------------

FEED = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/abs/2310.11511v2</id><title>Self-RAG:  Learning to
Retrieve</title><published>2023-10-17T00:00:00Z</published>
<summary>Retrieve, generate and critique.</summary>
<author><name>Akari Asai</name></author><author><name>Zeqiu Wu</name></author>
</entry></feed>"""

PAGE = """<html><head><style>x{}</style></head><body><nav>menu</nav>
<h1>Self-RAG</h1><p>We study <math alttext="p(y|x)">...</math> retrieval.</p>
<script>evil()</script></body></html>"""


@pytest.fixture
def arxiv(monkeypatch):
    urls = []

    def fake_fetch(url):
        urls.append(url)
        return PAGE if "/html/" in url else FEED

    monkeypatch.setattr(research, "fetch", fake_fetch)
    return urls


def test_search_papers(tools, arxiv):
    r = tools.execute("search_papers", {"query": "self rag", "sort": "newest"}).content
    assert "[2310.11511] Self-RAG: Learning to Retrieve (2023-10-17)" in r
    assert "Akari Asai, Zeqiu Wu" in r and "sortBy=submittedDate" in arxiv[0]


def test_read_paper_strips_page_chrome(tools, arxiv):
    r = tools.execute("read_paper", {"arxiv_id": "arXiv:2310.11511"}).content
    assert "page 1/1" in r and "$p(y|x)$" in r
    assert "menu" not in r and "evil" not in r
    assert tools.execute("read_paper", {"arxiv_id": "rm -rf"}).is_error


def test_paper_library(tools, arxiv):
    assert not tools.execute("save_paper", {"arxiv_id": "2310.11511", "note": "for EE6405"}).is_error
    assert tools.execute("save_paper", {"arxiv_id": "2310.11511"}).is_error  # no duplicates
    tools.execute("note_paper", {"arxiv_id": "2310.11511", "note": "critique tokens", "status": "read"})
    listed = tools.execute("list_papers", {"query": "critique"}).content
    assert "Self-RAG" in listed and "| read" in listed and "for EE6405" in listed
    assert tools.execute("list_papers", {"status": "to_read"}).content == "No papers match."
