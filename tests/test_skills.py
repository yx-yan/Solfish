from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from itertools import cycle
from pathlib import Path
from unittest.mock import MagicMock, call

import httpx2
import pytest

from solfish import cli
from solfish.config import BlogDrafts, Smtp, XAuth
from solfish.skills import SKILLS, Context, content, forms, open_db, reading, research, resume, web
from solfish.skills.publishers import blog, linkedin, x, xiaohongshu, zhihu
from solfish.tools import ToolError, ToolRegistry


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
    # A real git repo with a local bare repo as origin, so proposals push for real (offline).
    origin, repo = tmp_path / "origin.git", tmp_path / "resume"
    resume.git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))
    resume.git(tmp_path, "init", "--quiet", "-b", "main", str(repo))
    (repo / "resume-en.tex").write_text(r"\documentclass{resume-en}\name{Yan}", encoding="utf-8")
    (repo / "resume-zh.tex").write_text(r"\documentclass{resume-zh}\name{严}", encoding="utf-8")
    (repo / "resume-en.cls").write_text("% class", encoding="utf-8")
    (repo / "resume-en.pdf").write_bytes(b"%PDF old")
    (repo / ".gitignore").write_text("draft.tex\nassets/photo.jpg\n", encoding="utf-8")
    (repo / "draft.tex").write_text("private plans", encoding="utf-8")
    (repo / "assets").mkdir()
    (repo / "assets" / "photo.jpg").write_bytes(b"jpg")
    for args in (("config", "user.name", "Test"), ("config", "user.email", "test@test"),
                 ("add", "."), ("commit", "--quiet", "-m", "init"),
                 ("remote", "add", "origin", str(origin)), ("push", "--quiet", "origin", "main")):
        resume.git(repo, *args)
    smtp = Smtp(host="smtp.test", port=465, user="me", password="pw", sender="me@test")
    s = replace(settings, resume_dir=repo, smtp=smtp)

    def fake_latex(workdir, filename):
        assert (workdir / "resume-en.cls").exists()  # built inside a copy of the repo
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


def test_read_base_resume_only_reads_tracked_tex(resume_tools):
    tools, _, _ = resume_tools
    assert "严" in tools.execute("read_base_resume", {"file": "resume-zh.tex"}).content
    for name in ("draft.tex", "../resume/resume-en.tex", "resume-en.cls"):
        r = tools.execute("read_base_resume", {"file": name})
        assert r.is_error and "private plans" not in r.content


def origin_file(s, branch, path):
    return resume.git(s.resume_dir, "show", f"origin/{branch}:{path}")


def propose(tools, tex_en="\\name{Yan} Quoth", **extra):
    return tools.execute("propose_resume_change", {
        "name": "add-quoth", "title": "Add Quoth", "summary": "New project.",
        "changes": [{"path": "resume-en.tex", "content": tex_en},
                    {"path": "resume-zh.tex", "content": "\\name{严} Quoth"}],
        **extra,
    })


def test_propose_resume_change_pushes_a_branch(resume_tools):
    tools, s, _ = resume_tools
    r = propose(tools)
    assert not r.is_error, r.content
    assert "SOLFISH_GITHUB_TOKEN" in r.content or "isn't on GitHub" in r.content
    resume.git(s.resume_dir, "fetch", "--quiet", "origin")
    assert origin_file(s, "solfish/add-quoth", "resume-zh.tex") == "\\name{严} Quoth"
    assert origin_file(s, "solfish/add-quoth", "resume-en.pdf") == "%PDF"  # committed PDF rebuilt
    assert "Quoth" not in origin_file(s, "main", "resume-en.tex")  # main untouched
    assert "Quoth" not in (s.resume_dir / "resume-en.tex").read_text()  # so is the working copy
    files = resume.git(s.resume_dir, "ls-tree", "-r", "--name-only", "origin/solfish/add-quoth")
    assert "photo.jpg" not in files and "draft.tex" not in files
    assert resume.git(s.resume_dir, "worktree", "list").count("\n") == 0  # worktree cleaned up


def test_propose_again_revises_the_same_branch(resume_tools):
    tools, s, _ = resume_tools
    propose(tools)
    assert not propose(tools, tex_en="\\name{Yan} Quoth, revised").is_error
    log = resume.git(s.resume_dir, "log", "--format=%s", "origin/solfish/add-quoth")
    assert log.splitlines() == ["Add Quoth", "Add Quoth", "init"]


def test_propose_refuses_overflow_untracked_and_no_op(resume_tools):
    tools, s, _ = resume_tools
    assert "one page" in propose(tools, tex_en="LONG").content
    r = tools.execute("propose_resume_change", {
        "name": "x", "title": "x", "summary": "x",
        "changes": [{"path": "draft.tex", "content": "x"}],
    })
    assert r.is_error and "tracked" in r.content
    r = tools.execute("propose_resume_change", {
        "name": "x", "title": "x", "summary": "x",
        "changes": [{"path": "resume-en.tex", "content": r"\documentclass{resume-en}\name{Yan}"}],
    })
    assert r.is_error and "nothing to propose" in r.content
    assert not resume.git(s.resume_dir, "ls-remote", "--heads", "origin", "solfish/*")


def test_propose_opens_a_pull_request(resume_tools, monkeypatch):
    tools, s, _ = resume_tools
    opened = []
    monkeypatch.setattr(resume, "github_repo", lambda url: "yx-yan/resume")
    monkeypatch.setattr(resume, "open_pr", lambda *a: opened.append(a) or "https://github.com/pr/1")
    registry = ToolRegistry(SKILLS["resume"](
        Context(settings=replace(s, github_token="t"), db=open_db(s))).tools)
    r = propose(registry)
    assert "Opened https://github.com/pr/1" in r.content
    assert opened == [("t", "yx-yan/resume", "solfish/add-quoth", "Add Quoth", "New project.")]


@pytest.mark.parametrize("url", [
    "https://github.com/yx-yan/resume.git", "git@github.com:yx-yan/resume.git",
    "https://token@github.com/yx-yan/resume", "ssh://git@github.com/yx-yan/resume.git",
])
def test_github_repo_from_remote(url):
    assert resume.github_repo(url) == "yx-yan/resume"
    assert resume.github_repo("/tmp/origin.git") is None


# --- content --------------------------------------------------------------------------------

def test_post_limits(tools):
    r = tools.execute("draft_post", {"platform": "xiaohongshu", "title": "长" * 21, "body": "x"})
    assert r.is_error and "20" in r.content
    assert tools.execute("draft_post", {"platform": "x", "body": "a" * 281}).is_error


def test_x_counts_weighted_characters(tools):
    assert x.x_length("abc") == 3
    assert x.x_length("中文") == 4
    assert x.x_length("see https://example.com/a/very/long/path/indeed") == 4 + 23
    assert not tools.execute("draft_post", {"platform": "x", "body": "长" * 140}).is_error
    r = tools.execute("draft_post", {"platform": "x", "body": "长" * 141})
    assert r.is_error and "282" in r.content


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
    assert "handoff" in tools.execute("list_posts", {}).content
    assert tools.execute("publish_post", {"id": 1}).is_error  # no double publishing
    assert tools.execute("edit_post", {"id": 1, "body": "改"}).is_error
    assert not tools.execute("confirm_published", {"id": 1, "url": "https://xhs.test/1"}).is_error
    listed = tools.execute("list_posts", {"status": "published"}).content
    assert "https://xhs.test/1" in listed
    assert tools.execute("publish_post", {"id": 1}).is_error


def test_outbox_includes_tags_and_cover(tools, settings):
    (settings.workspace / "cover.png").write_bytes(b"png")
    tools.execute("draft_post", {
        "platform": "wechat", "title": "t", "body": "b", "tags": ["#AI", " agent "], "cover": "cover.png",
    })
    tools.execute("publish_post", {"id": 1})
    out = settings.workspace / "outbox" / "wechat" / "1-t"
    assert (out / "post.md").read_text(encoding="utf-8").endswith("#AI #agent\n")
    assert (out / "cover.png").exists()
    assert tools.execute("draft_post", {"platform": "wechat", "body": "b", "cover": "no.png"}).is_error


def test_scheduled_posts_publish_when_due(tools, settings):
    tools.execute("draft_post", {"platform": "weibo", "body": "early"})
    tools.execute("draft_post", {"platform": "weibo", "body": "later"})
    tools.execute("schedule_post", {"id": 1, "at": "2026-11-01T09:00"})
    tools.execute("schedule_post", {"id": 2, "at": "2026-11-02T09:00"})
    db = open_db(settings)
    lines = content.publish_due(settings, db, now=datetime(2026, 11, 1, 12, 0))
    assert len(lines) == 1 and lines[0].startswith("#1 ready")
    assert "scheduled" in tools.execute("list_posts", {"status": "scheduled"}).content


def test_failed_publisher_is_recorded(tools, monkeypatch):
    def broken(settings, db, post):
        raise RuntimeError("rate limited")

    monkeypatch.setitem(content.PUBLISHERS, "x", broken)
    tools.execute("draft_post", {"platform": "x", "body": "hi"})
    assert tools.execute("publish_post", {"id": 1}).is_error
    assert "rate limited" in tools.execute("list_posts", {"status": "failed"}).content


def test_api_publisher_publishes_for_real(tools, monkeypatch):
    monkeypatch.setitem(
        content.PUBLISHERS, "x", lambda settings, db, post: content.Delivery("https://x.test/1")
    )
    tools.execute("draft_post", {"platform": "x", "body": "hi"})
    assert tools.execute("publish_post", {"id": 1}).content == "#1 published: https://x.test/1"
    assert "published" in tools.execute("list_posts", {"status": "published"}).content


def test_a_post_is_only_published_once(tools, settings, monkeypatch):
    """A second run that starts while the first is talking to the platform gets nothing."""
    other_run, calls = [], []

    def slow(settings_, db, post):
        calls.append(post["id"])
        other_run.append(tools.execute("publish_post", {"id": post["id"]}))
        return content.Delivery("https://x.test/1")

    monkeypatch.setitem(content.PUBLISHERS, "x", slow)
    tools.execute("draft_post", {"platform": "x", "body": "hi"})
    tools.execute("schedule_post", {"id": 1, "at": "2026-11-01T09:00"})
    lines = content.publish_due(settings, open_db(settings), now=datetime(2026, 11, 1, 12, 0))
    assert calls == [1] and other_run[0].is_error and "being published" in other_run[0].content
    assert lines == ["#1 published: https://x.test/1"]


def test_unclear_result_is_not_retried(tools, monkeypatch):
    def timed_out(settings, db, post):
        raise content.MaybePublished("no answer")

    monkeypatch.setitem(content.PUBLISHERS, "x", timed_out)
    tools.execute("draft_post", {"platform": "x", "body": "hi"})
    r = tools.execute("publish_post", {"id": 1})
    assert r.is_error and "may have gone out" in r.content
    assert "publishing" in tools.execute("list_posts", {}).content
    assert tools.execute("publish_post", {"id": 1}).is_error
    assert tools.execute("schedule_post", {"id": 1, "at": "2026-11-01T09:00"}).is_error


def test_crash_while_publishing_leaves_the_post_for_a_person(tools, settings, monkeypatch):
    def killed(settings_, db, post):
        raise KeyboardInterrupt  # not an Exception: nothing records a failure, as in a real crash

    monkeypatch.setitem(content.PUBLISHERS, "x", killed)
    tools.execute("draft_post", {"platform": "x", "body": "hi"})
    tools.execute("schedule_post", {"id": 1, "at": "2026-11-01T09:00"})
    db = open_db(settings)
    with pytest.raises(KeyboardInterrupt):
        content.publish_due(settings, db, now=datetime(2026, 11, 1, 12, 0))
    assert content.publish_due(settings, db, now=datetime(2026, 11, 1, 12, 30)) == []  # no retry
    assert content.stuck_posts(db) == []  # too recent: could still be a run in progress
    db.execute("UPDATE posts SET updated_at = datetime('now', 'localtime', '-1 hour')")
    db.commit()
    assert "#1 stuck" in content.stuck_posts(db)[0]
    assert tools.execute("edit_post", {"id": 1, "body": "new"}).is_error
    assert not tools.execute("reset_post", {"id": 1}).is_error
    assert "draft" in tools.execute("list_posts", {}).content


def test_editing_a_scheduled_post_needs_approval_again(tools, settings):
    tools.execute("draft_post", {"platform": "x", "body": "approved text"})
    tools.execute("schedule_post", {"id": 1, "at": "2026-11-01T09:00"})
    r = tools.execute("edit_post", {"id": 1, "body": "something else"})
    assert "schedule" in r.content
    assert content.publish_due(settings, open_db(settings), now=datetime(2026, 11, 2)) == []
    assert "draft" in tools.execute("list_posts", {}).content


def test_approval_shows_the_stored_post(tools):
    tools.execute("draft_post", {"platform": "x", "body": "正文在这里", "tags": ["AI"]})
    args = {"id": 1, "at": "2026-11-01T20:00"}
    shown = tools.get("schedule_post").approval_args(args)
    assert shown["id"] == 1
    assert "正文在这里" in shown["preview"] and "#AI" in shown["preview"]
    assert "2026-11-01 20:00" in shown["preview"]
    assert "正文在这里" in tools.get("publish_post").approval_args({"id": 1})["preview"]
    assert tools.get("publish_post").approval_args({"id": 9}) == {"id": 9}  # fails when it runs


X_AUTH = XAuth("key", "secret", "token", "token-secret")


def x_server(respond):
    """A fake api.x.com: records requests, answers with respond(request)."""
    requests = []

    def handler(request):
        requests.append(request)
        return respond(request)

    return requests, httpx2.MockTransport(handler)


def x_post(db, **fields):
    fields = {"platform": "x", "body": "hello", **fields}
    cur = db.execute(
        f"INSERT INTO posts ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
        tuple(fields.values()),
    )
    return db.execute("SELECT * FROM posts WHERE id = ?", (cur.lastrowid,)).fetchone()


@pytest.fixture
def x_db(settings):
    db = open_db(settings)
    content.migrate(db)
    return db


def test_x_oauth_signature_matches_the_published_example():
    """The worked example from X's "Creating a signature" guide."""
    auth = XAuth(
        "xvz1evFS4wEEPTGEFPHBog", "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
    )
    header = x.oauth_header(
        auth, "POST", "https://api.twitter.com/1.1/statuses/update.json",
        {"include_entities": "true", "status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", timestamp="1318622958",
    )
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in header


def test_x_posts_text_with_tags(settings, x_db):
    requests, transport = x_server(lambda r: httpx2.Response(201, json={"data": {"id": "42"}}))
    done = x.publish(replace(settings, x=X_AUTH), x_db, x_post(x_db, tags="AI\nagent"), transport)
    assert done == content.Delivery("https://x.com/i/status/42")
    (r,) = requests
    assert str(r.url) == "https://api.x.com/2/tweets"
    assert json.loads(r.content) == {"text": "hello\n\n#AI #agent"}
    assert r.headers["authorization"].startswith("OAuth ")


def test_x_uploads_images_first(settings, x_db):
    settings.ensure_dirs()
    (settings.workspace / "a.png").write_bytes(b"png")

    def respond(request):
        if request.url.path.endswith("/media/upload"):
            return httpx2.Response(200, json={"data": {"id": "7"}})
        return httpx2.Response(201, json={"data": {"id": "42"}})

    requests, transport = x_server(respond)
    x.publish(replace(settings, x=X_AUTH), x_db, x_post(x_db, media="a.png"), transport)
    upload, create = requests
    assert b"tweet_image" in upload.content and b"png" in upload.content
    assert json.loads(create.content)["media"] == {"media_ids": ["7"]}


def test_x_refuses_what_it_cannot_send(settings, x_db):
    settings.ensure_dirs()
    (settings.workspace / "clip.mp4").write_bytes(b"mp4")
    requests, transport = x_server(lambda r: httpx2.Response(500))
    ready = replace(settings, x=X_AUTH)
    with pytest.raises(ToolError, match="SOLFISH_X_"):
        x.publish(replace(settings, x=XAuth(None, None, None, None)), x_db, x_post(x_db), transport)
    with pytest.raises(ToolError, match="only uploads images"):
        x.publish(ready, x_db, x_post(x_db, media="clip.mp4"), transport)
    with pytest.raises(ToolError, match="tags"):
        x.publish(ready, x_db, x_post(x_db, body="a" * 278, tags="AI"), transport)
    assert requests == []


def test_x_error_and_timeout(settings, x_db):
    ready = replace(settings, x=X_AUTH)
    _, transport = x_server(lambda r: httpx2.Response(403, text="duplicate content"))
    with pytest.raises(ToolError, match="403.*duplicate"):
        x.publish(ready, x_db, x_post(x_db), transport)

    def hang(request):
        raise httpx2.ReadTimeout("slow", request=request)

    _, transport = x_server(hang)
    with pytest.raises(content.MaybePublished):
        x.publish(ready, x_db, x_post(x_db), transport)


def test_x_approval_warns_about_links(tools):
    tools.execute("draft_post", {"platform": "x", "body": "read https://yuxuanyan.com"})
    assert "link" in tools.get("publish_post").approval_args({"id": 1})["preview"]


def xhs_page(url=xiaohongshu.PUBLISH_URL):
    page = MagicMock(url=url)
    tabs = page.locator.return_value.filter.return_value.locator.return_value
    tabs.count.return_value = 2
    tabs.nth.return_value.bounding_box.side_effect = cycle([{"x": -9999, "y": 0}, {"x": 10, "y": 10}])
    return page


def xhs_post(tools, settings):
    (settings.workspace / "a.png").write_bytes(b"png")
    (settings.workspace / "b.png").write_bytes(b"png")
    tools.execute("draft_post", {
        "platform": "xiaohongshu", "title": "标题", "body": "第一段\n第二段", "media": ["b.png"],
        "cover": "a.png", "tags": ["AI"],
    })


def test_xiaohongshu_goes_to_handoff_with_a_command(tools, settings):
    xhs_post(tools, settings)
    r = tools.execute("publish_post", {"id": 1})
    assert "solfish handoff 1" in r.content
    assert "handoff" in tools.execute("list_posts", {}).content


def test_xiaohongshu_needs_an_image(tools):
    tools.execute("draft_post", {"platform": "xiaohongshu", "title": "t", "body": "b"})
    r = tools.execute("publish_post", {"id": 1})
    assert r.is_error and "at least one image" in r.content


def test_xiaohongshu_fill_never_reaches_for_publish(tools, settings):
    xhs_post(tools, settings)
    page = xhs_page()
    post = open_db(settings).execute("SELECT * FROM posts").fetchone()
    xiaohongshu.fill(page, settings, post)

    assert page.goto.call_count == 1
    tabs = page.locator.return_value.filter.return_value.locator.return_value
    assert tabs.nth.call_args_list[-2:] == [call(1), call(1)]  # the one on screen, then its click
    uploads = page.locator.return_value.first.set_input_files.call_args_list
    assert [c.args[0].rsplit("/", 1)[1] for c in uploads] == ["a.png", "b.png"]  # cover first
    page.locator.return_value.fill.assert_called_once_with("标题")
    assert [c.args[0] for c in page.keyboard.insert_text.call_args_list] == ["第一段", "第二段"]
    assert call("#AI") in page.keyboard.type.call_args_list

    used = {c.args[0] for c in page.locator.call_args_list}
    assert used == {
        xiaohongshu.TAB, xiaohongshu.FIRST_FILE_INPUT, xiaohongshu.NEXT_FILE_INPUT,
        xiaohongshu.IMAGE_PREVIEW, xiaohongshu.TITLE, xiaohongshu.BODY,
        xiaohongshu.TOPIC_SUGGESTION, xiaohongshu.TOPIC_NAME,
    }
    source = Path(xiaohongshu.__file__).read_text(encoding="utf-8")
    assert "xhs-publish-btn" not in source and "publish-btn" not in source and "bg-red" not in source


def test_xiaohongshu_waits_for_login(tools, settings):
    xhs_post(tools, settings)
    page = xhs_page("https://creator.xiaohongshu.com/login")
    post = open_db(settings).execute("SELECT * FROM posts").fetchone()
    xiaohongshu.fill(page, settings, post)
    page.wait_for_url.assert_called_once()
    assert page.goto.call_count == 2


def test_handoff_records_what_the_user_says(tools, settings, monkeypatch):
    xhs_post(tools, settings)
    tools.execute("publish_post", {"id": 1})
    page = xhs_page()
    monkeypatch.setattr(forms, "open_browser", lambda settings_, profile: page)
    answers = iter(["n", "y", "https://xhs.test/1"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    assert cli.handoff(settings, 1) == 0
    assert "handoff" in tools.execute("list_posts", {}).content  # not published yet
    assert cli.handoff(settings, 1) == 0
    assert "https://xhs.test/1" in tools.execute("list_posts", {"status": "published"}).content
    assert cli.handoff(settings, 1) == 1  # nothing left to hand off


def test_handoff_stops_at_the_first_surprise(tools, settings, monkeypatch, capsys):
    xhs_post(tools, settings)
    tools.execute("publish_post", {"id": 1})
    page = MagicMock(url=xiaohongshu.PUBLISH_URL)
    page.wait_for_selector.side_effect = RuntimeError("Timeout 30000ms exceeded\nlong log")
    monkeypatch.setattr(forms, "open_browser", lambda settings_, profile: page)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    assert cli.handoff(settings, 1) == 0
    assert page.goto.call_count == 1 and page.wait_for_selector.call_count == 1
    out = capsys.readouterr().out
    assert "Timeout 30000ms exceeded" in out and "long log" not in out
    page.wait_for_event.assert_called_once()  # the browser stays for the user
    assert "handoff" in tools.execute("list_posts", {}).content


BLOG = BlogDrafts("https://db.test", "pub-key", "draft-token", "https://site.test")


def test_blog_adds_a_draft_for_the_user_to_publish(settings, x_db):
    requests, transport = x_server(lambda r: httpx2.Response(200, json="uuid-1"))
    post = x_post(x_db, platform="blog", title="手写 agent", body="正文", tags="ai\n中文标签")
    done = blog.publish(replace(settings, blog=BLOG), x_db, post, transport)
    assert not done.final
    assert done.location == "https://site.test/admin/articles/uuid-1"
    (r,) = requests
    assert str(r.url) == "https://db.test/rest/v1/rpc/create_article_draft"
    assert r.headers["apikey"] == "pub-key"
    assert json.loads(r.content) == {
        "p_token": "draft-token", "p_slug": f"solfish-{post['id']}", "p_locale": "zh",
        "p_title": "手写 agent", "p_body_md": "正文", "p_tags": ["ai"],
    }


def test_blog_failures(settings, x_db):
    ready = replace(settings, blog=BLOG)
    post = x_post(x_db, platform="blog", title="Hello", body="text")
    assert blog.locale(post) == "en"
    with pytest.raises(ToolError, match="SOLFISH_BLOG_"):
        blog.publish(replace(settings, blog=BlogDrafts(None, None, None)), x_db, post)
    with pytest.raises(ToolError, match="needs a title"):
        blog.publish(ready, x_db, x_post(x_db, platform="blog", title=""))
    _, transport = x_server(lambda r: httpx2.Response(409, json={"code": "23505"}))
    with pytest.raises(ToolError, match="already has a draft"):
        blog.publish(ready, x_db, post, transport)

    def hang(request):
        raise httpx2.ReadTimeout("slow", request=request)

    _, transport = x_server(hang)
    with pytest.raises(content.MaybePublished):
        blog.publish(ready, x_db, post, transport)


def linkedin_api(request):
    if request.url.path == "/v2/userinfo":
        return httpx2.Response(200, json={"sub": "abc"})
    if request.url.path == "/rest/images":
        return httpx2.Response(200, json={"value": {
            "uploadUrl": "https://upload.test/1", "image": "urn:li:image:1",
        }})
    if request.url.host == "upload.test":
        return httpx2.Response(201)
    return httpx2.Response(201, headers={"x-restli-id": "urn:li:share:9"})


def test_linkedin_escapes_reserved_characters():
    assert linkedin.escape("a (b) #tag # c_d") == "a \\(b\\) #tag \\# c\\_d"


def test_linkedin_posts_text(settings, x_db):
    requests, transport = x_server(linkedin_api)
    ready = replace(settings, linkedin_token="tok")
    done = linkedin.publish(ready, x_db, x_post(x_db, body="hi (there)", tags="AI"), transport)
    assert done == content.Delivery("https://www.linkedin.com/feed/update/urn:li:share:9")
    _, create = requests
    assert create.headers["x-restli-protocol-version"] == "2.0.0"
    assert create.headers["linkedin-version"] == ready.linkedin_version
    body = json.loads(create.content)
    assert body["author"] == "urn:li:person:abc"
    assert body["commentary"] == "hi \\(there\\)\n\n#AI"
    assert "content" not in body


def test_linkedin_uploads_one_image(settings, x_db):
    settings.ensure_dirs()
    (settings.workspace / "a.png").write_bytes(b"png")
    (settings.workspace / "b.png").write_bytes(b"png")
    requests, transport = x_server(linkedin_api)
    ready = replace(settings, linkedin_token="tok")
    linkedin.publish(ready, x_db, x_post(x_db, media="a.png", title="alt"), transport)
    _, init, upload, create = requests
    assert json.loads(init.content)["initializeUploadRequest"]["owner"] == "urn:li:person:abc"
    assert upload.method == "PUT" and upload.content == b"png"
    assert upload.headers["authorization"] == "Bearer tok"
    assert json.loads(create.content)["content"]["media"]["id"] == "urn:li:image:1"
    with pytest.raises(ToolError, match="one image"):
        linkedin.publish(ready, x_db, x_post(x_db, media="a.png\nb.png"), transport)


def test_linkedin_says_when_the_token_has_expired(settings, x_db):
    with pytest.raises(ToolError, match="SOLFISH_LINKEDIN_TOKEN"):
        linkedin.publish(settings, x_db, x_post(x_db))
    _, transport = x_server(lambda r: httpx2.Response(401))
    with pytest.raises(ToolError, match="60 days"):
        linkedin.publish(replace(settings, linkedin_token="old"), x_db, x_post(x_db), transport)


def test_zhihu_goes_to_handoff_and_needs_a_title(tools):
    tools.execute("draft_post", {"platform": "zhihu", "title": "标题", "body": "正文"})
    tools.execute("draft_post", {"platform": "zhihu", "body": "正文"})
    assert "solfish handoff 1" in tools.execute("publish_post", {"id": 1}).content
    r = tools.execute("publish_post", {"id": 2})
    assert r.is_error and "needs a title" in r.content


def test_zhihu_fill_never_reaches_for_publish(tools, settings):
    (settings.workspace / "c.png").write_bytes(b"png")
    tools.execute("draft_post", {"platform": "zhihu", "title": "标题", "body": "正文", "cover": "c.png"})
    page = MagicMock()
    zhihu.fill(page, settings, open_db(settings).execute("SELECT * FROM posts").fetchone())
    page.locator.return_value.fill.assert_called_once_with("标题")
    assert page.evaluate.call_args.args[1] == "正文"
    page.keyboard.press.assert_called_once_with("ControlOrMeta+V")
    used = {c.args[0] for c in page.locator.call_args_list}
    assert used == {zhihu.TITLE, zhihu.COVER_INPUT, zhihu.BODY}


def test_old_posts_table_is_migrated(settings):
    db = open_db(settings)
    db.executescript("""
        CREATE TABLE posts (id INTEGER PRIMARY KEY, platform TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, media TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft', scheduled_at TEXT, published_at TEXT,
            location TEXT, error TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')));
        INSERT INTO posts (platform, body) VALUES ('x', 'old');
    """)
    content.migrate(db)
    content.migrate(db)  # safe to run on every start
    row = db.execute("SELECT * FROM posts").fetchone()
    assert row["body"] == "old" and row["tags"] == "" and row["cover"] == ""


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


# --- forms ----------------------------------------------------------------------------------

class FakeField:
    def __init__(self, type, label, options=(), readonly=False):
        self.type, self.label, self.options, self.readonly = type, label, list(options), readonly
        self.value = "false" if type in ("checkbox", "radio") else ""

    def fill(self, value, timeout=None):
        if self.readonly:
            raise TimeoutError(f'Locator.fill: Timeout exceeded.\nCall log:\n  - fill("{value}")')
        self.value = value

    def click(self, timeout=None):
        self.clicks = getattr(self, "clicks", 0) + 1

    def set_checked(self, checked):
        self.value = str(checked).lower()

    def select_option(self, value):
        self.value = next(t for t, v in self.options if v == value)

    def evaluate(self, js, arg=None):
        if js is forms.SET_JS:
            self.value = arg
            return None
        return self.options if js is forms.OPTIONS_JS else self.value


class FakePage:
    url = "https://form.test/arrival"

    def __init__(self):
        self.fields = [
            FakeField("text", "Full Name"),
            FakeField("text", "Passport No."),
            FakeField("select", "Nationality", [("Select", ""), ("CHINA", "CHN"), ("SINGAPORE", "SGP")]),
            FakeField("checkbox", "I declare the above is true"),
            FakeField("text", "DD/MM/YYYY", readonly=True),
            FakeField("button", "Next"),
            FakeField("button", "Submit"),
            FakeField("button", "Hantar / 提交"),
        ]
        self.visited = []

    def is_closed(self):
        return False

    def goto(self, url, wait_until):
        self.visited.append(url)

    def title(self):
        return "Arrival Card"

    def wait_for_load_state(self, state):
        pass

    def evaluate(self, js):
        assert js is forms.READ_JS
        return [{"ref": i, "type": f.type, "label": f.label, "name": "", "required": True,
                 "readonly": f.readonly,
                 "value": f.value, "options": [t for t, _ in f.options]} for i, f in enumerate(self.fields)]

    def locator(self, selector):
        return self.fields[int(selector.split('"')[1])]


@pytest.fixture
def browser(monkeypatch):
    page = FakePage()
    monkeypatch.setattr(forms, "open_browser", lambda settings: page)
    return page


def test_profile_masks_sensitive_values(tools, settings):
    tools.execute("set_profile", {"key": "full_name", "value": "Yan Yuxuan"})
    tools.execute("set_profile", {"key": "passport_no", "value": "E12345678", "sensitive": True})
    listed = tools.execute("list_profile", {}).content
    assert "full_name: Yan Yuxuan" in listed and "{{passport_no}}" in listed
    assert "E12345678" not in listed
    assert tools.execute("set_profile", {"key": "Passport No", "value": "x"}).is_error


def test_fill_form_substitutes_placeholders_locally(tools, browser):
    tools.execute("set_profile", {"key": "full_name", "value": "Yan Yuxuan"})
    tools.execute("set_profile", {"key": "passport_no", "value": "E12345678", "sensitive": True})
    assert "read_form" in tools.execute("fill_form", {"fields": [{"ref": 0, "value": "x"}]}).content
    tools.execute("open_page", {"url": "https://form.test/arrival"})
    form = tools.execute("read_form", {}).content
    assert "[2] Nationality (select, required)" in form and "CHINA | SINGAPORE" in form

    r = tools.execute("fill_form", {"fields": [
        {"ref": 0, "value": "{{full_name}}"},
        {"ref": 1, "value": "{{passport_no}}"},
        {"ref": 2, "value": "china"},
        {"ref": 3, "value": "true"},
    ]}).content
    assert [f.value for f in browser.fields][:4] == ["Yan Yuxuan", "E12345678", "CHINA", "true"]
    assert "E12345678" not in r and "*** (from {{passport_no}})" in r
    assert "E12345678" not in tools.execute("read_form", {}).content


def test_fill_form_reports_bad_values_per_field(tools, browser):
    tools.execute("open_page", {"url": "https://form.test/arrival"})
    tools.execute("read_form", {})
    r = tools.execute("fill_form", {"fields": [
        {"ref": 2, "value": "Singapor"}, {"ref": 0, "value": "{{nickname}}"}, {"ref": 9, "value": "x"},
        {"ref": 3, "value": "yes"},
    ]}).content
    assert "closest: SINGAPORE" in r and "No profile value for nickname" in r and "[9] Error" in r
    assert browser.fields[3].value == "true"  # one bad field doesn't stop the rest


def test_open_page_rejects_other_schemes(tools, browser):
    assert tools.execute("open_page", {"url": "javascript:alert(1)"}).is_error
    assert tools.execute("read_form", {}).is_error  # nothing open yet


def test_sensitive_dates_are_reformatted_and_never_echoed(tools, browser):
    tools.execute("set_profile", {"key": "date_of_birth", "value": "1999-12-31", "sensitive": True})
    tools.execute("open_page", {"url": "https://form.test/arrival"})
    assert "(text, required, readonly)" in tools.execute("read_form", {}).content

    r = tools.execute("fill_form", {"fields": [{"ref": 4, "value": "{{date_of_birth|DD/MM/YYYY}}"}]})
    assert browser.fields[4].value == "31/12/1999"  # read-only date picker set via script
    assert "31/12/1999" not in r.content and "1999" not in tools.execute("read_form", {}).content

    # A browser error that echoes the typed value must not leak it either.
    browser.fields[1].readonly = True
    r = tools.execute("fill_form", {"fields": [{"ref": 1, "value": "{{date_of_birth}}"}]}).content
    assert "TimeoutError" in r and "1999" not in r
    r = tools.execute("fill_form", {"fields": [{"ref": 0, "value": "{{date_of_birth|DD/MM/YYYY}}"}]}).content
    assert "1999" not in r


def test_date_format_needs_iso_value(tools, browser):
    tools.execute("set_profile", {"key": "birthday", "value": "23 April", "sensitive": True})
    tools.execute("open_page", {"url": "https://form.test/arrival"})
    tools.execute("read_form", {})
    r = tools.execute("fill_form", {"fields": [{"ref": 4, "value": "{{birthday|DD/MM/YYYY}}"}]}).content
    assert "YYYY-MM-DD" in r and "April" not in r


def test_click_button_moves_on_but_never_submits(tools, browser):
    tools.execute("open_page", {"url": "https://form.test/arrival"})
    form = tools.execute("read_form", {}).content
    assert "Buttons (click_button):\n[5] Next\n[6] Submit (final" in form

    r = tools.execute("click_button", {"ref": 5})
    assert not r.is_error and browser.fields[5].clicks == 1 and "read_form again" in r.content
    assert tools.execute("click_button", {"ref": 5}).is_error  # numbers are stale after a click

    tools.execute("read_form", {})
    for ref in (6, 7):
        r = tools.execute("click_button", {"ref": ref})
        assert r.is_error and "only the user" in r.content
        assert not hasattr(browser.fields[ref], "clicks")
    assert tools.execute("click_button", {"ref": 0}).is_error  # a text field, not a button
    assert tools.execute("fill_form", {"fields": [{"ref": 5, "value": "x"}]}).content.count("button") == 2


@pytest.mark.parametrize("text, final", [
    ("Submit", True), ("submitRegistration", True), ("Confirm & Pay", True), ("Reset", True),
    ("提交申请", True), ("Hantar", True), ("Next", False), ("Continue", False),
    ("Add another traveller", False), ("Design options", False),
])
def test_final_button_detection(text, final):
    assert forms.is_final({"label": text, "name": ""}) is final


def test_interests_roundtrip(tools):
    assert "Saved interest #1" in tools.execute("add_interest", {"topic": "MLX quantization"}).content
    again = tools.execute("add_interest", {"topic": "mlx  Quantization", "evidence": "asked twice"})
    assert "Interest #1 already existed" in again.content
    goal = tools.execute("add_interest", {"topic": "Land an AI job", "kind": "goal"})
    assert goal.content == "Saved goal #2."
    listed = tools.execute("list_interests", {}).content
    assert listed.index("🎯 Land an AI job") < listed.index("MLX quantization (weight 1.5")
    assert tools.execute("mute_interest", {"id": 1}).content == "Muted #1."
    assert "MLX" not in tools.execute("list_interests", {}).content
    assert tools.execute("mute_interest", {"id": 9}).is_error


# --- web ------------------------------------------------------------------------------------

PUBLIC_IP = {"example.com": "93.184.215.14", "news.test": "93.184.215.15", "search.test": "93.184.215.16"}


@pytest.fixture
def web_net(monkeypatch):
    """A fake internet: DNS from PUBLIC_IP plus a few private hosts, and pages from `pages`."""
    pages: dict[str, httpx2.Response] = {}
    requests: list[httpx2.Request] = []

    def handler(request):
        requests.append(request)
        return pages.get(str(request.url), httpx2.Response(404))

    hosts = {**PUBLIC_IP, "localhost": "127.0.0.1", "router.test": "192.168.1.1",
             "metadata.google.internal": "169.254.169.254", "v6.test": "::1"}
    monkeypatch.setattr(web, "resolve", lambda host: [hosts.get(host, "203.0.113.9")])
    monkeypatch.setattr(web, "transport", httpx2.MockTransport(handler))
    return pages, requests


def web_tools(settings, **config):
    s = replace(settings, **{"searxng_url": None, "tavily_api_key": None, **config})
    return ToolRegistry(SKILLS["web"](Context(settings=s, db=open_db(s))).tools)


def html(body, title="Acme careers"):
    return httpx2.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                           text=f"<html><head><title>{title}</title><script>evil()</script></head>"
                                f"<body><nav>Menu</nav><h1>AI Engineer</h1><p>{body}</p></body></html>")


def test_fetch_url_reads_text_and_pages(settings, web_net):
    pages, _ = web_net
    pages["https://example.com/job"] = html("Python and RAG. " * 2000)
    tools = web_tools(settings)
    r = tools.execute("fetch_url", {"url": "https://example.com/job"})
    assert not r.is_error, r.content
    assert r.content.startswith("[Acme careers | https://example.com/job | page 1/3]")
    assert "AI Engineer" in r.content and "evil" not in r.content and "Menu" not in r.content
    assert "page 3/3" in tools.execute("fetch_url", {"url": "https://example.com/job", "page": 3}).content
    assert tools.execute("fetch_url", {"url": "https://example.com/job", "page": 4}).is_error


@pytest.mark.parametrize("url", [
    "http://localhost:8765/api/sessions", "http://127.0.0.1/", "http://router.test/admin",
    "http://metadata.google.internal/computeMetadata/v1/", "http://v6.test/", "file:///etc/passwd",
    "ftp://example.com/x",
])
def test_fetch_url_refuses_private_and_non_http(settings, web_net, url):
    _, requests = web_net
    r = web_tools(settings).execute("fetch_url", {"url": url})
    assert r.is_error and not requests


def test_fetch_url_checks_every_redirect(settings, web_net):
    pages, requests = web_net
    pages["https://example.com/a"] = httpx2.Response(302, headers={"location": "/b"})
    pages["https://example.com/b"] = httpx2.Response(301, headers={"location": "http://127.0.0.1:8765/"})
    tools = web_tools(settings)
    r = tools.execute("fetch_url", {"url": "https://example.com/a"})
    assert r.is_error and "private or local" in r.content
    assert [str(q.url) for q in requests] == ["https://example.com/a", "https://example.com/b"]

    pages["https://example.com/c"] = httpx2.Response(302, headers={"location": "https://news.test/story"})
    pages["https://news.test/story"] = httpx2.Response(200, headers={"content-type": "text/plain"}, text="Hi")
    assert "| https://news.test/story |" in tools.execute("fetch_url", {"url": "https://example.com/c"}).content

    pages["https://example.com/loop"] = httpx2.Response(302, headers={"location": "/loop"})
    assert "redirects" in tools.execute("fetch_url", {"url": "https://example.com/loop"}).content


def test_fetch_url_refuses_binary_huge_and_errors(settings, web_net):
    pages, _ = web_net
    pages["https://example.com/cv.pdf"] = httpx2.Response(200, headers={"content-type": "application/pdf"},
                                                         content=b"%PDF")
    pages["https://example.com/huge"] = httpx2.Response(200, headers={"content-type": "text/plain"},
                                                       content=b"x" * (web.MAX_BYTES + 1))
    tools = web_tools(settings)
    assert "application/pdf" in tools.execute("fetch_url", {"url": "https://example.com/cv.pdf"}).content
    assert "larger than" in tools.execute("fetch_url", {"url": "https://example.com/huge"}).content
    assert "HTTP 404" in tools.execute("fetch_url", {"url": "https://example.com/missing"}).content


def test_web_search_needs_a_provider(settings, web_net):
    r = web_tools(settings).execute("web_search", {"query": "AI engineer Singapore"})
    assert r.is_error and "SOLFISH_TAVILY_API_KEY" in r.content


def test_web_search_tavily(settings, web_net):
    pages, requests = web_net
    pages[web.TAVILY_API] = httpx2.Response(200, json={"results": [
        {"title": "Acme hiring", "url": "https://example.com/job", "content": "AI  Engineer\nrole"},
    ]})
    tools = web_tools(settings, tavily_api_key="tvly-k")
    r = tools.execute("web_search", {"query": "acme", "max_results": 3})
    assert r.content == "Acme hiring\nhttps://example.com/job\nAI Engineer role"
    assert requests[0].headers["authorization"] == "Bearer tvly-k"
    assert json.loads(requests[0].content) == {"query": "acme", "max_results": 3}


def test_web_search_prefers_searxng(settings, web_net):
    pages, requests = web_net
    pages["http://127.0.0.1:8888/search?q=acme&format=json"] = httpx2.Response(200, json={"results": [
        {"title": f"Hit {i}", "url": f"https://example.com/{i}", "content": "x"} for i in range(8)
    ]})
    tools = web_tools(settings, searxng_url="http://127.0.0.1:8888/", tavily_api_key="tvly-k")
    r = tools.execute("web_search", {"query": "acme", "max_results": 2})
    assert r.content.count("Hit ") == 2 and requests[0].url.host == "127.0.0.1"  # the user's own server

    pages["http://127.0.0.1:8888/search?q=down&format=json"] = httpx2.Response(403, text="json disabled")
    assert "SearXNG said 403" in tools.execute("web_search", {"query": "down"}).content


# ---------- reading ----------

@pytest.fixture
def reading_net(monkeypatch):
    """A fake yuxuanyan.com: responses by (method, path), and the requests it got."""
    routes: dict[tuple[str, str], httpx2.Response] = {}
    requests: list[httpx2.Request] = []

    def handler(request):
        requests.append(request)
        return routes.get((request.method, request.url.path), httpx2.Response(404))

    monkeypatch.setattr(reading, "transport", httpx2.MockTransport(handler))
    return routes, requests


def reading_tools(settings, token="tok"):
    s = replace(settings, reading_token=token)
    return ToolRegistry(SKILLS["reading"](Context(settings=s, db=open_db(s))).tools)


def test_list_books(settings, reading_net):
    routes, _ = reading_net
    routes[("GET", "/api/reading/books")] = httpx2.Response(200, json={"books": [
        {"slug": "principles", "title": "Principles: Life and Work", "author": "Ray Dalio",
         "status": "reading", "noteCount": 2},
        {"slug": "huo-zhe", "title": "活着", "author": "", "status": "finished", "noteCount": 0},
    ]})
    r = reading_tools(settings).execute("list_books", {})
    assert r.content == ("principles: Principles: Life and Work (Ray Dalio) · reading · 2 notes\n"
                         "huo-zhe: 活着 · finished · 0 notes")
    routes[("GET", "/api/reading/books")] = httpx2.Response(200, json={"books": []})
    empty = reading_tools(settings).execute("list_books", {})
    assert "/admin/reading/books" in empty.content


def test_add_reading_note_needs_approval_and_shows_the_note(settings):
    note = reading_tools(settings).get("add_reading_note")
    assert note.needs_approval
    shown = note.approval_args({"book": "principles", "quote": "Pain + Reflection = Progress",
                                "thoughts": "痛苦本身不是坏事。", "location": "Ch. 1",
                                "read_on": "2026-11-11"})
    assert shown["preview"] == ("Public reading note on yuxuanyan.com/reading/principles\n"
                                "2026-11-11 · principles · Ch. 1\n\n"
                                "> Pain + Reflection = Progress\n\n痛苦本身不是坏事。")


def test_add_reading_note_posts_with_the_token(settings, reading_net):
    routes, requests = reading_net
    routes[("POST", "/api/reading/notes")] = httpx2.Response(
        201, json={"id": "n1", "url": "https://yuxuanyan.com/en/reading/principles#note-n1"})
    args = {"book": "principles", "thoughts": "Mixed 中英 note"}
    r = reading_tools(settings).execute("add_reading_note", args)
    assert r.content == "Posted: https://yuxuanyan.com/en/reading/principles#note-n1"
    sent = requests[0]
    assert str(sent.url) == "https://yuxuanyan.com/api/reading/notes"
    assert sent.headers["authorization"] == "Bearer tok"
    assert json.loads(sent.content) == {"book": "principles", "readOn": str(reading.today()), "location": "",
                                        "quote": "", "bodyMd": "Mixed 中英 note"}


def test_add_reading_note_failures(settings, reading_net):
    routes, requests = reading_net
    args = {"book": "principles", "thoughts": "x"}
    unset = reading_tools(settings, token=None).execute("add_reading_note", args)
    assert "SOLFISH_READING_TOKEN" in unset.content
    empty = reading_tools(settings).execute("add_reading_note", {"book": "principles"})
    assert "quote or thoughts" in empty.content
    assert not requests
    cases = [(401, "refused the reading token"), (404, "No book with the slug"), (400, "said 400")]
    for status, message in cases:
        routes[("POST", "/api/reading/notes")] = httpx2.Response(status, json={"error": "x"})
        r = reading_tools(settings).execute("add_reading_note", args)
        assert r.is_error and message in r.content


def test_add_reading_note_timeout_warns_against_retrying(settings, monkeypatch):
    def handler(request):
        raise httpx2.ReadTimeout("slow", request=request)

    monkeypatch.setattr(reading, "transport", httpx2.MockTransport(handler))
    r = reading_tools(settings).execute("add_reading_note", {"book": "principles", "thoughts": "x"})
    assert r.is_error and "may be posted anyway" in r.content
