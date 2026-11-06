"""Forms skill: fill web forms (arrival cards, sign-ups, applications) from the user's profile.

The profile is a key/value table. Values marked sensitive (passport number, date of birth...)
never reach the model: it writes placeholders like {{passport_no}} and fill_form substitutes
them locally, right before typing into the page; `{{date_of_birth|DD/MM/YYYY}}` reformats a
date without the model seeing it. Anything the tools return is scrubbed of sensitive values,
including browser error messages. Set sensitive values with
`solfish profile set <key> --sensitive`, which reads them from the terminal, not the chat.

The browser is a visible Chromium window driven by Playwright (optional dependency), so the
user watches it fill. The agent can click buttons that move through a form (Next, Add), but
never the final one: buttons whose text says submit, confirm, pay or reset are refused in
code, so captchas, declarations and the final click stay with the human.
"""

from __future__ import annotations

import atexit
import contextlib
import difflib
import re
import sqlite3
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ..config import Settings
from ..tools import ToolError, tool
from .base import Context, Skill

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    sensitive INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

KEY = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
# {{key}} or {{key|DD/MM/YYYY}}
PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*(?:\|\s*([^}]*?)\s*)?\}\}")
DATE_TOKENS = re.compile(r"YYYY|YY|MM|DD")
MASK = "***"
MAX_FIELDS = 150
MAX_OPTIONS = 30
TRUTHY = {"true", "yes", "y", "1", "on", "checked"}
# Buttons the agent may never click: the final, binding or destructive ones. Checked against the
# button's visible text, in code, so a confused model can't talk itself past it.
# English words match at a word start, so "submitRegistration" counts and "design" doesn't.
FINAL_BUTTON = re.compile(
    r"\b(submit|send|confirm|pay|checkout|purchase|book|register|apply|finish|complete|declare|agree|"
    r"sign|reset|clear|delete|cancel|hantar|sahkan)|提交|确认|确定|支付|付款|注册|申请|同意|删除|重置|清空|取消",
    re.IGNORECASE,
)
FILL_TIMEOUT_MS = 5000

# Tags every visible field with data-solfish="<n>" so fill_form can find it again, and
# describes it. Runs in the page.
READ_JS = """() => {
  const skip = ['hidden', 'submit', 'button', 'reset', 'image', 'file', 'password'];
  const visible = el => !el.disabled && el.getClientRects().length > 0;
  const out = [];
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = el.tagName === 'INPUT' ? (el.type || 'text').toLowerCase() : el.tagName.toLowerCase();
    if (skip.includes(type) || !visible(el)) continue;
    const ref = out.length;
    el.dataset.solfish = String(ref);
    const label = (el.labels && el.labels[0] && el.labels[0].innerText)
      || el.getAttribute('aria-label') || el.placeholder || el.name || el.id || '';
    out.push({
      ref, type, label: label.trim().replace(/\\s+/g, ' '), name: el.name || '',
      required: el.required, readonly: el.readOnly,
      value: type === 'checkbox' || type === 'radio' ? String(el.checked)
        : type === 'select' ? (el.selectedOptions[0] ? el.selectedOptions[0].text.trim() : '') : el.value,
      options: type === 'select' ? Array.from(el.options).map(o => o.text.trim()) : [],
    });
  }
  const buttons = 'button, input[type=submit], input[type=button], input[type=reset], [role=button]';
  for (const el of document.querySelectorAll(buttons)) {
    if (!visible(el)) continue;
    const ref = out.length;
    el.dataset.solfish = String(ref);
    const text = el.innerText || el.value || el.getAttribute('aria-label') || el.title || '';
    out.push({ref, type: 'button', label: text.trim().replace(/\\s+/g, ' '), name: el.name || '',
              required: false, readonly: false, value: '', options: []});
  }
  return out;
}"""
OPTIONS_JS = "el => Array.from(el.options).map(o => [o.text.trim(), o.value])"
VALUE_JS = """el => el.type === 'checkbox' || el.type === 'radio' ? String(el.checked)
  : el.tagName === 'SELECT' ? (el.selectedOptions[0] ? el.selectedOptions[0].text.trim() : '') : el.value"""
# Read-only inputs (date pickers) can't be typed into; set the value and fire the events the
# page's scripts listen for.
SET_JS = """(el, v) => {
  el.value = v;
  for (const t of ['input', 'change', 'blur']) el.dispatchEvent(new Event(t, {bubbles: true}));
}"""


def open_browser(settings: Settings, profile: str = "browser") -> Any:
    """Start a visible Chromium with a persistent profile and return its page.
    Tests replace this function, so nothing else touches a real browser."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ToolError(
            'Browser support is not installed. Run: pip install -e ".[forms]" && playwright install chromium'
        ) from None
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch_persistent_context(str(settings.home / profile), headless=False)
    except Exception as e:
        pw.stop()
        if "already in use" in str(e) or "existing browser session" in str(e):
            raise ToolError(
                "Another Solfish session is using the browser. Tell the user to quit that session "
                "(Ctrl-D in its terminal), then try again."
            ) from None
        raise ToolError(f"Could not start Chromium ({e}). Run: playwright install chromium") from None

    def close() -> None:
        with contextlib.suppress(Exception):
            browser.close()
        with contextlib.suppress(Exception):
            pw.stop()

    atexit.register(close)
    return browser.pages[0] if browser.pages else browser.new_page()


def set_value(db: sqlite3.Connection, key: str, value: str, sensitive: bool) -> None:
    if not KEY.match(key):
        raise ToolError(f"{key!r} is not a valid key; use snake_case like passport_no.")
    db.execute(
        "INSERT INTO profile (key, value, sensitive) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
        "value = excluded.value, sensitive = excluded.sensitive, updated_at = datetime('now', 'localtime')",
        (key, value.strip(), int(sensitive)),
    )
    db.commit()


def describe_profile(db: sqlite3.Connection) -> str:
    """The profile as the model may see it: sensitive values masked."""
    db.executescript(SCHEMA)
    rows = db.execute("SELECT key, value, sensitive FROM profile ORDER BY key").fetchall()
    return "\n".join(
        f"{r['key']}: {MASK + ' (sensitive, use {{' + r['key'] + '}})' if r['sensitive'] else r['value']}"
        for r in rows
    ) or "The profile is empty."


def format_date(key: str, value: str, fmt: str) -> str:
    """Reformat a YYYY-MM-DD profile value, e.g. to DD/MM/YYYY. Errors never include the value."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    if not m:
        raise ToolError(f"{{{{{key}}}}} is not stored as YYYY-MM-DD, so it can't be formatted.")
    y, mo, d = m.groups()
    parts = {"YYYY": y, "YY": y[2:], "MM": mo, "DD": d}
    return DATE_TOKENS.sub(lambda t: parts[t.group(0)], fmt)


def substitute(db: sqlite3.Connection, text: str) -> tuple[str, bool]:
    """Replace {{key}} / {{key|format}} placeholders with profile values. Returns the text and
    whether any sensitive value went into it."""
    rows = {r["key"]: r for r in db.execute("SELECT key, value, sensitive FROM profile")}
    keys = [k for k, _ in PLACEHOLDER.findall(text)]
    missing = sorted({k for k in keys if k not in rows})
    if missing:
        raise ToolError(f"No profile value for {', '.join(missing)}; ask the user or check list_profile.")

    def value(m: re.Match[str]) -> str:
        key, fmt = m.group(1), m.group(2)
        v = rows[key]["value"]
        return format_date(key, v, fmt) if fmt else v

    return PLACEHOLDER.sub(value, text), any(rows[k]["sensitive"] for k in keys)


def is_final(field: dict[str, Any]) -> bool:
    return bool(FINAL_BUTTON.search(f"{field['label']} {field['name']}"))


class SetProfileArgs(BaseModel):
    key: str = Field(description="snake_case name, e.g. full_name, nationality, passport_no.")
    value: str
    sensitive: bool = Field(
        default=False, description="True for identity numbers, birth date, phone: hidden from you afterwards."
    )


class OpenPageArgs(BaseModel):
    url: str = Field(description="http(s) or file:// URL of the form.")


class FieldValue(BaseModel):
    ref: int = Field(description="Field number from read_form.")
    value: str = Field(
        description="Text to enter, may contain {{key}} placeholders. For selects: an option's text. "
        "For checkboxes/radios: true or false."
    )


class FillFormArgs(BaseModel):
    fields: list[FieldValue] = Field(min_length=1)


class ClickArgs(BaseModel):
    ref: int = Field(description="Button number from read_form.")


class NoArgs(BaseModel):
    pass


def load(ctx: Context) -> Skill:
    db = ctx.db
    db.executescript(SCHEMA)
    # typed: sensitive strings put into the page this session (possibly reformatted), to scrub.
    state: dict[str, Any] = {"page": None, "fields": {}, "typed": set()}

    def page() -> Any:
        p = state["page"]
        if p is None or p.is_closed():
            raise ToolError("No form is open; call open_page first.")
        return p

    def secrets() -> list[str]:
        raw = {r[0] for r in db.execute("SELECT value FROM profile WHERE sensitive = 1") if r[0]}
        return sorted(raw | state["typed"], key=len, reverse=True)

    def scrub(text: str) -> str:
        """Last line of defence: whatever a tool returns, no sensitive value survives in it."""
        for s in secrets():
            text = text.replace(s, MASK)
        return text

    @tool(SetProfileArgs, needs_approval=True)
    def set_profile(args: SetProfileArgs) -> str:
        """Save or update one piece of the user's profile, for filling forms later."""
        set_value(db, args.key, args.value, args.sensitive)
        return f"Saved {args.key}{' (sensitive)' if args.sensitive else ''}."

    @tool(NoArgs)
    def list_profile(args: NoArgs) -> str:
        """Show the user's profile. Sensitive values are masked; refer to them as {{key}}."""
        return describe_profile(db)

    @tool(OpenPageArgs)
    def open_page(args: OpenPageArgs) -> str:
        """Open a web page in the user's visible browser window."""
        if not re.match(r"^(https?|file)://", args.url):
            raise ToolError("Only http(s) and file:// URLs can be opened.")
        if state["page"] is None or state["page"].is_closed():
            state["page"] = open_browser(ctx.settings)
        p = state["page"]
        try:
            p.goto(args.url, wait_until="domcontentloaded")
        except Exception as e:
            raise ToolError(f"Could not open {args.url}: {e}") from None
        state["fields"] = {}
        return f"Opened {p.title() or p.url}. Call read_form to see its fields."

    @tool(NoArgs)
    def read_form(args: NoArgs) -> str:
        """List the visible fields of the open page: number, label, type, current value, options.
        Call it again after the page changes (next step, new section)."""
        fields = page().evaluate(READ_JS)[:MAX_FIELDS]
        state["fields"] = {f["ref"]: f for f in fields}
        if not fields:
            return "No fillable fields are visible on this page."
        lines, buttons = [], []
        for f in fields:
            if f["type"] == "button":
                final = " (final/destructive: only the user may click this)" if is_final(f) else ""
                buttons.append(f"[{f['ref']}] {f['label'] or f['name'] or '(no text)'}{final}")
                continue
            flags = [f["type"]]
            if f["name"] and f["name"] != f["label"]:
                flags.append(f"name={f['name']}")
            flags += [k for k in ("required", "readonly") if f.get(k)]
            line = f"[{f['ref']}] {f['label'] or '(no label)'} ({', '.join(flags)})"
            if f["value"] and f["value"] != "false":
                line += f" = {f['value']!r}"
            if f["options"]:
                opts = f["options"][:MAX_OPTIONS]
                more = len(f["options"]) - len(opts)
                line += "\n    options: " + " | ".join(opts) + (f" (+{more} more)" if more > 0 else "")
            lines.append(line)
        if buttons:
            lines += ["Buttons (click_button):", *buttons]
        return scrub(f"{page().title()}\n" + "\n".join(lines))

    def fill_one(field: dict[str, Any], value: str) -> str:
        el = page().locator(f'[data-solfish="{field["ref"]}"]')
        if field["type"] == "button":
            raise ToolError("that's a button; use click_button")
        if field["type"] in ("checkbox", "radio"):
            el.set_checked(value.strip().lower() in TRUTHY)
        elif field["type"] == "select":
            options = el.evaluate(OPTIONS_JS)
            want = value.strip().lower()
            match = next((v for t, v in options if want in (t.lower(), v.lower())), None)
            if match is None:
                texts = {t.lower(): t for t, _ in options}
                close = [texts[t] for t in difflib.get_close_matches(want, texts, n=5, cutoff=0.4)]
                raise ToolError(f"no option {value!r}" + (f"; closest: {', '.join(close)}" if close else ""))
            el.select_option(value=match)
        elif field.get("readonly"):
            el.evaluate(SET_JS, value)
        else:
            el.fill(value, timeout=FILL_TIMEOUT_MS)
        return el.evaluate(VALUE_JS)

    @tool(FillFormArgs, needs_approval=True)
    def fill_form(args: FillFormArgs) -> str:
        """Fill fields of the open form, by number from read_form. Use {{key}} placeholders for
        profile values. Never submits; returns what each field now holds."""
        if not state["fields"]:
            raise ToolError("Call read_form first, so field numbers are known.")
        lines = []
        for fv in args.fields:
            field = state["fields"].get(fv.ref)
            if field is None:
                lines.append(f"[{fv.ref}] Error: no such field; call read_form again.")
                continue
            try:
                value, sensitive = substitute(db, fv.value)
                if sensitive:
                    state["typed"].add(value)
                now = fill_one(field, value)
            except ToolError as e:
                lines.append(f"[{fv.ref}] {field['label']}: Error: {e}")
            except Exception as e:  # page changed, element detached, browser closed...
                # Only the first line: Playwright's call log echoes the value it tried to type.
                first = str(e).splitlines()[0] if str(e) else ""
                lines.append(f"[{fv.ref}] {field['label']}: Error: {type(e).__name__}: {first}")
            else:
                shown = f"{MASK} (from {fv.value})" if sensitive else repr(now)
                lines.append(f"[{fv.ref}] {field['label']} = {shown}")
        return scrub("\n".join(lines))

    @tool(ClickArgs, needs_approval=True)
    def click_button(args: ClickArgs) -> str:
        """Click a button on the open page to move through the form (Next, Continue, Add another).
        Final buttons (submit, confirm, pay, reset...) are refused: the user clicks those."""
        field = state["fields"].get(args.ref)
        if field is None or field["type"] != "button":
            raise ToolError(f"No button [{args.ref}]; call read_form to see the buttons.")
        if is_final(field):
            raise ToolError(
                f"[{args.ref}] {field['label']!r} submits or clears the form; only the user may click it. "
                "Tell them it's ready for review."
            )
        p = page()
        try:
            p.locator(f'[data-solfish="{args.ref}"]').click(timeout=FILL_TIMEOUT_MS)
            p.wait_for_load_state("domcontentloaded")
        except Exception as e:
            first = str(e).splitlines()[0] if str(e) else ""
            raise ToolError(f"Click failed: {type(e).__name__}: {first}") from None
        state["fields"] = {}  # the page may have changed; numbers are stale
        return scrub(f"Clicked {field['label']!r}; now on {p.title() or p.url}. Call read_form again.")

    @tool(NoArgs)
    def screenshot_form(args: NoArgs) -> str:
        """Save a full-page screenshot of the open form to the workspace for the user to check."""
        out = ctx.settings.workspace / "forms" / f"{datetime.now():%Y%m%d-%H%M%S}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        page().screenshot(path=str(out), full_page=True)
        return f"Saved {out}"

    return Skill(
        name="forms",
        instructions=(
            "You fill web forms for the user in a browser window they can see. Start with "
            "list_profile, open_page and read_form. Fill only from the profile or from what the "
            "user told you in this conversation; never guess identity details. For sensitive "
            "values write the {{key}} placeholder; when a field wants another date format, "
            "write it after a bar, e.g. {{date_of_birth|DD/MM/YYYY}} (the field's placeholder "
            "usually shows the format). Never ask the user to paste them into the "
            "chat (they can run `solfish profile set <key> --sensitive`). Check fill_form's "
            "output and fix errors. Multi-step forms: click_button the Next/Continue button, "
            "then read_form again. You cannot and must not submit: when done, list any fields "
            "you left empty or were unsure about, and tell the user to review, solve any "
            "captcha and submit themselves."
        ),
        tools=[set_profile, list_profile, open_page, read_form, fill_form, click_button, screenshot_form],
    )
