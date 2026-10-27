"""Résumé skill: tailor the LaTeX résumé to a job description, build it, and send applications.

The résumé repo (`SOLFISH_RESUME_DIR`) is only ever read. A tailored version is built in a
temporary copy of that repo, so the class file and fonts resolve, and the resulting .tex and
.pdf land in the workspace under `resumes/`. LaTeX runs with shell escape off: the model writes
the source, so it must not be able to run commands through `\\write18`.

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
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Smtp
from ..tools import ToolError, tool
from . import jobs
from .base import Context, Skill, slugify
from .core import resolve_in

LATEX_TIMEOUT_S = 120


class NoArgs(BaseModel):
    pass


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

    @tool(NoArgs)
    def read_base_resume(_: NoArgs) -> str:
        """Read the master LaTeX résumé. Start from this when tailoring for a job."""
        return (resume_repo() / settings.resume_main).read_text(encoding="utf-8")

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

    return Skill(
        name="resume",
        instructions=(
            "You tailor the user's LaTeX résumé to a job description. Read the base résumé, then "
            "reorder and reword what is already there to match the JD's keywords and priorities. "
            "Never invent experience, skills, dates or numbers; if the JD asks for something the "
            "résumé lacks, tell the user instead. Keep the LaTeX macros and layout unchanged and "
            "the result on one page; rebuild until it is. Cover letters go in the workspace as "
            "cover-letters/<name>.md. send_application is only for postings that take email "
            "applications; for job portals, prepare the files and tell the user to submit."
        ),
        tools=[read_base_resume, build_resume, send_application],
    )
