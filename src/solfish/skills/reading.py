"""Reading skill: check what the user learned from a book, then post it as a reading note.

The user reads every day and keeps public notes at yuxuanyan.com/reading. The order matters more
than the tools: first check their understanding (say what is right, wrong or missing), then draft
the note in their own voice, and post it only after they approve the exact text.

    list_books        GET  {site}/api/reading/books   the shelf, to find the book's slug
    add_reading_note  POST {site}/api/reading/notes   needs approval; public at once

The site checks the token against a stored hash, and the database function behind it can only add
a note to an existing book: it can't edit or delete notes, add books, or touch articles. See
docs/design.md, section 19.
"""

from __future__ import annotations

from datetime import date, datetime

import httpx2
from pydantic import BaseModel, Field

from ..config import Settings
from ..tools import ToolError, tool
from .base import Context, Skill

TIMEOUT_S = 30.0

transport: httpx2.BaseTransport | None = None  # tests swap in a MockTransport


class NoArgs(BaseModel):
    pass


class NoteArgs(BaseModel):
    book: str = Field(description="The book's slug, as shown by list_books.")
    thoughts: str = Field(
        "",
        max_length=20_000,
        description="The user's own thoughts, in their voice and language (Markdown). 2-6 sentences.",
    )
    quote: str = Field(
        "",
        max_length=5000,
        description="Exact words from the book, only if the user gave them. Never write one yourself.",
    )
    location: str = Field("", max_length=80, description="Where in the book, e.g. 'Ch. 3' or 'p. 120'.")
    read_on: date | None = Field(None, description="Day of the reading (YYYY-MM-DD). Default: today.")


def today() -> date:
    return datetime.now().astimezone().date()


def preview(args: NoteArgs) -> str:
    """The note as it will appear, shown when asking for approval."""
    head = " · ".join(x for x in (str(args.read_on or today()), args.book, args.location) if x)
    quote = "\n".join(f"> {line}" for line in args.quote.strip().splitlines())
    body = "\n\n".join(x for x in (quote, args.thoughts.strip()) if x)
    return f"Public reading note on yuxuanyan.com/reading/{args.book}\n{head}\n\n{body}"


def site(settings: Settings) -> str:
    return settings.blog.site.rstrip("/")


def list_books(settings: Settings) -> str:
    try:
        with httpx2.Client(transport=transport, timeout=TIMEOUT_S) as c:
            r = c.get(f"{site(settings)}/api/reading/books")
    except httpx2.HTTPError as e:
        raise ToolError(f"Couldn't reach the site: {e}") from None
    if not r.is_success:
        raise ToolError(f"The site said {r.status_code}: {r.text[:300]}")
    books = r.json().get("books", [])
    if not books:
        return "The shelf is empty. The user adds books at /admin/reading/books."
    return "\n".join(
        f"{b['slug']}: {b['title']}" + (f" ({b['author']})" if b.get("author") else "")
        + f" · {b['status']} · {b['noteCount']} notes"
        for b in books
    )


def add_note(settings: Settings, args: NoteArgs) -> str:
    if not settings.reading_token:
        raise ToolError("Reading notes aren't set up: set SOLFISH_READING_TOKEN in .env.")
    if not args.quote.strip() and not args.thoughts.strip():
        raise ToolError("A note needs a quote or thoughts.")
    try:
        with httpx2.Client(transport=transport, timeout=TIMEOUT_S) as c:
            r = c.post(
                f"{site(settings)}/api/reading/notes",
                headers={"Authorization": f"Bearer {settings.reading_token}"},
                json={
                    "book": args.book,
                    "readOn": str(args.read_on or today()),
                    "location": args.location,
                    "quote": args.quote,
                    "bodyMd": args.thoughts,
                },
            )
    except httpx2.TimeoutException:
        # The note may already be live, and posting again would add it twice.
        raise ToolError(
            f"The site didn't answer in {TIMEOUT_S:.0f}s. The note may be posted anyway: check "
            f"{site(settings)}/en/reading/{args.book} before trying again."
        ) from None
    except httpx2.HTTPError as e:
        raise ToolError(f"Couldn't reach the site: {e}") from None
    if r.status_code == 401:
        raise ToolError("The site refused the reading token; it may have been rotated.")
    if r.status_code == 404:
        raise ToolError(f"No book with the slug {args.book!r}. Check list_books.")
    if not r.is_success:
        raise ToolError(f"The site said {r.status_code}: {r.text[:300]}")
    return f"Posted: {r.json()['url']}"


INSTRUCTIONS = """\
The user reads every day and keeps public reading notes at yuxuanyan.com/reading. When they \
tell you what they read or learned, work in this order:

1. Check their understanding before anything else. Restate the idea in one sentence, then say \
plainly what is right, what is wrong or oversimplified, and what is missing. If you aren't sure \
what the book says or whether a fact holds, say so (search the web when you can) instead of \
guessing. Then ask at most two short questions that test whether it stuck, for example applying \
it to something from their own life or work. Be a sparring partner, not a lecturer.
2. When they are happy with their understanding, draft the note: where in the book; a quote only \
if they gave you the exact words (never write or recall one yourself and present it as the \
book's); and 2-6 sentences of thoughts in first person, in their voice and in the language or \
mix of languages they used, keeping their wording and including the corrected understanding and \
what they will do with it. Not a summary of the book. Leave out private details about other \
people.
3. Show the draft and ask. Call add_reading_note only after they agree to that text: the note is \
public as soon as it is posted. Use list_books to find the book's slug; you can't add books, so \
if it's missing, point them to /admin/reading/books."""


def load(ctx: Context) -> Skill:
    settings = ctx.settings

    @tool(NoArgs, name="list_books")
    def list_books_tool(args: NoArgs) -> str:
        """List the books on the user's reading shelf: slug, title, author, status, note count."""
        return list_books(settings)

    @tool(NoteArgs, needs_approval=True, preview=preview)
    def add_reading_note(args: NoteArgs) -> str:
        """Post a reading note to the user's site. Public at once; only after the user approved the text."""
        return add_note(settings, args)

    return Skill(name="reading", instructions=INSTRUCTIONS, tools=[list_books_tool, add_reading_note])
