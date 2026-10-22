"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("SOLFISH_HOME", "~/.solfish")).expanduser()


@dataclass(frozen=True)
class Settings:
    model: str = field(default_factory=lambda: os.environ.get("SOLFISH_MODEL", "claude-opus-5-5"))
    effort: str = field(default_factory=lambda: os.environ.get("SOLFISH_EFFORT", "high"))
    max_tokens: int = 16000
    max_steps: int = field(default_factory=lambda: int(os.environ.get("SOLFISH_MAX_STEPS", "20")))
    # Server-side refusal fallback: if the main model declines, the API retries on another model.
    fallbacks: bool = True
    home: Path = field(default_factory=_home)

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
