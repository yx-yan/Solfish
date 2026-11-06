"""Skills are how Solfish stays general-purpose.

A skill bundles a few tools with the instructions for using them. The agent core knows nothing
about job hunting or files; it just loads whichever skills are enabled.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from ..config import Settings
from ..tools import Tool


@dataclass
class Context:
    settings: Settings
    db: sqlite3.Connection


@dataclass(frozen=True)
class Skill:
    name: str
    instructions: str
    tools: list[Tool] = field(default_factory=list)


def slugify(name: str) -> str:
    """A filename-safe version of a name. Keeps CJK characters; may return ''."""
    return re.sub(r"[^\w]+", "-", name.lower()).strip("-_")[:60]


def open_db(settings: Settings) -> sqlite3.Connection:
    settings.ensure_dirs()
    # Sessions run their turns on worker threads (one turn at a time per connection).
    db = sqlite3.connect(settings.db_path, check_same_thread=False)
    settings.db_path.chmod(0o600)
    db.row_factory = sqlite3.Row
    return db
