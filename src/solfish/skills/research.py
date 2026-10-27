"""Research skill: find papers on arXiv, read them, and keep a reading library with notes.

arXiv needs no API key. Full text comes from arXiv's HTML rendering (available for most papers
since late 2023) and is paged, because a whole paper is larger than one tool result. The
library is a SQLite table, so "what have I read on X?" is a query, not a memory guess.

Literature reviews and idea notes are plain files in the workspace (core skill's write_file).
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from html.parser import HTMLParser
from typing import Literal

from pydantic import BaseModel, Field

from ..tools import ToolError, tool
from .base import Context, Skill

API = "https://export.arxiv.org/api/query"
ATOM = {"a": "http://www.w3.org/2005/Atom"}
PAGE_CHARS = 15_000
ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5}|[a-z\-]+(\.[A-Z]{2})?/\d{7})(v\d+)?$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY,
    arxiv_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    authors TEXT NOT NULL,
    published TEXT,
    abstract TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'to_read',
    notes TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def fetch(url: str) -> str:
    """GET a URL as text. Tests replace this function, so nothing else touches the network."""
    req = urllib.request.Request(url, headers={"User-Agent": "solfish-agent (research skill)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP {e.code} fetching {url}") from None
    except urllib.error.URLError as e:
        raise ToolError(f"Network error fetching {url}: {e.reason}") from None


def parse_feed(xml: str) -> list[dict[str, str]]:
    papers = []
    for e in ET.fromstring(xml).findall("a:entry", ATOM):
        raw_id = e.findtext("a:id", "", ATOM).rsplit("/abs/", 1)[-1]
        papers.append({
            "arxiv_id": re.sub(r"v\d+$", "", raw_id),
            "title": " ".join(e.findtext("a:title", "", ATOM).split()),
            "authors": ", ".join(a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)),
            "published": e.findtext("a:published", "", ATOM)[:10],
            "abstract": " ".join(e.findtext("a:summary", "", ATOM).split()),
        })
    return papers


class _Text(HTMLParser):
    """Collects visible text from a page, skipping scripts, styles and navigation."""

    SKIP = {"script", "style", "nav", "header", "footer", "math", "form", "button", "dialog"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            alt = dict(attrs).get("alttext")
            if tag == "math" and alt and not self.depth:
                self.parts.append(f" ${alt}$ ")  # arXiv HTML keeps the LaTeX source here
            self.depth += 1
        elif tag in ("p", "h1", "h2", "h3", "h4", "li", "tr", "figcaption"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    text = re.sub(r"[ \t]+", " ", "".join(p.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def checked_id(arxiv_id: str) -> str:
    arxiv_id = arxiv_id.strip().removeprefix("arXiv:")
    if not ARXIV_ID.match(arxiv_id):
        raise ToolError(f"{arxiv_id!r} is not an arXiv id (e.g. 2310.06825).")
    return arxiv_id


class SearchArgs(BaseModel):
    query: str = Field(
        min_length=1, description="Search terms, e.g. 'retrieval augmented generation abstention'."
    )
    max_results: int = Field(default=10, ge=1, le=30)
    sort: Literal["relevance", "newest"] = "relevance"


class ReadArgs(BaseModel):
    arxiv_id: str
    page: int = Field(default=1, ge=1, description="Full text is paged; ask for the next page to go on.")


class SaveArgs(BaseModel):
    arxiv_id: str
    note: str = Field(default="", description="Why it matters to the user's research.")


class NoteArgs(BaseModel):
    arxiv_id: str
    note: str | None = Field(default=None, description="Appended to the paper's notes, dated.")
    status: Literal["to_read", "reading", "read"] | None = None


class ListArgs(BaseModel):
    status: Literal["to_read", "reading", "read", "all"] = "all"
    query: str | None = Field(default=None, description="Keyword to match in title, abstract or notes.")


def load(ctx: Context) -> Skill:
    db = ctx.db
    db.executescript(SCHEMA)

    @tool(SearchArgs)
    def search_papers(args: SearchArgs) -> str:
        """Search arXiv. Returns id, title, authors, date and abstract for each hit."""
        params = urllib.parse.urlencode({
            # arXiv ORs bare words; requiring each one gives far more relevant hits.
            "search_query": " AND ".join(f"all:{w}" for w in args.query.split()),
            "max_results": args.max_results,
            "sortBy": "relevance" if args.sort == "relevance" else "submittedDate",
        })
        papers = parse_feed(fetch(f"{API}?{params}"))
        if not papers:
            return "No papers found. Try broader or different terms."
        return "\n\n".join(
            f"[{p['arxiv_id']}] {p['title']} ({p['published']})\n{p['authors']}\n{p['abstract']}"
            for p in papers
        )

    @tool(ReadArgs)
    def read_paper(args: ReadArgs) -> str:
        """Read a paper's full text from arXiv, one page at a time."""
        arxiv_id = checked_id(args.arxiv_id)
        try:
            text = html_to_text(fetch(f"https://arxiv.org/html/{arxiv_id}"))
        except ToolError as e:
            raise ToolError(
                f"No HTML full text for {arxiv_id} ({e}); search_papers has the abstract."
            ) from None
        pages = max(1, -(-len(text) // PAGE_CHARS))
        if args.page > pages:
            raise ToolError(f"{arxiv_id} has only {pages} page(s).")
        chunk = text[(args.page - 1) * PAGE_CHARS: args.page * PAGE_CHARS]
        return f"[{arxiv_id} page {args.page}/{pages}]\n{chunk}"

    @tool(SaveArgs)
    def save_paper(args: SaveArgs) -> str:
        """Add a paper to the user's reading library."""
        arxiv_id = checked_id(args.arxiv_id)
        if db.execute("SELECT 1 FROM papers WHERE arxiv_id = ?", (arxiv_id,)).fetchone():
            raise ToolError(f"{arxiv_id} is already in the library; use note_paper to add to it.")
        found = parse_feed(fetch(f"{API}?id_list={arxiv_id}"))
        if not found:
            raise ToolError(f"arXiv has no paper {arxiv_id}.")
        p = found[0]
        note = f"[{date.today()}] {args.note}" if args.note else ""
        db.execute(
            "INSERT INTO papers (arxiv_id, title, authors, published, abstract, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (arxiv_id, p["title"], p["authors"], p["published"], p["abstract"], note),
        )
        db.commit()
        return f"Saved {arxiv_id}: {p['title']}."

    @tool(NoteArgs)
    def note_paper(args: NoteArgs) -> str:
        """Add reading notes to a saved paper and/or change its reading status."""
        row = db.execute("SELECT * FROM papers WHERE arxiv_id = ?", (checked_id(args.arxiv_id),)).fetchone()
        if row is None:
            raise ToolError(f"{args.arxiv_id} isn't in the library; save_paper it first.")
        if args.note is None and args.status is None:
            raise ToolError("Nothing to update: give a note, a status, or both.")
        notes = row["notes"]
        if args.note:
            notes = f"{notes}\n[{date.today()}] {args.note}".strip()
        db.execute("UPDATE papers SET notes = ?, status = ? WHERE id = ?",
                   (notes, args.status or row["status"], row["id"]))
        db.commit()
        return f"Updated {row['arxiv_id']} ({args.status or row['status']})."

    @tool(ListArgs)
    def list_papers(args: ListArgs) -> str:
        """List papers in the reading library, with notes."""
        where, params = [], []
        if args.status != "all":
            where.append("status = ?")
            params.append(args.status)
        if args.query:
            where.append("(title LIKE ? OR abstract LIKE ? OR notes LIKE ?)")
            params += [f"%{args.query}%"] * 3
        sql = "SELECT * FROM papers" + (f" WHERE {' AND '.join(where)}" if where else "")
        rows = db.execute(sql + " ORDER BY id DESC", params).fetchall()
        lines = []
        for r in rows:
            line = f"[{r['arxiv_id']}] {r['title']} | {r['status']}"
            if r["notes"]:
                line += f"\n    notes: {r['notes'].replace(chr(10), ' / ')}"
            lines.append(line)
        return "\n".join(lines) or "No papers match."

    return Skill(
        name="research",
        instructions=(
            "You are the user's research assistant. Ground every claim about a paper in what you "
            "read through these tools, and cite arXiv ids; never invent papers, authors or "
            "results. To survey a topic, search with a few different phrasings, read the most "
            "relevant papers, then write the review to research/<topic>.md in the workspace with "
            "a comparison of methods, open problems and concrete next experiments. Save papers "
            "the user should keep, and record what you learned with note_paper."
        ),
        tools=[search_papers, read_paper, save_paper, note_paper, list_papers],
    )
