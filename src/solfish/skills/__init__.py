from collections.abc import Callable

from . import content, core, forms, jobs, memory, research, resume, spark
from .base import Context, Skill, open_db

# Name -> loader. Adding a capability to Solfish means adding one module and one line here.
SKILLS: dict[str, Callable[[Context], Skill]] = {
    "core": core.load,
    "memory": memory.load,
    "jobs": jobs.load,
    "resume": resume.load,
    "content": content.load,
    "research": research.load,
    "forms": forms.load,
    "spark": spark.load,
}

__all__ = ["SKILLS", "Context", "Skill", "open_db"]
