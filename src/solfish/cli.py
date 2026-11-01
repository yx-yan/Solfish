"""Command-line interface: `solfish` for a chat session, `solfish ask "..."` for one question."""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any

import anthropic

from .agent import format_args
from .app import DEFAULT_SKILLS, build_agent
from .config import Settings, load_dotenv
from .skills import content, forms, open_db
from .tools import ToolError
from .trace import Tracer

DIM, CYAN, RED, RESET = "\033[2m", "\033[36m", "\033[31m", "\033[0m"


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
        answer = input(f"Allow {name} {format_args(args)}? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="solfish", description="Solfish personal agent")
    p.add_argument("--yes", action="store_true", help="approve every side-effecting tool call")
    p.add_argument("--skills", default=",".join(DEFAULT_SKILLS), help="comma-separated skills")
    p.add_argument("--quiet", action="store_true", help="hide thinking and tool calls")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("chat", help="interactive session (default)")
    ask = sub.add_parser("ask", help="one question, then exit")
    ask.add_argument("prompt", nargs="+")
    sub.add_parser("publish-due", help="publish scheduled posts that are due (for cron; no model)")
    prof = sub.add_parser("profile", help="view or set form-filling details (no model)")
    prof.add_argument("action", choices=["list", "set"])
    prof.add_argument("key", nargs="?")
    prof.add_argument("--sensitive", action="store_true",
                      help="hide the value from the model; it is read here, not typed into chat")
    args = p.parse_args(argv)
    load_dotenv()

    if args.cmd == "publish-due":
        settings = Settings()
        for line in content.publish_due(settings, open_db(settings)) or ["Nothing due."]:
            print(line)
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

    agent, tracer = build_agent(
        skills=tuple(s.strip() for s in args.skills.split(",") if s.strip()),
        approve=(lambda name, a: True) if args.yes else ask_approval,
        listeners=[] if args.quiet else [show],
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
        print(f"\n🐟 {result.text}\n")

    if args.cmd == "ask":
        turn(" ".join(args.prompt))
        print(f"{DIM}{summary(tracer)}{RESET}")
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
                print(f"\n{DIM}(interrupted){RESET}")
    print(f"{DIM}{summary(tracer)}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
