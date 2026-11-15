"""Learning skill: the nightly check-in for the user's study plan at yuxuanyan.com/learning.

The plan is split into weeks, each with what counts as done. Every night Solfish looks at what the
user pushed that day, asks a few questions to check it stuck, then drafts the day's log in their
voice and posts it only after they approve the exact text. On the last day of a week it also
drafts the week's review.

    learning_plan          GET  {site}/api/learning/tracks   plans, this week, what counts as done
    todays_work            git log over SOLFISH_PROJECTS_DIR, every pushed branch, read-only
    add_learning_log       POST {site}/api/learning/logs     needs approval; public at once
    review_learning_week   POST {site}/api/learning/weeks    needs approval; status + review only

The token can add logs to an existing plan and set a week's status and review, nothing else: the
week's title, goal and materials stay admin-only. See docs/design.md, section 21.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

import httpx2
from pydantic import BaseModel, Field

from ..config import Settings
from ..tools import ToolError, tool
from .base import Context, Skill
from .reading import site, today
from .resume import git, github_repo

TIMEOUT_S = 30.0
WORK_LIMIT = 12_000  # characters of commit history in all
BODY_LIMIT = 300  # per commit message body

transport: httpx2.BaseTransport | None = None  # tests swap in a MockTransport


class NoArgs(BaseModel):
    pass


class WorkArgs(BaseModel):
    days: int = Field(1, ge=1, le=14, description="1 = since midnight today; 7 = the past week.")


class LogArgs(BaseModel):
    track: str = Field(description="The plan's slug, as shown by learning_plan.")
    body: str = Field(
        min_length=1,
        max_length=20_000,
        description="The log in the user's voice and language (Markdown): did, stuck, a thought, "
        "tomorrow. 3-10 lines. Links only from todays_work or the user.",
    )
    minutes: int = Field(ge=0, le=1440, description="Time spent today, as the user said.")
    mode: Literal["learn", "deliver"] = Field(
        description="learn: worked it through themselves; deliver: shipped it with AI help."
    )
    logged_on: date | None = Field(None, description="The day (YYYY-MM-DD). Default: today.")


class ReviewArgs(BaseModel):
    track: str = Field(description="The plan's slug.")
    week_no: int = Field(ge=1, le=104)
    status: Literal["planned", "active", "done", "partial"] = Field(
        description="done: every 'done when' item met; partial: some; active: the week now running."
    )
    review: str = Field(
        "",
        max_length=10_000,
        description="Markdown: each 'done when' item and whether it was met, what moves to next "
        "week, one lesson. Empty when only marking a week active.",
    )


def preview_log(args: LogArgs) -> str:
    """The log as it will appear, shown when asking for approval."""
    mode = "learning" if args.mode == "learn" else "delivering"
    head = f"{args.logged_on or today()} · {args.minutes} min · {mode}"
    return f"Public learning log on yuxuanyan.com/learning/{args.track}\n{head}\n\n{args.body.strip()}"


def preview_review(args: ReviewArgs) -> str:
    head = f"Week {args.week_no} of yuxuanyan.com/learning/{args.track} → {args.status}"
    return f"{head}\n\n{args.review.strip()}" if args.review.strip() else head


def schedule_row(schedule_md: str, day: int) -> str | None:
    """Day `day` (1-7) of a week's day-by-day Markdown table, as 'Column: cell' pairs; the rows
    are in order, one per day. None if the table has no such row."""
    rows = [[c.strip().replace("\\|", "|") for c in line.strip().strip("|").split(" | ")]
            for line in schedule_md.splitlines() if line.strip().startswith("|")]
    rows = [r for r in rows if not all(set(c) <= set("-: ") for c in r)]  # drop the --- line
    if len(rows) < 2 or day >= len(rows):
        return None
    return "; ".join(f"{h}: {c}" for h, c in zip(rows[0], rows[day], strict=False) if c and c != "—")


def get_plan(settings: Settings) -> str:
    try:
        with httpx2.Client(transport=transport, timeout=TIMEOUT_S) as c:
            r = c.get(f"{site(settings)}/api/learning/tracks")
    except httpx2.HTTPError as e:
        raise ToolError(f"Couldn't reach the site: {e}") from None
    if not r.is_success:
        raise ToolError(f"The site said {r.status_code}: {r.text[:300]}")
    data = r.json()
    if not data.get("tracks"):
        return "No study plan yet. The user creates one at /admin/learning/tracks."
    out = [f"Today: {data['today']}"]
    for t in data["tracks"]:
        n = t.get("currentWeek")
        out += ["", f"# {t['slug']}: {t['title']} (started {t['startedOn']}, {t['logCount']} logs)"]
        out += [f"W{w['weekNo']} {w['title']} [{w['status']}]" for w in t["weeks"]]
        week = next((w for w in t["weeks"] if w["weekNo"] == n), None)
        if week is None:
            out.append("Today is outside this plan's weeks." if n else "This plan hasn't started yet.")
            continue
        day = (date.fromisoformat(data["today"]) - date.fromisoformat(t["startedOn"])).days % 7 + 1
        out += ["", f"## This week: W{n} {week['title']}, day {day} of 7",
                "Done when:", week["goalMd"] or "(not written)",
                "Materials:", week["materialsMd"] or "(none)"]
        schedule = week.get("scheduleMd", "")
        if today_row := schedule_row(schedule, day):
            out += [f"Today (day {day}): {today_row}"]
            if day > 1 and (yesterday := schedule_row(schedule, day - 1)):
                out += [f"Yesterday (day {day - 1}): {yesterday}"]
            out += ["Day by day:", schedule]
        if week["reviewMd"]:
            out += ["Review so far:", week["reviewMd"]]
        for log in t.get("recentLogs", []):
            out += ["", f"## Log {log['loggedOn']} ({log['minutes']} min, {log['mode']})", log["bodyMd"]]
    return "\n".join(out)


def repo_work(repo, since: str) -> tuple[str, str] | None:
    """The user's commits since `since` on any pushed branch, as (remote, text); None if none."""
    try:
        git(repo, "fetch", "--quiet", "--prune", "origin")
        stale = ""
    except ToolError:
        stale = " (couldn't fetch; local history only)"
    try:
        authors = [f"--author={git(repo, 'config', key)}" for key in ("user.email", "user.name")]
        # Every branch pushed to origin, so today's unmerged pull requests count too
        log = git(repo, "log", "--exclude=origin/HEAD", "--remotes=origin", "--no-merges", f"--since={since}",
                  *authors, "--date=format:%Y-%m-%d %H:%M", "--source", "--format=%x1e%ad %S: %s%n%b")
    except ToolError:
        return None
    commits = []
    for record in filter(None, (r.strip() for r in log.split("\x1e"))):
        head, _, body = record.partition("\n")
        body = " ".join(body.split())
        commits.append(f"- {head.replace('origin/', '')}" + (f"\n  {body[:BODY_LIMIT]}" if body else ""))
    if not commits:
        return None
    try:
        remote = github_repo(git(repo, "remote", "get-url", "origin")) or repo.name
    except ToolError:
        remote = repo.name
    head = f"## {remote}{stale}: {len(commits)} commit(s), newest first (time branch: subject)"
    return remote, "\n".join([head, *commits])


def get_work(settings: Settings, days: int, now: datetime | None = None) -> str:
    root = settings.projects_dir
    if root is None or not root.is_dir():
        raise ToolError("Set SOLFISH_PROJECTS_DIR to the folder that holds the project clones.")
    now = now or datetime.now()
    since = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00")
    found: dict[str, str] = {}
    for repo in sorted(d for d in root.iterdir() if (d / ".git").exists()):
        if (work := repo_work(repo, since)) and len(work[1]) > len(found.get(work[0], "")):
            found[work[0]] = work[1]  # extra clones of one repo count once
    when = "today" if days == 1 else f"in the last {days} days"
    if not found:
        return f"No pushed commits {when} across the projects. Ask what they worked on instead."
    share = WORK_LIMIT // len(found)
    parts = [t if len(t) <= share else t[:share] + "\n  …(cut)" for t in found.values()]
    return f"Pushed commits {when} (since {since}):\n\n" + "\n\n".join(parts)


def post(settings: Settings, path: str, payload: dict, not_found: str) -> dict:
    if not settings.learning_token:
        raise ToolError("Learning logs aren't set up: set SOLFISH_LEARNING_TOKEN in .env.")
    try:
        with httpx2.Client(transport=transport, timeout=TIMEOUT_S) as c:
            r = c.post(f"{site(settings)}{path}", json=payload,
                       headers={"Authorization": f"Bearer {settings.learning_token}"})
    except httpx2.TimeoutException:
        # It may be saved anyway, and posting again would add the log twice.
        raise ToolError(
            f"The site didn't answer in {TIMEOUT_S:.0f}s. It may be saved anyway: check "
            f"{site(settings)}/en/learning/{payload['track']} before trying again."
        ) from None
    except httpx2.HTTPError as e:
        raise ToolError(f"Couldn't reach the site: {e}") from None
    if r.status_code == 401:
        raise ToolError("The site refused the learning token; it may have been rotated.")
    if r.status_code == 404:
        raise ToolError(not_found)
    if not r.is_success:
        raise ToolError(f"The site said {r.status_code}: {r.text[:300]}")
    return r.json()


INSTRUCTIONS = """\
The user follows a study plan for forward deployed engineer skills and keeps a public daily log \
at yuxuanyan.com/learning. Each week has "done when" items. A check-in, whether it is the nightly \
one or they bring it up, goes in this order:

1. Look first: learning_plan for this week, its "done when" and today's row of the day-by-day \
schedule, and todays_work for what they pushed.
2. Open in Chinese, briefly: what you see they did today, against today's row and this week's goal. Ask what \
else they did, about how long it took, and whether they worked it through themselves or shipped \
it with AI help (that decides the mode). If nothing was pushed, ask without judging: a rest day \
or a three-line log is fine, a skipped day only matters twice in a row.
3. Check that it stuck: at most three short questions about today's topic, from their own work \
where you can (why this design, what breaks if…, explain it to a client). Wait for the answers, \
then say plainly what is right, what is off and what is missing. If unsure of a fact, say so or \
search instead of guessing. A sparring partner, not a lecturer.
4. Draft the log in first person, in their voice and in the language mix they used: what they \
did (with commit or pull request links only from todays_work or from them), where they got stuck, \
one thought including any corrected understanding, and tomorrow's step. 3-10 lines; never invent \
work, numbers or links. Show it with the minutes and mode, and call add_learning_log only after \
they agree to that text: it is public at once.
5. On day 7 of a week, after the log: call todays_work with days=7, compare the week with each \
"done when" item, and draft the review (each item met or not, what moves to next week, one \
lesson) with status done or partial. After they approve, call review_learning_week for it, then \
mark the next week active with an empty review. If the plan itself needs changing, say so: \
titles, goals and materials are edited by the user at /admin/learning."""

# The nightly check-in (Telegram /learn, or from the server at SOLFISH_LEARNING_AT). It goes in the
# user turn, so the cached system prompt stays the same as in any other session.
CHECKIN_PROMPT = """Nightly learning check-in for {day}. Follow the check-in steps: look at the plan \
and today's work, then open the conversation with me. Keep the first message short."""


# The morning brief (Telegram /today, or from the server at SOLFISH_LEARNING_BRIEF_AT). One message,
# no questions: it is read on the phone before the day starts.
BRIEF_PROMPT = """Morning learning brief for {day}. Call learning_plan, then reply in Chinese with one \
short message and no questions:
- Week n, day d of 7: the week's title, and which "done when" items still look open given the logs.
- Today, from today's row of the day-by-day schedule: the build task (about two hours) and the \
reading (about an hour), each made concrete enough to start on (which repo, file, command, chapter \
or link from the materials). If the latest log shows yesterday's row unfinished, put what is left \
of it first and say what slips; if there is no schedule, pick up the latest log's "tomorrow" line.
- Anything else in today's row (LeetCode, the weekly review at 10 pm).
- On day 7, add that tonight's check-in includes the weekly review.
If today is outside the plan, say so in one line. Never invent material that isn't in the plan."""


def load(ctx: Context) -> Skill:
    settings = ctx.settings

    @tool(NoArgs)
    def learning_plan(args: NoArgs) -> str:
        """The user's study plans: every week with its status, and this week's 'done when' items,
        materials and day of the week. Public data."""
        return get_plan(settings)

    @tool(WorkArgs)
    def todays_work(args: WorkArgs) -> str:
        """The user's commits pushed today (or over the last few days) on any branch of the
        projects in SOLFISH_PROJECTS_DIR, newest first, with the branch. Read-only."""
        return get_work(settings, args.days)

    @tool(LogArgs, needs_approval=True, preview=preview_log)
    def add_learning_log(args: LogArgs) -> str:
        """Post today's learning log to the user's site. Public at once; only after the user
        approved the text."""
        data = post(settings, "/api/learning/logs",
                    {"track": args.track, "loggedOn": str(args.logged_on or today()), "minutes": args.minutes,
                     "mode": args.mode, "bodyMd": args.body},
                    f"No plan {args.track!r}, or the date is before it starts. Check learning_plan.")
        return f"Posted: {data['url']}"

    @tool(ReviewArgs, needs_approval=True, preview=preview_review)
    def review_learning_week(args: ReviewArgs) -> str:
        """Set one week's status and review on the user's site. Public at once; only after the
        user approved it."""
        data = post(settings, "/api/learning/weeks",
                    {"track": args.track, "weekNo": args.week_no, "status": args.status,
                     "reviewMd": args.review},
                    f"No week {args.week_no} in {args.track!r}. Check learning_plan.")
        return f"Saved: {data['url']}"

    return Skill(name="learning", instructions=INSTRUCTIONS,
                 tools=[learning_plan, todays_work, add_learning_log, review_learning_week])
