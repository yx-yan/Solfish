# CLAUDE.md

Solfish (solve + fish) is a general-purpose personal agent written from scratch on the Claude
API, with no agent framework. It is both a tool I use daily and a public portfolio piece
showing I understand how agents work, so code clarity and the docs matter as much as features.

## Commands
```bash
source .venv/bin/activate      # venv built from /opt/miniconda3/bin/python (3.14); system python is 3.9
pip install -e ".[dev]"
pytest                         # offline, uses tests/conftest.py FakeClient
ruff check .
solfish                        # chat (needs ANTHROPIC_API_KEY or `ant auth login`)
python evals/run_evals.py      # live API, costs money: ask before running
```

## Layout
- `src/solfish/agent.py`: the hand-written loop. Keep it append-only (never edit earlier messages)
- `src/solfish/tools/`: `@tool(ParamsModel)` + `ToolRegistry`; tool failures return `is_error` results, never raise
- `src/solfish/skills/`: one module per capability exposing `load(ctx) -> Skill`; register in `skills/__init__.py`
- `src/solfish/app.py`: wiring + system prompt (stable cached block, then memory snapshot)
- `evals/tasks.json`: end-state checks (sql / tool_called / reply_contains_all / file_contains)
- `docs/design.md`: design decisions; update it when a decision changes

## Conventions
- New skill: add the module, register it, add offline tests in `tests/test_skills.py`, add eval tasks
- Side-effecting tools set `needs_approval=True`
- Nothing time-varying in the system prompt (breaks prompt caching)
- Personal data stays in `~/.solfish/` and is gitignored; the repo is public
