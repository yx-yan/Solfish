"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("SOLFISH_HOME", "~/.solfish")).expanduser()


def _optional_path(var: str) -> Path | None:
    value = os.environ.get(var)
    return Path(value).expanduser() if value else None


@dataclass(frozen=True)
class Smtp:
    """Outgoing mail for the resume skill. Unset host means sending is disabled."""

    host: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_SMTP_HOST"))
    port: int = field(default_factory=lambda: int(os.environ.get("SOLFISH_SMTP_PORT", "465")))
    user: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_SMTP_USER"))
    password: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_SMTP_PASSWORD"))
    sender: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_MAIL_FROM"))


@dataclass(frozen=True)
class Settings:
    model: str = field(default_factory=lambda: os.environ.get("SOLFISH_MODEL", "claude-opus-5-5"))
    effort: str = field(default_factory=lambda: os.environ.get("SOLFISH_EFFORT", "high"))
    max_tokens: int = 16000
    max_steps: int = field(default_factory=lambda: int(os.environ.get("SOLFISH_MAX_STEPS", "20")))
    # Server-side refusal fallback: if the main model declines, the API retries on another model.
    fallbacks: bool = True
    home: Path = field(default_factory=_home)
    # The LaTeX résumé repo the resume skill tailors from (read-only; builds happen in a copy).
    resume_dir: Path | None = field(default_factory=lambda: _optional_path("SOLFISH_RESUME_DIR"))
    resume_main: str = field(
        default_factory=lambda: os.environ.get("SOLFISH_RESUME_MAIN", "resume-ntu-en.tex")
    )
    smtp: Smtp = field(default_factory=Smtp)

    @property
    def db_path(self) -> Path:
        return self.home / "solfish.db"

    @property
    def workspace(self) -> Path:
        return self.home / "workspace"

    @property
    def traces_dir(self) -> Path:
        return self.home / "traces"

    def ensure_dirs(self) -> None:
        for d in (self.home, self.workspace, self.traces_dir):
            d.mkdir(parents=True, exist_ok=True)
