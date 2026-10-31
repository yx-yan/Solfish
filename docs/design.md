# Design notes

Why Solfish is built the way it is. Each section is a decision, the alternatives, and the
trade-off.

## 1. A hand-written loop instead of a framework

The Anthropic SDK ships a tool runner, and frameworks like LangChain go further. Solfish writes
the loop itself (`agent.py`, about 150 lines) because the loop is where an agent's behaviour
is decided: when to stop, what happens on a refusal, how errors reach the model, when a human
is asked. Owning it keeps all of that visible and testable. The cost is handling every
`stop_reason` myself, which is also the point.

## 2. The history is append-only

The loop only ever appends: the model's full response (including thinking blocks), then the
tool results. It never trims, rewrites or reorders earlier turns. Two reasons:

- **Prompt caching** is a prefix match. Any edit to an earlier turn invalidates the cache for
  everything after it. With auto-caching on, each step of the loop reads the previous step
  from cache, which is visible in the trace as `cache_read` tokens.
- **Thinking blocks** must be passed back unchanged for the model to keep its reasoning across
  tool calls.

When a turn is interrupted mid-tool-call, the next turn *appends* "interrupted" results
instead of deleting the dangling call (`_close_dangling_tool_calls`). A test asserts that every
request's history is a prefix of the next one.

## 3. Prompt layout for caching

Order is `tools → system → messages`. Tools are sorted by name so the block is byte-identical
every request. The system prompt is two blocks: a stable block (persona + skill instructions,
with a cache breakpoint) and a per-session memory snapshot after it. The current time is
**not** in the prompt (it would break the cache every request); the model calls `current_time`
when it needs it.

## 4. Tools: one Pydantic model, two jobs

Each tool's parameters are a Pydantic model. `model_json_schema()` becomes the `input_schema`
sent to the API, and `model_validate()` checks what the model sends back. Schema and
validation can't drift apart.

Tool calls never raise into the loop. Invalid arguments, unknown tool names, `ToolError`s and
unexpected exceptions all become `tool_result` blocks with `is_error: true`, so the model can
read the message and correct itself (a test covers the model forgetting a required field).
Large outputs are truncated before they reach the context.

## 5. Skills keep the core general

A skill is `name + instructions + tools`. The loop, registry and CLI don't know what a job
application is. Adding a capability means writing one module and registering it in
`skills/__init__.py`. This is the seam where MCP servers will plug in as well: an MCP server's
tools become one more skill.

## 6. Human approval for side effects

Tools that write or delete are marked `needs_approval`. The loop asks the approver (a `y/N`
prompt in the CLI) before running them, and a refusal goes back to the model as an error
result, so it can ask the user what to do instead. Read-only tools run freely.

## 7. Memory: simple on purpose

Memory is a SQLite table of short notes. The 20 most recent are shown in the system prompt at
session start; older ones are found with keyword search (`recall`). This is deliberately not a
vector store yet: with tens or hundreds of notes, keyword search plus the model choosing good
keywords works, and it's transparent. Embedding search is the upgrade path once recall
quality shows up as a problem in evals.

## 8. Evals grade end state

The eval tasks (`evals/tasks.json`) check what the agent *did*, not what it *said*: rows in
the database, which tools were called, whether a duplicate was created. Wording varies run to
run; the end state shouldn't. Each run reports pass rate, tokens and cost per task, so a
prompt or tool change can be judged on numbers.

## 9. Model settings

- **DeepSeek by default, in Anthropic format.** `deepseek-flash` is the best price/quality
  for daily use (about $0.30 / $1.20 per million input / output tokens, cache hits $0.006).
  DeepSeek serves an Anthropic-compatible endpoint, so the loop keeps the anthropic SDK and
  message format, and moving to `deepseek-v4-pro` or back to Claude is a config change
  (`SOLFISH_MODEL`, `SOLFISH_BASE_URL`), not a rewrite.
- Thinking is on with effort `high` (configurable). With tools, DeepSeek requires thinking to
  be sent back unchanged, which the append-only history already does.
- **Compatibility gaps, handled in the loop.** DeepSeek ignores `tool_result.is_error`, so a
  failed tool's content also starts with `Error:`; otherwise the model could read an error as
  a normal result. It ignores `cache_control` too, but caches prefixes automatically, so the
  layout in section 3 still pays off and shows up as `cache_read` tokens.
- Server-side refusal fallback (`fallbacks: "default"`) is a Claude beta, off by default. With
  `fallbacks=True` the loop uses the beta endpoint, and if the main model declines, the API
  retries on another model in the same call.
- Non-streaming requests with `max_tokens=16000`; streaming is on the roadmap for the chat UI.

## 10. Résumé: the source repo is read-only

`build_resume` copies the résumé repo to a temp dir, writes the tailored `.tex` there and
compiles it, so the class file and fonts resolve by relative path exactly as in the real repo,
and the master copy can never be overwritten by the agent. Outputs land in the workspace. Two
guards matter more than they look:

- **Shell escape off.** The model writes the LaTeX, and `\write18` would let LaTeX run shell
  commands. XeLaTeX runs with `-no-shell-escape`.
- **Page count is fed back.** The résumé must fit on one page; the tool reads the page count
  from the log and tells the model to cut and rebuild. A deterministic check beats asking the
  model to estimate length.

The instructions forbid inventing experience. Tailoring means reordering and rewording what's
there; a gap goes back to the user.

## 11. Publishing: approve once, then run without the model

Posts are rows (`draft → scheduled → published | failed`). Approval happens at
`schedule_post`; `solfish publish-due` then publishes what's due with no model call, so cron
can run it cheaply and predictably. Letting the model wake up and decide to post would be the
wrong place for autonomy: what to post is a judgement call made with the user; when to post is
a timestamp.

Each platform has a `Publisher` (`post → location`). Most Chinese platforms have no posting
API for individuals, and browser automation against them breaks often and risks the account,
so the default publisher is an **outbox**: a folder with the text and media, ready to upload.
A platform that does have an API is one function in `PUBLISHERS`. Failures are stored on the
post, not lost.

Platform limits (小红书 title ≤ 20 characters, X ≤ 280) are checked in code when a draft is
saved, so the model gets an error it can fix instead of a post that fails later.

## 12. Research: grounded and paged

Papers come from the arXiv API (no key). Full text comes from arXiv's HTML rendering, stripped
to text (formulas kept as their LaTeX `alttext`) and served in pages of 15k characters: a whole
paper would be truncated by the 20k result limit, and paging lets the model read only as far as
it needs. The library is a table, so "what have I read about X" is a query over real records,
and the instructions require arXiv ids for every claim about a paper.

