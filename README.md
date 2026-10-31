# 🐟 Solfish

**Solve + fish.** A general-purpose personal agent, built from scratch on the Anthropic Messages API, running on DeepSeek by default.

Solfish is the agent I use day to day. Its core is a hand-written agent loop with no agent
framework: tool calling, approval gates, long-term memory, tracing and evals are all in this
repo, small enough to read in one sitting. Capabilities plug in as **skills**: job search and
résumé tailoring, social-media publishing, and research (arXiv search, reading, a paper library).

> 中文简介：Solfish（solve + fish）是我自己从零写的通用个人 agent。不依赖 agent 框架，
> 核心循环、工具调用、记忆、追踪和评测都在这个仓库里。技能包括：求职与简历定制投递、
> 自媒体内容排期发布、科研（arXiv 检索、读论文、文献库）。

## What it looks like

<!-- TODO: replace with a real transcript or GIF from a live session -->
Illustrative session:

```text
you › I just applied to Acme for the AI Engineer role, and Globex invited me to an interview on Friday
💭 Two updates. Check the tracker first so I don't add duplicates.
→ list_applications {"status": "all"}
← #1 Globex | ML Engineer | applied | applied 2026-10-15
→ add_application {"company": "Acme", "role": "AI Engineer", "status": "applied"}
← Tracked #2: AI Engineer at Acme (applied).
→ update_application {"id": 1, "status": "interview", "note": "Interview on Friday"}
← Updated #1 (Globex): status applied -> interview.

🐟 Done: Acme is tracked as applied, and Globex has moved to interview (Friday is noted).

3 model calls, 3 tool calls | tokens in 412 (+5180 cached) out 236 | $0.0089
```

## Architecture

```mermaid
flowchart LR
    U[User / CLI] -->|message| L[Agent loop]
    L -->|system + tools + history| M[(DeepSeek API<br/>Anthropic format)]
    M -->|tool_use| L
    L -->|validate + approve| R[Tool registry]
    R --> S1[core: files, clock]
    R --> S2[memory: remember / recall]
    R --> S3[jobs + resume: tracker, LaTeX tailoring, email]
    R --> S4[content: drafts, schedule, publishers]
    R --> S5[research: arXiv, paper library]
    S2 & S3 & S4 & S5 --> DB[(SQLite)]
    C[cron: solfish publish-due] -.no model.-> S4
    L -.events.-> T[Tracer → JSONL]
    L -.events.-> P[CLI printer]
```

| Piece | File | What it shows |
|---|---|---|
| Agent loop | [`agent.py`](src/solfish/agent.py) | Every `stop_reason` handled (`tool_use`, `pause_turn`, `refusal`, `max_tokens`); step limit; append-only history |
| Tools | [`tools/`](src/solfish/tools/) | One Pydantic model per tool gives both the JSON Schema and input validation; failures go back to the model as `Error:` results |
| Skills | [`skills/`](src/solfish/skills/) | Tools + instructions bundled per capability; the core knows nothing about job hunting |
| Memory | [`skills/memory.py`](src/solfish/skills/memory.py) | Persistent notes; recent ones in the system prompt, older ones searchable |
| Safety | `needs_approval` flag | Writes and deletes wait for a human `y` |
| Observability | [`trace.py`](src/solfish/trace.py) | JSONL trace per session: every model call, tool call, token count, cache hit and dollar cost |
| Evals | [`evals/`](evals/) | Task suite graded on *end state* (database rows, tools called), not on wording |

Design decisions and trade-offs are written up in [`docs/design.md`](docs/design.md).

## Quick start

Requires Python 3.11+ and a DeepSeek API key (`DEEPSEEK_API_KEY`). Solfish uses DeepSeek's
Anthropic-compatible endpoint, so switching back to Claude is only environment variables
(see `.env.example`).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

solfish                                    # chat
solfish ask "what applications are still active?"
solfish --skills core,memory               # load only some skills
solfish publish-due                        # publish scheduled posts that are due (run from cron)
```

Optional setup per skill (see `.env.example`):

- **resume**: `SOLFISH_RESUME_DIR` points at a LaTeX résumé repo; needs XeLaTeX. Email
  applications need the `SOLFISH_SMTP_*` settings.
- **content**: schedule automatic publishing with cron, e.g.
  `*/15 * * * * solfish publish-due`.
- **research**: nothing; arXiv needs no key.

Data lives in `~/.solfish/` (SQLite database, workspace, traces) and never enters the repo.

## Tests and evals

```bash
pytest                        # offline: the agent loop runs against a scripted fake client
ruff check .
python evals/run_evals.py     # live: runs the task suite against the real API (costs money)
```

Unit tests cover the loop's control flow: parallel tool calls, bad arguments, declined
approvals, step limits, refusals, interrupted turns, and that history stays append-only.
The eval suite measures what the tests can't: whether the model, given these prompts and
tools, actually does the right thing.

## Roadmap

- [x] Agent loop, tool registry, approval gate, tracing
- [x] Skills: core (files, clock), memory, jobs (application tracker)
- [x] Offline tests and live eval suite
- [x] resume: tailor my LaTeX résumé to a JD, build the PDF (one-page check), apply by email
- [x] content: per-platform drafts, scheduling, outbox publisher, `publish-due` for cron
- [x] research: arXiv search, paged full-text reading, paper library with notes
- [ ] content: API publishers (X, Bilibili, the yuxuanyan.com blog)
- [ ] research: run experiments in a sandbox (code execution) and log results
- [ ] jobs: interview prep from a JD + my past notes
- [ ] MCP client: load tools from any MCP server as a skill
- [ ] Context management for long sessions (compaction)
- [ ] Telegram front end, so it's reachable from my phone
- [ ] Scheduled runs (morning brief: deadlines, follow-ups)

## License

MIT
