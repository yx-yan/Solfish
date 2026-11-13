"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    """Read KEY=value lines from a .env file into the environment. Variables already set in the
    shell win, and empty values count as unset. Called by the entry points, not on import, so
    tests never pick up a real .env."""
    if not path.is_file():
        return
    path.chmod(0o600)  # API keys live here
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            os.environ.setdefault(key, value)


def home_dir() -> Path:
    return Path(os.environ.get("SOLFISH_HOME", "~/.solfish")).expanduser()


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _weekly(var: str) -> tuple[int, int, int] | None:
    """"sun 20:00" -> (6, 20, 0), weekday as in datetime.weekday(); unset or empty -> None."""
    value = os.environ.get(var, "").strip().lower()
    if not value:
        return None
    day, _, clock = value.partition(" ")
    hour, _, minute = clock.partition(":")
    if day[:3] not in WEEKDAYS or not hour.isdigit():
        raise ValueError(f'{var} must look like "sun 20:00", not {value!r}')
    return WEEKDAYS.index(day[:3]), int(hour), int(minute or 0)


def _range(var: str, default: str) -> tuple[int, int]:
    """"5-8" -> (5, 8); a single number "0" -> (0, 0)."""
    low, _, high = os.environ.get(var, default).partition("-")
    return int(low), int(high or low)


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
class XAuth:
    """OAuth 1.0a keys for the X publisher, from the app's Keys and Tokens page."""

    api_key: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_X_API_KEY"))
    api_secret: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_X_API_SECRET"))
    access_token: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_X_ACCESS_TOKEN"))
    access_secret: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_X_ACCESS_SECRET"))

    @property
    def configured(self) -> bool:
        return all((self.api_key, self.api_secret, self.access_token, self.access_secret))


@dataclass(frozen=True)
class BlogDrafts:
    """Where the blog publisher adds drafts. The token can do nothing else (see blog.py)."""

    supabase_url: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_BLOG_SUPABASE_URL"))
    publishable_key: str | None = field(
        default_factory=lambda: os.environ.get("SOLFISH_BLOG_PUBLISHABLE_KEY")
    )
    token: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_BLOG_DRAFT_TOKEN"))
    site: str = field(default_factory=lambda: os.environ.get("SOLFISH_BLOG_SITE", "https://yuxuanyan.com"))

    @property
    def configured(self) -> bool:
        return all((self.supabase_url, self.publishable_key, self.token))


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
    home: Path = field(default_factory=home_dir)
    # The LaTeX résumé repo the resume skill tailors from. Never written in place: builds happen
    # in a copy, and changes to the master go up as a pull request from a temporary worktree.
    resume_dir: Path | None = field(default_factory=lambda: _optional_path("SOLFISH_RESUME_DIR"))
    # The folder holding the user's project clones; recent_project_activity reads their history
    # to keep the résumé current (`solfish resume-review`).
    projects_dir: Path | None = field(default_factory=lambda: _optional_path("SOLFISH_PROJECTS_DIR"))
    resume_main: str = field(
        default_factory=lambda: os.environ.get("SOLFISH_RESUME_MAIN", "resume-en.tex")
    )
    # Opens résumé pull requests (fine-grained, that repo only: Pull requests read/write).
    # Without it the branch is still pushed and the reply carries a link to open the PR by hand.
    github_token: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_GITHUB_TOKEN"))
    # Web search: a self-hosted SearXNG wins when set, else Tavily (free tier: 1,000 a month).
    searxng_url: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_SEARXNG_URL"))
    tavily_api_key: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_TAVILY_API_KEY"))
    smtp: Smtp = field(default_factory=Smtp)
    x: XAuth = field(default_factory=XAuth)
    blog: BlogDrafts = field(default_factory=BlogDrafts)
    # Adds reading notes on the blog's site (SOLFISH_BLOG_SITE); can do nothing else (see reading.py).
    reading_token: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_READING_TOKEN"))
    linkedin_token: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_LINKEDIN_TOKEN"))
    # LinkedIn retires API versions about a year after release, so this needs a bump now and then.
    linkedin_version: str = field(
        default_factory=lambda: os.environ.get("SOLFISH_LINKEDIN_VERSION", "202610")
    )
    # Telegram front end (runs inside `solfish serve`). Only this chat id is answered; leave it
    # unset at first and the bot replies with yours.
    telegram_token: str | None = field(default_factory=lambda: os.environ.get("SOLFISH_TELEGRAM_TOKEN"))
    telegram_chat_id: int | None = field(
        default_factory=lambda: int(v) if (v := os.environ.get("SOLFISH_TELEGRAM_CHAT_ID")) else None
    )
    # Conversations Solfish starts through Telegram: how many a day (random), and the hours they
    # fall in (start inclusive, end exclusive). "0" turns them off.
    sparks_per_day: tuple[int, int] = field(default_factory=lambda: _range("SOLFISH_SPARKS_PER_DAY", "5-8"))
    spark_hours: tuple[int, int] = field(default_factory=lambda: _range("SOLFISH_SPARK_HOURS", "9-23"))
    # When the server starts the weekly résumé review in Telegram, e.g. "sun 20:00"; unset: never.
    review_at: tuple[int, int, int] | None = field(default_factory=lambda: _weekly("SOLFISH_REVIEW_AT"))

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
        self.home.chmod(0o700)  # sessions, traces and profile secrets are all under here
