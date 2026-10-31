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
    model: str = field(default_factory=lambda: os.environ.get("SOLFISH_MODEL", "deepseek-flash"))
    # DeepSeek's Anthropic-compatible endpoint, so the same SDK and message format work.
    # Set SOLFISH_BASE_URL=https://api.anthropic.com to run on Claude instead.
    base_url: str = field(
        default_factory=lambda: os.environ.get("SOLFISH_BASE_URL", "https://api.deepseek.com/anthropic")
    )
    # Unset falls back to ANTHROPIC_API_KEY, which is what you want on Claude.
    api_key: str | None = field(default_factory=lambda: os.environ.get("DEEPSEEK_API_KEY"))
    effort: str = field(default_factory=lambda: os.environ.get("SOLFISH_EFFORT", "high"))
    max_tokens: int = 16000
    max_steps: int = field(default_factory=lambda: int(os.environ.get("SOLFISH_MAX_STEPS", "20")))
    # Server-side refusal fallback: if the main model declines, the API retries on another model.
    # Claude only; DeepSeek has no such beta.
    fallbacks: bool = False
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
