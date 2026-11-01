from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from solfish.config import Smtp
from solfish.skills import SKILLS, Context, content, forms, open_db, research, resume
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
