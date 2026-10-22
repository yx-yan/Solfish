"""Skills are how Solfish stays general-purpose.

A skill bundles a few tools with the instructions for using them. The agent core knows nothing
about job hunting or files; it just loads whichever skills are enabled.
"""

from __future__ import annotations

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


def open_db(settings: Settings) -> sqlite3.Connection:
    settings.ensure_dirs()
    db = sqlite3.connect(settings.db_path)
    db.row_factory = sqlite3.Row
    return db
