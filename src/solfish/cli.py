"""Command-line interface: `solfish` for a chat session, `solfish ask "..."` for one question."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import sys
from datetime import datetime
from typing import Any

import anthropic

from .agent import format_args
from .app import DEFAULT_SKILLS, build_agent, new_client, new_session_id
from .channels import telegram
from .config import Settings, home_dir, load_dotenv
from .session import SessionStore
from .skills import content, forms, open_db, spark
from .tools import ToolError
from .trace import Tracer

DIM, CYAN, RED, RESET = "\033[2m", "\033[36m", "\033[31m", "\033[0m"
PUBLISH_PROFILE = "browser-publish"  # kept apart from the forms skill, which may be running


def _preview(text: str, limit: int = 160) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def show(kind: str, data: dict[str, Any]) -> None:
    """Listener that prints the agent's work as it happens."""
    if kind == "thinking":
        print(f"{DIM}💭 {_preview(data['text'], 240)}{RESET}")
    elif kind == "tool_call":
        print(f"{CYAN}→ {data['name']}{RESET} {DIM}{_preview(format_args(data['input']))}{RESET}")
    elif kind == "tool_result":
        color = RED if data["is_error"] else DIM
        print(f"{color}← {_preview(data['output'])}{RESET}")
    elif kind == "fallback":
        print(f"{DIM}(main model declined; continued on {data['to']}){RESET}")


def ask_approval(name: str, args: dict[str, Any]) -> bool:
    try:
        shown = f"\n{args['preview']}\n" if "preview" in args else f"{format_args(args)}"
        answer = input(f"Allow {name} {shown}? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def handoff(settings: Settings, post_id: int) -> int:
    """Fill a post into its platform's page, leave publishing to the user, record the outcome."""
    db = open_db(settings)
    content.migrate(db)
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if post is None or post["status"] != "handoff":
        print(f"{RED}Post #{post_id} isn't waiting for you to publish it.{RESET}")
        return 1
    module = content.BROWSER.get(post["platform"])
    if module is None:
        print(f"{RED}No browser handoff for {post['platform']}; upload from {post['location']}.{RESET}")
        return 1
    try:
        page = forms.open_browser(settings, PUBLISH_PROFILE)
    except ToolError as e:
        print(f"{RED}{e}{RESET}")
        return 1
    print("If the page asks you to log in, scan the code; the post is filled in after that.")
    try:
        module.fill(page, settings, post)
        print("Filled in. Check it, press publish yourself, then close the browser window.")
    except Exception as e:  # any surprise on the page: stop there, and never try again by itself
        print(f"{RED}Stopped: {str(e).splitlines()[0]}{RESET}")
        print(f"Finish in the browser from {post['location']}, or close it to leave this for later.")
    with contextlib.suppress(Exception):  # closing the last window can drop the connection first
        page.wait_for_event("close", timeout=0)
    try:
        if input("Did you publish it? [y/N] ").strip().lower() not in ("y", "yes"):
            print(f"Left waiting; run `solfish handoff {post_id}` again when you're ready.")
            return 0
        url = input("Link to the post (Enter to skip): ").strip()
    except EOFError:
        return 0
    content.mark_published(db, post_id, url or None)
    print(f"Post #{post_id} is marked as published.")
    return 0


def summary(tracer: Tracer) -> str:
    t = tracer.totals
    tok = t.tokens
    line = (
        f"{t.llm_calls} model calls, {t.tool_calls} tool calls | tokens in {tok['input']} "
        f"(+{tok['cache_read']} cached) out {tok['output']} | ${t.cost_usd:.4f}"
    )
    if tracer.path:
        line += f"\ntrace: {tracer.path}"
    return line


def resume_hint(session_id: str) -> str:
    return f"resume: solfish --resume {session_id}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="solfish", description="Solfish personal agent")
    p.add_argument("--yes", action="store_true", help="approve every side-effecting tool call")
    p.add_argument("--skills", default=",".join(DEFAULT_SKILLS), help="comma-separated skills")
    p.add_argument("--quiet", action="store_true", help="hide thinking and tool calls")
    p.add_argument("--resume", metavar="SESSION", help="continue a stored session (see `solfish sessions`)")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("chat", help="interactive session (default)")
    ask = sub.add_parser("ask", help="one question, then exit")
    ask.add_argument("prompt", nargs="+")
    sub.add_parser("sessions", help="list recent sessions (no model)")
    srv = sub.add_parser("serve", help="local web UI on 127.0.0.1")
    srv.add_argument("--port", type=int, default=8765)
    srv.add_argument("--open", action="store_true", help="open it in the browser")
    sub.add_parser("publish-due", help="publish scheduled posts that are due (for cron; no model)")
    fill = sub.add_parser("handoff", help="fill a post into its platform's page; you press publish")
    fill.add_argument("id", type=int)
    done = sub.add_parser("mark-published", help="record that you published a post yourself (no model)")
    done.add_argument("id", type=int)
    done.add_argument("url", nargs="?", help="link to the live post")
    prof = sub.add_parser("profile", help="view or set form-filling details (no model)")
    prof.add_argument("action", choices=["list", "set"])
    prof.add_argument("key", nargs="?")
    prof.add_argument("--sensitive", action="store_true",
                      help="hide the value from the model; it is read here, not typed into chat")
    sub.add_parser("interests", help="what Solfish knows you like talking about (no model)")
    sp = sub.add_parser("spark", help="let Solfish start a conversation, then chat")
    sp.add_argument("--dry-run", action="store_true", help="print the opener without saving anything")
    args = p.parse_args(argv)
    load_dotenv()
    load_dotenv(home_dir() / ".env")  # cron doesn't start in the directory you run solfish from

    if args.cmd == "publish-due":
        settings = Settings()
        db = open_db(settings)
        lines = content.publish_due(settings, db)
        print("\n".join(lines) or "Nothing due.")
        for line in content.stuck_posts(db):  # printed, not sent: cron would repeat it every run
            print(f"{RED}{line}{RESET}")
        if lines:
            try:
                telegram.notify(settings, "Scheduled posts:\n" + "\n".join(lines))
            except telegram.TelegramError as e:
                print(f"{RED}Telegram: {e}{RESET}")
        return 0

    if args.cmd == "handoff":
        return handoff(Settings(), args.id)

    if args.cmd == "mark-published":
        db = open_db(Settings())
        content.migrate(db)
        try:
            content.mark_published(db, args.id, args.url)
        except ToolError as e:
            print(f"{RED}{e}{RESET}")
            return 1
        print(f"Post #{args.id} is marked as published.")
        return 0

    if args.cmd == "profile":
        db = open_db(Settings())
        db.executescript(forms.SCHEMA)
        if args.action == "set":
            if not args.key:
                p.error("profile set needs a key, e.g. solfish profile set passport_no --sensitive")
            value = getpass.getpass(f"{args.key}: ") if args.sensitive else input(f"{args.key}: ")
            try:
                forms.set_value(db, args.key, value, args.sensitive)
            except ToolError as e:
                print(f"{RED}{e}{RESET}")
                return 1
        print(forms.describe_profile(db))
        return 0

    if args.cmd == "interests":
        print(spark.describe(open_db(Settings())))
        return 0

    settings = Settings()
    skills = tuple(s.strip() for s in args.skills.split(",") if s.strip())

    if args.cmd == "spark" and args.dry_run:
        db = open_db(settings)
        db.executescript(spark.SCHEMA)
        interest = spark.pick(db, datetime.now())
        if interest is None:
            print("No interests yet. Tell Solfish what you like talking about.")
            return 0
        print(f"{DIM}{spark.seed_message(interest)}{RESET}\n")
        print(f"🐟 {spark.write_opener(new_client(settings), settings, db, interest)}")
        return 0

    if args.cmd == "serve":
        try:
            from .server import serve
        except ImportError:
            print(f"{RED}The web UI needs its extras: pip install -e \".[server]\"{RESET}")
            return 1
        return serve(settings, skills=skills, port=args.port, open_browser=args.open)

    store = SessionStore(open_db(settings))

    if args.cmd == "sessions":
        for r in store.recent():
            print(f"{r['id']}  {DIM}{r['updated_at']} {r['channel']:<8}{RESET} {r['title']}")
        return 0

    if args.cmd == "spark":
        started = spark.start(store, new_client(settings), settings, "cli")
        if started is None:
            print("No interests yet. Tell Solfish what you like talking about.")
            return 0
        session_id, opener = started
        history = store.load(session_id)
        print(f"\n🐟 {opener}\n")
    elif args.resume:
        if store.get(args.resume) is None:
            p.error(f"no session {args.resume}; see `solfish sessions`")
        session_id, history = args.resume, store.load(args.resume)
    else:
        session_id, history = new_session_id(), []
        store.create(session_id, "cli")

    agent, tracer = build_agent(
        settings,
        skills=skills,
        approve=(lambda name, a: True) if args.yes else ask_approval,
        listeners=[] if args.quiet else [show],
        session_id=session_id,
        messages=history,
    )

    def turn(text: str) -> None:
        try:
            result = agent.run(text)
        except anthropic.AuthenticationError:
            print(f"{RED}No valid credentials. Set DEEPSEEK_API_KEY.{RESET}")
            raise SystemExit(1) from None
        except anthropic.APIConnectionError:
            print(f"{RED}Network error talking to the model API.{RESET}")
            return
        except anthropic.APIStatusError as e:
            print(f"{RED}API error {e.status_code}: {e.message}{RESET}")
            return
        finally:
            store.sync(session_id, agent.messages)
        print(f"\n🐟 {result.text}\n")

    if args.cmd == "ask":
        turn(" ".join(args.prompt))
        print(f"{DIM}{summary(tracer)}\n{resume_hint(session_id)}{RESET}")
        return 0

    print(f"{DIM}Solfish — Ctrl-D or /exit to quit.{RESET}")
    while True:
        try:
            text = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text in ("/exit", "/quit"):
            break
        if text:
            try:
                turn(text)
            except KeyboardInterrupt:
                store.sync(session_id, agent.messages)
                print(f"\n{DIM}(interrupted){RESET}")
    print(f"{DIM}{summary(tracer)}\n{resume_hint(session_id)}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
