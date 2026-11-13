"""Résumé skill: tailor the LaTeX résumé to a job description, build it, send applications,
and propose changes to the master résumé.

The résumé repo (`SOLFISH_RESUME_DIR`) is never written in place. A tailored version is built
in a temporary copy of that repo, so the class file and fonts resolve, and the resulting .tex
and .pdf land in the workspace under `resumes/`. LaTeX runs with shell escape off: the model
writes the source, so it must not be able to run commands through `\\write18`.

Changes to the master résumé go up as a pull request: a temporary git worktree on the latest
`main`, rebuilt and page-checked, pushed to a `solfish/<name>` branch. Merging is the user's
review, so the agent can never change the résumé the user sends out on its own. Only tracked
top-level .tex files can be read or changed; ignored local drafts stay off the model's input.

Applying by email is automated (SMTP, approval required). Job portals (Workday, LinkedIn, ...)
are not: they have no API for applicants, so the agent prepares the package and the user clicks
submit.
"""

from __future__ import annotations

import re
import shutil
import smtplib
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import httpx2
from pydantic import BaseModel, Field

from ..config import Smtp
from ..tools import ToolError, tool
from . import jobs
from .base import Context, Skill, slugify
from .core import resolve_in

LATEX_TIMEOUT_S = 120
GIT_TIMEOUT_S = 60
BASE_BRANCH = "main"
BRANCH_PREFIX = "solfish/"
TEX_NAME = re.compile(r"^[\w.-]+\.tex$")
BRANCH_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
GITHUB_REMOTE = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([\w.-]+/[\w.-]+?)(?:\.git)?/?$"
)


class ReadArgs(BaseModel):
    file: str | None = Field(
        default=None, description="Another .tex in the repo, e.g. 'resume-zh.tex'. Default: the main résumé."
    )


class FileChange(BaseModel):
    path: str = Field(description="A tracked .tex file at the top of the résumé repo, e.g. 'resume-en.tex'.")
    content: str = Field(description="The file's complete new content.")


class ProposeArgs(BaseModel):
    name: str = Field(
        description="Short English name for the change, e.g. 'add-quoth-project'. "
        "Using the name of an open proposal adds a commit to that pull request."
    )
    title: str = Field(description="Pull request title, which is also the commit message.")
    summary: str = Field(
        description="Pull request description: what changed and why, and any fact the user should check."
    )
    changes: list[FileChange] = Field(min_length=1)


class ActivityArgs(BaseModel):
    days: int = Field(default=7, ge=1, le=365, description="How many days back to look.")


class BuildArgs(BaseModel):
    name: str = Field(description="Short name for this version, e.g. 'acme-ai-engineer'.")
    tex: str = Field(description="Complete LaTeX source of the tailored résumé.")


class SendArgs(BaseModel):
    application_id: int = Field(description="Tracker id of the application (see list_applications).")
    to: str = Field(description="Recipient email address from the job posting.")
    subject: str
    body: str = Field(description="Plain-text email body (the cover letter or a short note).")
    attachments: list[str] = Field(
        default_factory=list, description="Workspace paths to attach, e.g. 'resumes/acme.pdf'."
    )


def run_latex(workdir: Path, filename: str) -> str:
    """Run XeLaTeX twice (so aligned dates and links settle) and return the final log."""
    cmd = ["xelatex", "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", filename]
    for _ in range(2):
        try:
            subprocess.run(cmd, cwd=workdir, capture_output=True, timeout=LATEX_TIMEOUT_S, check=False)
        except FileNotFoundError:
            raise ToolError("xelatex is not installed (install MacTeX or TeX Live).") from None
        except subprocess.TimeoutExpired:
            raise ToolError(f"xelatex timed out after {LATEX_TIMEOUT_S}s.") from None
    log = workdir / Path(filename).with_suffix(".log")
    return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""


def page_count(log: str) -> int | None:
    m = re.search(r"Output written on .*?\((\d+) pages?", log)
    return int(m.group(1)) if m else None


def latex_errors(log: str, limit: int = 3) -> str:
    """The first few '! ...' error lines with the line after each, which holds the location."""
    lines = log.splitlines()
    out = [f"{ln}\n{lines[i + 1] if i + 1 < len(lines) else ''}"
           for i, ln in enumerate(lines) if ln.startswith("!")]
    return "\n".join(out[:limit]) or "(no error lines in the log)"


def send_mail(smtp: Smtp, msg: EmailMessage) -> None:
    if smtp.port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=30)
    else:
        server = smtplib.SMTP(smtp.host, smtp.port, timeout=30)
        server.starttls()
    with server:
        if smtp.user:
            server.login(smtp.user, smtp.password or "")
        server.send_message(msg)


def git(repo: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                           timeout=GIT_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        raise ToolError(f"git {args[0]} timed out after {GIT_TIMEOUT_S}s.") from None
    if r.returncode:
        raise ToolError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-500:]}")
    return r.stdout.strip()


# The weekly review: `/review` in Telegram, weekly from the server (SOLFISH_REVIEW_AT), or
# `solfish resume-review`. The instructions go in the user turn, not the system prompt, so the
# cached prefix stays the same as in any other session.
REVIEW_PROMPT = """Weekly résumé review. Keep my résumé current with what I've actually shipped.

1. Call recent_project_activity with days={days}.
2. Read the résumé with read_base_resume (resume-en.tex), and resume-zh.tex as well.
3. Decide whether anything is worth changing: a new project, a milestone shipped, a result with
   a number. Skip routine work (refactors, small fixes, docs) and anything already on the résumé.
4. If it is, call propose_resume_change with name "weekly-project-update" (an open proposal with
   that name gets the new commit, so unreviewed weeks pile up in one pull request). Change
   resume-en.tex and resume-zh.tex together, same facts in the same order.
   - Use only facts from the activity or already on the résumé. Never invent numbers, users or
     outcomes. Where a number would help but isn't stated, write the line without it and add a
     LaTeX comment above it: % TODO(确认): <what to confirm>.
   - Keep one page: tighten or drop weaker lines to make room. If it doesn't fit, cut and propose
     again.
5. Reply in Chinese, in a few lines: what you proposed and why, with the pull request link; or
   that nothing was worth changing and what you looked at. If the tools failed, say which. I may
   answer with changes; revise by proposing again under the same name."""

# What a review may do without asking: test builds land in the local workspace and change nothing
# anyone sees. propose_resume_change needs no approval anyway, since merging is the approval.
REVIEW_PREAPPROVED = frozenset({"build_resume"})

ACTIVITY_LIMIT = 16_000  # characters in all, shared evenly so every active project shows up
BODY_LIMIT = 400  # per commit message body
README_LINES = 25


@dataclass
class Activity:
    remote: str  # 'owner/name', or the folder name; several clones of one repo share it
    latest: str  # date of the newest commit, YYYY-MM-DD
    text: str


def default_ref(repo: Path) -> str:
    """origin's default branch (origin/HEAD), else origin/main or origin/master, else HEAD."""
    for args in (("rev-parse", "--abbrev-ref", "origin/HEAD"),
                 ("rev-parse", "--verify", "--quiet", "--abbrev-ref", "origin/main"),
                 ("rev-parse", "--verify", "--quiet", "--abbrev-ref", "origin/master")):
        try:
            if ref := git(repo, *args):
                return ref
        except ToolError:
            continue
    return "HEAD"


def repo_activity(repo: Path, days: int) -> Activity | None:
    """The user's commits that reached the default branch in the last `days` days, newest first:
    merge commits carry pull request titles, the merged commits their full messages. Plus the top
    of the README; None if there are no commits."""
    try:
        git(repo, "fetch", "--quiet", "origin")
        stale = ""
    except ToolError:
        stale = " (couldn't fetch; local history only)"
    try:
        # Several --author flags match any of them: the local email, and the name, which also
        # catches merges made on GitHub (authored with a noreply address)
        authors = [f"--author={git(repo, 'config', key)}" for key in ("user.email", "user.name")]
        log = git(repo, "log", default_ref(repo), f"--since={days}.days",
                  *authors, "--date=short", "--format=%x1e%ad %s%n%b")
    except ToolError:
        return None
    commits = []
    for record in filter(None, (r.strip() for r in log.split("\x1e"))):
        head, _, body = record.partition("\n")
        body = " ".join(body.split())
        commits.append(f"- {head}" + (f"\n  {body[:BODY_LIMIT]}" if body else ""))
    if not commits:
        return None
    try:
        remote = github_repo(git(repo, "remote", "get-url", "origin")) or repo.name
    except ToolError:
        remote = repo.name
    out = [f"## {repo.name} ({remote}){stale}", f"{len(commits)} commit(s):", *commits]
    readme = repo / "README.md"
    if readme.is_file():
        top = readme.read_text(encoding="utf-8", errors="replace").splitlines()[:README_LINES]
        out += ["README (top):", *top]
    return Activity(remote=remote, latest=commits[0][2:12], text="\n".join(out))


def tracked_tex(repo: Path) -> set[str]:
    """Top-level .tex files under version control; ignored local drafts are left out."""
    try:
        return {f for f in git(repo, "ls-files", "--", "*.tex").splitlines() if "/" not in f}
    except ToolError:
        return set()  # not a git repo: only the main résumé is readable


def github_repo(remote: str) -> str | None:
    """'owner/name' from a GitHub remote URL (https or ssh), else None."""
    m = GITHUB_REMOTE.match(remote)
    return m.group(1) if m else None


def open_pr(token: str, repo: str, branch: str, title: str, body: str) -> str:
    """Open a pull request for `branch`, or find the one already open. Returns its URL."""
    api = f"https://api.github.com/repos/{repo}/pulls"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    with httpx2.Client(timeout=30.0, headers=headers) as client:
        found = client.get(api, params={"head": f"{repo.split('/')[0]}:{branch}", "state": "open"})
        if found.is_success and found.json():
            return found.json()[0]["html_url"]
        r = client.post(api, json={"title": title, "body": body, "head": branch, "base": BASE_BRANCH})
    if not r.is_success:
        raise ToolError(
            f"Pushed {branch}, but GitHub refused the pull request ({r.status_code}): {r.text[:300]}"
        )
    return r.json()["html_url"]


def load(ctx: Context) -> Skill:
    settings, db = ctx.settings, ctx.db
    db.executescript(jobs.SCHEMA)
    out_dir = settings.workspace / "resumes"

    def resume_repo() -> Path:
        d = settings.resume_dir
        if d is None or not (d / settings.resume_main).is_file():
            raise ToolError(
                "No résumé configured. Set SOLFISH_RESUME_DIR to the résumé repo "
                f"(it must contain {settings.resume_main})."
            )
        return d

    @tool(ReadArgs)
    def read_base_resume(args: ReadArgs) -> str:
        """Read the master LaTeX résumé (or another tracked .tex in its repo). Start from this when
        tailoring for a job or proposing a change."""
        repo = resume_repo()
        name = args.file or settings.resume_main
        allowed = tracked_tex(repo) | {settings.resume_main}
        if name not in allowed:
            raise ToolError(f"{name!r} isn't a tracked .tex file; choose from {', '.join(sorted(allowed))}.")
        return (repo / name).read_text(encoding="utf-8")

    @tool(ActivityArgs)
    def recent_project_activity(args: ActivityArgs) -> str:
        """What the user shipped recently: their commits and merged pull requests on the main
        branch of each project in SOLFISH_PROJECTS_DIR, with each active project's README.
        Read-only. Use it to keep the résumé current."""
        root = settings.projects_dir
        if root is None or not root.is_dir():
            raise ToolError("Set SOLFISH_PROJECTS_DIR to the folder that holds the project clones.")
        resume_dir = settings.resume_dir.resolve() if settings.resume_dir else None
        repos = sorted(d for d in root.iterdir() if (d / ".git").exists() and d.resolve() != resume_dir)
        by_remote: dict[str, Activity] = {}
        for a in filter(None, (repo_activity(d, args.days) for d in repos)):
            if a.remote not in by_remote or len(a.text) > len(by_remote[a.remote].text):
                by_remote[a.remote] = a  # extra clones of one repo count once
        if not by_remote:
            return f"No commits by the user in the last {args.days} days across {len(repos)} projects."
        found = sorted(by_remote.values(), key=lambda a: a.latest, reverse=True)
        share = ACTIVITY_LIMIT // len(found)
        text = "\n\n".join(a.text if len(a.text) <= share else a.text[:share] + "\n[cut]" for a in found)
        return (f"Activity in the last {args.days} days ({len(found)} of {len(repos)} projects, "
                f"most recent first):\n\n{text}")

    @tool(BuildArgs, needs_approval=True)
    def build_resume(args: BuildArgs) -> str:
        """Compile a tailored résumé to PDF. Saves resumes/<name>.tex and resumes/<name>.pdf in
        the workspace and reports the page count."""
        repo, slug = resume_repo(), slugify(args.name)
        if not slug:
            raise ToolError("Give the version a name with letters or digits, e.g. 'acme-ai-engineer'.")
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "resume"
            shutil.copytree(repo, work, ignore=shutil.ignore_patterns(".git", "*.pdf", "*.synctex.gz"))
            (work / f"{slug}.tex").write_text(args.tex, encoding="utf-8")
            log = run_latex(work, f"{slug}.tex")
            pdf = work / f"{slug}.pdf"
            if not pdf.exists() or "Fatal error" in log or re.search(r"^!", log, re.M):
                raise ToolError(f"LaTeX failed:\n{latex_errors(log)}")
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(pdf, out_dir / f"{slug}.pdf")
        (out_dir / f"{slug}.tex").write_text(args.tex, encoding="utf-8")

        pages = page_count(log)
        msg = f"Built resumes/{slug}.pdf ({pages or '?'} page{'' if pages == 1 else 's'})."
        if pages and pages > 1:
            msg += " The résumé must fit on one page: cut or tighten content and rebuild."
        return msg

    @tool(SendArgs, needs_approval=True)
    def send_application(args: SendArgs) -> str:
        """Email a job application with attachments, then mark it applied in the tracker.
        Only for postings that ask for applications by email."""
        smtp = settings.smtp
        if not (smtp.host and smtp.sender):
            raise ToolError("Email is not set up (SOLFISH_SMTP_HOST and SOLFISH_MAIL_FROM).")
        row = db.execute("SELECT * FROM applications WHERE id = ?", (args.application_id,)).fetchone()
        if row is None:
            raise ToolError(f"No application #{args.application_id}. Track it with add_application first.")

        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = smtp.sender, args.to, args.subject
        msg.set_content(args.body)
        for rel in args.attachments:
            f = resolve_in(settings.workspace, rel)
            if not f.is_file():
                raise ToolError(f"No file at {rel!r} to attach.")
            subtype = "pdf" if f.suffix.lower() == ".pdf" else "octet-stream"
            msg.add_attachment(f.read_bytes(), maintype="application", subtype=subtype, filename=f.name)
        send_mail(smtp, msg)

        note = f"[{date.today()}] Emailed application to {args.to}"
        db.execute(
            "UPDATE applications SET status = 'applied', applied_on = COALESCE(applied_on, ?), "
            "notes = trim(notes || char(10) || ?), updated_at = datetime('now', 'localtime') "
            "WHERE id = ?",
            (date.today().isoformat(), note, args.application_id),
        )
        db.commit()
        n = len(args.attachments)
        return f"Sent to {args.to} with {n} attachment(s); #{args.application_id} is now applied."

    def commit_change(work: Path, repo: Path, branch: str, args: ProposeArgs) -> list[str]:
        """Write, rebuild and page-check the changed files in the worktree, then commit and push."""
        paths = list(dict.fromkeys(c.path for c in args.changes))
        for c in args.changes:
            (work / c.path).write_text(c.content, encoding="utf-8")
        if not git(work, "status", "--porcelain", "--", *paths):
            raise ToolError("These files already match the résumé on GitHub; nothing to propose.")
        # Ignored files the build needs, such as the photo the Chinese résumé embeds.
        if (repo / "assets").is_dir():
            (work / "assets").mkdir(exist_ok=True)
            for f in (repo / "assets").iterdir():
                if f.is_file() and not (work / "assets" / f.name).exists():
                    shutil.copy(f, work / "assets" / f.name)

        to_add = list(paths)
        for path in paths:
            log = run_latex(work, path)
            pdf = Path(path).with_suffix(".pdf").name
            if not (work / pdf).exists() or "Fatal error" in log or re.search(r"^!", log, re.M):
                raise ToolError(f"{path} doesn't compile:\n{latex_errors(log)}")
            pages = page_count(log)
            if pages != 1:
                raise ToolError(
                    f"{path} comes out at {pages or '?'} pages; it must fit on one page. "
                    "Cut or tighten, then propose again."
                )
            if git(work, "ls-files", "--", pdf):  # a committed PDF (resume-en.pdf) must match its source
                to_add.append(pdf)
        git(work, "add", "--", *to_add)
        git(work, "commit", "--quiet", "-m", args.title)
        git(work, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        return paths

    @tool(ProposeArgs)
    def propose_resume_change(args: ProposeArgs) -> str:
        """Change the master résumé through a pull request. Rebuilds each changed file, checks it
        fits on one page, and pushes a solfish/<name> branch; the user reviews and merges."""
        repo = resume_repo()
        if not BRANCH_NAME.match(args.name):
            raise ToolError(
                "Name the change in lowercase English words joined by '-', e.g. 'add-quoth-project'."
            )
        allowed = tracked_tex(repo)
        for c in args.changes:
            if c.path not in allowed:
                raise ToolError(
                    f"{c.path!r} isn't a tracked .tex file in the résumé repo; "
                    f"choose from {', '.join(sorted(allowed)) or 'none (is it a git repo?)'}."
                )
        github = github_repo(git(repo, "remote", "get-url", "origin"))
        branch = BRANCH_PREFIX + args.name

        git(repo, "fetch", "--quiet", "origin")
        # Revising an open proposal builds on its branch, so the change lands in the same PR.
        on_branch = bool(git(repo, "ls-remote", "--heads", "origin", branch))
        start = f"origin/{branch}" if on_branch else f"origin/{BASE_BRANCH}"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "resume"
            git(repo, "worktree", "add", "--detach", "--quiet", str(work), start)
            try:
                paths = commit_change(work, repo, branch, args)
            finally:
                git(repo, "worktree", "remove", "--force", str(work))

        files = ", ".join(paths)
        if github is None:
            return f"Pushed {files} to {branch}. origin isn't on GitHub, so open the pull request there."
        if not settings.github_token:
            return (
                f"Pushed {files} to {branch}. SOLFISH_GITHUB_TOKEN isn't set, so give the user this "
                f"link to open the pull request: https://github.com/{github}/compare/{BASE_BRANCH}...{branch}?expand=1"
            )
        url = open_pr(settings.github_token, github, branch, args.title, args.summary)
        verb = "Updated" if on_branch else "Opened"
        return f"{verb} {url} ({files}). Nothing changes until the user merges it."

    return Skill(
        name="resume",
        instructions=(
            "You tailor the user's LaTeX résumé to a job description. Read the base résumé, then "
            "reorder and reword what is already there to match the JD's keywords and priorities. "
            "Never invent experience, skills, dates or numbers; if the JD asks for something the "
            "résumé lacks, tell the user instead. Keep the LaTeX macros and layout unchanged and "
            "the result on one page; rebuild until it is. Cover letters go in the workspace as "
            "cover-letters/<name>.md. send_application is only for postings that take email "
            "applications; for job portals, prepare the files and tell the user to submit.\n"
            "To change the master résumé itself (new experience, better wording), use "
            "propose_resume_change; it opens a pull request the user reviews. The same rule "
            "holds: only facts the user gave you or that are already there; ask for missing "
            "details instead of guessing. If the résumé exists in more than one language (e.g. "
            "resume-en.tex and resume-zh.tex), read each and change them together in one "
            "proposal, with the same facts, numbers and dates in the same order. To revise a "
            "proposal after the user's feedback, propose again under the same name."
        ),
        tools=[read_base_resume, build_resume, send_application, propose_resume_change,
               recent_project_activity],
    )
