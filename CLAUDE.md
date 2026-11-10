# CLAUDE.md

Solfish (salt fish, also "solve and finish") is a general-purpose personal agent written from scratch on the Anthropic
Messages API (served by DeepSeek by default), with no agent framework. It is both a tool I use daily and a public portfolio piece
showing I understand how agents work, so code clarity and the docs matter as much as features.

## Commands
```bash
source .venv/bin/activate      # venv built from /opt/miniconda3/bin/python (3.14); system python is 3.9
pip install -e ".[dev]"
pytest                         # offline, uses tests/conftest.py FakeClient
ruff check .
solfish                        # chat (needs DEEPSEEK_API_KEY)
solfish serve --open           # local web UI on 127.0.0.1:8765
python evals/run_evals.py      # live API, costs money: ask before running
```

## Layout
- `src/solfish/agent.py`: the hand-written loop. Keep it append-only (never edit earlier messages).
  DeepSeek ignores `is_error`, so error results also start with `Error:`
- `src/solfish/tools/`: `@tool(ParamsModel)` + `ToolRegistry`; tool failures return `is_error` results, never raise
- `src/solfish/skills/`: one module per capability exposing `load(ctx) -> Skill`; register in `skills/__init__.py`
- `src/solfish/skills/publishers/`: one module per platform the content skill can do more than the
  outbox for. Nothing here may press a platform's publish button
- `src/solfish/session.py`: stored sessions, `SessionManager` (worker-thread turns, event fan-out,
  approvals any channel can answer); channels (CLI, web, Telegram) sit on top of it
- `src/solfish/server.py` + `src/solfish/web/`: FastAPI + SSE + plain JS, no build step. Model output
  is untrusted in the page: never render its raw HTML, keep the CSP strict
- `src/solfish/channels/telegram.py`: Telegram bot started by `serve` when `SOLFISH_TELEGRAM_TOKEN` is
  set; raw Bot API over httpx2 (what the anthropic SDK uses; plain httpx isn't installed)
- `src/solfish/app.py`: wiring + system prompt (stable cached block, then memory snapshot)
- `evals/tasks.json`: end-state checks (sql / tool_called / reply_contains_all / file_contains)
- `docs/design.md`: design decisions; update it when a decision changes

## Conventions
- New skill: add the module, register it, add offline tests in `tests/test_skills.py`, add eval tasks
- Side-effecting tools set `needs_approval=True`
- Nothing time-varying in the system prompt (breaks prompt caching)
- Personal data stays in `~/.solfish/` and is gitignored; the repo is public
