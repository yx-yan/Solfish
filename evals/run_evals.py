"""Run the eval suite against the real model API (DeepSeek by default).

Each task gets a fresh, empty Solfish home, optional setup SQL, one prompt, and deterministic
checks on the end state. Prints pass rate, steps, tokens and cost; saves full results to
evals/results/.

    python evals/run_evals.py                 # all tasks once
    python evals/run_evals.py --runs 3        # repeat to see variance
    python evals/run_evals.py --only jobs/    # filter by task id prefix
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from solfish.app import build_agent
from solfish.config import Settings, load_dotenv
from solfish.skills import content, forms, jobs, memory, research

HERE = Path(__file__).parent


def check(c: dict[str, Any], *, db: sqlite3.Connection, reply: str, tools: list[str],
          settings: Settings) -> tuple[bool, str]:
    if "sql" in c:
        row = db.execute(c["sql"]).fetchone()
        got = row[0] if row else None
        return got == c["equals"], f"{c['sql']} -> {got!r}, want {c['equals']!r}"
    if "tool_called" in c:
        return c["tool_called"] in tools, f"tool {c['tool_called']} called (got {tools})"
    if "reply_contains_all" in c:
        missing = [s for s in c["reply_contains_all"] if s.lower() not in reply.lower()]
        return not missing, f"reply missing {missing}" if missing else "reply ok"
    if "reply_excludes" in c:
        return c["reply_excludes"] not in reply, f"reply excludes {c['reply_excludes']!r}"
    if "file_contains" in c:
        rel, needle = c["file_contains"]
        f = settings.workspace / rel
        ok = f.is_file() and needle in f.read_text(encoding="utf-8")
        return ok, f"{rel} contains {needle!r}"
    raise ValueError(f"unknown check: {c}")


def run_task(task: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(home=Path(tmp))
        settings.ensure_dirs()
        db = sqlite3.connect(settings.db_path)
        db.executescript(jobs.SCHEMA + memory.SCHEMA + content.SCHEMA + research.SCHEMA + forms.SCHEMA)
        for stmt in task.get("setup", []):
            db.execute(stmt)
        db.commit()

        tools_called: list[str] = []
        agent, tracer = build_agent(
            settings,
            approve=lambda name, args: True,
            listeners=[lambda k, d: tools_called.append(d["name"]) if k == "tool_call" else None],
            trace=False,
        )
        started = time.monotonic()
        try:
            result = agent.run(task["prompt"])
            reply, steps, stop, error = result.text, result.steps, result.stop, None
        except Exception as e:  # record API failures as task failures, keep the suite going
            reply, steps, stop, error = "", 0, "error", f"{type(e).__name__}: {e}"

        checks = [
            check(c, db=db, reply=reply, tools=tools_called, settings=settings)
            for c in task["checks"]
        ] if error is None else [(False, error)]
        db.close()
        t = tracer.totals
        return {
            "id": task["id"],
            "passed": all(ok for ok, _ in checks),
            "checks": [{"ok": ok, "detail": d} for ok, d in checks],
            "stop": stop,
            "steps": steps,
            "tools": tools_called,
            "tool_errors": t.tool_errors,
            "tokens": t.tokens,
            "cost_usd": round(t.cost_usd, 5),
            "seconds": round(time.monotonic() - started, 1),
            "reply": reply,
        }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--only", default="", help="run tasks whose id starts with this")
    args = p.parse_args()
    load_dotenv()

    tasks = [t for t in json.loads((HERE / "tasks.json").read_text()) if t["id"].startswith(args.only)]
    results = []
    for run in range(args.runs):
        for task in tasks:
            r = run_task(task)
            r["run"] = run
            results.append(r)
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"{mark}  {r['id']:<28} steps={r['steps']} ${r['cost_usd']:.4f} {r['seconds']}s")
            for c in r["checks"]:
                if not c["ok"]:
                    print(f"      ✗ {c['detail']}")

    passed = sum(r["passed"] for r in results)
    cost = sum(r["cost_usd"] for r in results)
    print(f"\n{passed}/{len(results)} passed ({passed / len(results):.0%}) | total ${cost:.4f}")

    out = HERE / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"results: {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
