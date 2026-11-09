"""Conversations Solfish starts itself, at random times through the day, like a friend texting.

The aim is to get to know the user, help them grow, and find things to discuss and learn
together. Each spark is one of four kinds:

    interest  one specific point about a topic they like
    goal      how it's going with something they want to get better at
    followup  something they said in the last few days
    checkin   how their day is going

    plan_day (random times) -> may_send (gap, back-off, not mid-chat) -> choose a card
      -> one model call writes the opener -> a seeded session: [user: "[spark] ...", assistant: opener]

The seed starts with a user message so the history stays valid and append-only: the user's reply
is simply the next turn. See docs/design.md, section 17.
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from ..config import Settings
from ..tools import ToolError, tool
from .base import Context, Skill, open_db
from .memory import recent_memories

if TYPE_CHECKING:
    from ..session import SessionStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS interests (
    id INTEGER PRIMARY KEY,
    topic TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'interest' CHECK (kind IN ('interest', 'goal')),
    evidence TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    weight REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'muted')),
    last_sparked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS sparks (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    interest_id INTEGER REFERENCES interests(id),
    session_id TEXT NOT NULL,
    opener TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
"""

# An interest or goal sparked this long ago counts as fully fresh again.
FRESH_AFTER_H = 72
# Weekday hours when the user is at work and answers late: sparks there are things to come
# back to, not questions that need an answer now.
WORK_HOURS = (9, 18)
# Least time between two sparks; doubles for each spark in a row the user didn't answer.
MIN_GAP = timedelta(minutes=45)
MAX_BACKOFF = 4  # 45 min x 16 = 12 h
# The user wrote something this recently: they're mid-conversation, so don't cut in.
QUIET_AFTER_ACTIVITY = timedelta(minutes=30)
FOLLOWUP_DAYS = 3

# kind -> weight, at work and otherwise. Kinds with nothing to say about are left out.
WEIGHTS = {
    "work": {"interest": 0.7, "goal": 0.15, "followup": 0.15, "checkin": 0.0},
    "free": {"interest": 0.35, "goal": 0.2, "followup": 0.25, "checkin": 0.2},
}

OPENER_PROMPT = """\
You are Solfish, the user's personal agent and a curious friend, and you are texting them \
first, unprompted. Your aim is to get to know them better, help them grow, and find things \
worth discussing and learning together.

- Write it like a text from a friend: casual and warm, at most three short sentences.
- Be specific, and connect to what you know about them.
- End with one question that is easy and interesting to answer.
- No greeting formula, no lists, no "as an AI". Don't repeat what you asked recently.
- Never invent facts, links, events, or things the user said.
- Write in the language of the user's recent messages; if there are none, the note's language."""


@dataclass(frozen=True)
class Card:
    """What a spark is about: `seed` opens the session, `note` tells the model what to write."""
    kind: str
    seed: str
    note: str
    interest_id: int | None = None


class AddInterestArgs(BaseModel):
    topic: str = Field(description="Short name, e.g. 'MLX quantization' or 'land an AI engineer job'.")
    kind: Literal["interest", "goal"] = Field(
        "interest", description="interest: a topic they enjoy. goal: something they want to get better at."
    )
    evidence: str = Field("", description="One line on why: what the user said or did that shows it.")


class NoArgs(BaseModel):
    pass


class InterestIdArgs(BaseModel):
    id: int = Field(description="Id of the interest or goal, as shown by list_interests.")


def migrate(db: sqlite3.Connection) -> None:
    """Create the tables, and bring a database from the first version of this skill up to date."""
    db.executescript(SCHEMA)
    if "kind" not in {r["name"] for r in db.execute("PRAGMA table_info(interests)")}:
        db.execute("ALTER TABLE interests ADD COLUMN kind TEXT NOT NULL DEFAULT 'interest'")
    if "kind" not in {r["name"] for r in db.execute("PRAGMA table_info(sparks)")}:
        # interest_id was NOT NULL, and SQLite can't relax that in place: rebuild the table.
        db.execute("ALTER TABLE sparks RENAME TO sparks_old")
        db.executescript(SCHEMA)
        db.execute("INSERT INTO sparks (id, kind, interest_id, session_id, opener, sent_at) "
                   "SELECT id, 'interest', interest_id, session_id, opener, sent_at FROM sparks_old")
        db.execute("DROP TABLE sparks_old")
    db.commit()


def describe(db: sqlite3.Connection) -> str:
    migrate(db)
    rows = db.execute(
        "SELECT * FROM interests WHERE status = 'active' ORDER BY kind = 'goal' DESC, weight DESC, id"
    ).fetchall()
    return "\n".join(
        f"#{r['id']} {'🎯 ' if r['kind'] == 'goal' else ''}{r['topic']} (weight {r['weight']:g}"
        + (f", last sparked {r['last_sparked_at']}" if r["last_sparked_at"] else "") + ")"
        + (f": {r['evidence']}" if r["evidence"] else "")
        for r in rows
    ) or "No interests yet."


# ---------- when ----------

def plan_day(day: date, rng: random.Random, per_day: tuple[int, int],
             hours: tuple[int, int]) -> list[datetime]:
    """Random times for the day's sparks, between `hours` (start inclusive, end exclusive)."""
    start = datetime.combine(day, time(hours[0]))
    span = (hours[1] - hours[0]) * 3600
    count = rng.randint(*per_day)
    return sorted(start + timedelta(seconds=rng.randrange(span)) for _ in range(count))


def unanswered_streak(db: sqlite3.Connection) -> int:
    """How many of the latest sparks in a row got no reply."""
    streak = 0
    for (replies,) in db.execute(
        "SELECT (SELECT COUNT(*) FROM session_messages m WHERE m.session_id = s.session_id) - 2 "
        "FROM sparks s ORDER BY s.id DESC LIMIT ?", (MAX_BACKOFF,)
    ):
        if replies > 0:
            break
        streak += 1
    return streak


def may_send(db: sqlite3.Connection, now: datetime) -> bool:
    """Not too soon after the last spark (longer when ignored), and not while the user is chatting."""
    (last,) = db.execute("SELECT MAX(sent_at) FROM sparks").fetchone()
    gap = MIN_GAP * 2 ** unanswered_streak(db)
    if last is not None and now - datetime.fromisoformat(last) < gap:
        return False
    (active,) = db.execute("SELECT MAX(updated_at) FROM sessions").fetchone()
    return active is None or now - datetime.fromisoformat(active) >= QUIET_AFTER_ACTIVITY


# ---------- what ----------

def at_work(now: datetime) -> bool:
    return now.weekday() < 5 and WORK_HOURS[0] <= now.hour < WORK_HOURS[1]


def pick(db: sqlite3.Connection, now: datetime, kind: str = "interest") -> sqlite3.Row | None:
    """The active interest (or goal) with the highest weight x freshness."""
    def score(r: sqlite3.Row) -> float:
        if r["last_sparked_at"] is None:
            return r["weight"]
        hours = (now - datetime.fromisoformat(r["last_sparked_at"])).total_seconds() / 3600
        return r["weight"] * min(1.0, max(0.0, hours) / FRESH_AFTER_H)

    rows = db.execute("SELECT * FROM interests WHERE status = 'active' AND kind = ? ORDER BY id",
                      (kind,)).fetchall()
    best = max(rows, key=score, default=None)
    return best if best is not None and score(best) > 0 else None


def recent_user_lines(db: sqlite3.Connection, now: datetime, limit: int = 12) -> list[str]:
    """What the user typed in the last few days, oldest first (no tool results, no spark seeds)."""
    since = (now - timedelta(days=FOLLOWUP_DAYS)).isoformat(timespec="seconds")
    rows = db.execute(
        "SELECT m.content FROM session_messages m JOIN sessions s ON s.id = m.session_id "
        "WHERE m.role = 'user' AND s.updated_at >= ? ORDER BY s.updated_at DESC, m.seq DESC",
        (since,),
    ).fetchall()
    lines = []
    for (raw,) in rows:
        text = json.loads(raw)
        if isinstance(text, str) and not text.startswith("[spark]"):
            lines.append(" ".join(text.split())[:300])
            if len(lines) == limit:
                break
    return lines[::-1]


def choose(db: sqlite3.Connection, now: datetime, rng: random.Random) -> Card | None:
    """Pick what to talk about. None when there's nothing worth sending."""
    work = at_work(now)
    cards: dict[str, Card] = {}
    for kind in ("interest", "goal"):
        row = pick(db, now, kind)
        if row is not None:
            label = "goal: " if kind == "goal" else ""
            why = f"\nWhy: {row['evidence']}" if row["evidence"] else ""
            ask = ("Ask how it's going, or offer one concrete next step or a question to reflect on."
                   if kind == "goal" else
                   "One specific point (an idea, a question, a link to something they do), "
                   "not the topic in general.")
            cards[kind] = Card(kind, f"[spark] {label}{row['topic']}",
                               f"A {kind} of theirs: {row['topic']}{why}\n{ask}", row["id"])
    if recent_user_lines(db, now):
        cards["followup"] = Card("followup", "[spark] follow-up",
                                 "Follow up on one thing they said recently: how it went, what they "
                                 "decided, what they think now.")
    today = datetime.combine(now.date(), time()).isoformat(timespec="seconds")
    if not db.execute("SELECT 1 FROM sparks WHERE kind = 'checkin' AND sent_at >= ?", (today,)).fetchone():
        cards["checkin"] = Card("checkin", "[spark] check-in",
                                "Check in on how their day is going, or what they're up to.")

    weights = WEIGHTS["work" if work else "free"]
    kinds = [k for k in cards if weights[k] > 0]
    if not kinds:
        return None
    card = cards[rng.choices(kinds, [weights[k] for k in kinds])[0]]
    if work:
        card = Card(card.kind, card.seed, card.note + "\nThey're probably at work and will answer "
                    "later: make it something worth coming back to.", card.interest_id)
    return card


def write_opener(client: Any, settings: Settings, db: sqlite3.Connection, card: Card,
                 now: datetime) -> str:
    """One model call, no tools: the opening message for this card."""
    system = OPENER_PROMPT
    notes = recent_memories(db)
    if notes:
        system += "\n\nWhat you remember about them:\n" + "\n".join(f"- {r['content']}" for r in notes)
    lines = recent_user_lines(db, now)
    if lines:
        system += "\n\nWhat they said in the last few days:\n" + "\n".join(f"- {s}" for s in lines)
    asked = db.execute("SELECT opener FROM sparks ORDER BY id DESC LIMIT 5").fetchall()
    if asked:
        system += "\n\nWhat you asked recently:\n" + "\n".join(f"- {r['opener']}" for r in asked)
    response = client.messages.create(
        model=settings.model,
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": f"{card.note}\n\nTheir local time: {now:%A %H:%M}"}],
    )
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise ToolError("The model returned an empty opener.")
    return text


def start(store: SessionStore, client: Any, settings: Settings, channel: str,
          now: datetime | None = None, rng: random.Random | None = None) -> tuple[str, str] | None:
    """Choose a card, write an opener and open a session with it: (session id, opener).
    None when there is nothing to talk about."""
    from ..app import new_session_id  # app imports the skills package

    now = now or datetime.now()
    db = open_db(settings)  # its own connection: this runs on a thread of its own
    migrate(db)
    card = choose(db, now, rng or random.Random())
    if card is None:
        return None
    opener = write_opener(client, settings, db, card, now)
    session_id = new_session_id()
    store.create(session_id, channel)
    store.sync(session_id, [
        {"role": "user", "content": card.seed},
        # Blocks, not a bare string: that's the shape the loop stores assistant turns in.
        {"role": "assistant", "content": [{"type": "text", "text": opener}]},
    ])
    stamp = now.isoformat(timespec="seconds")
    with db:
        if card.interest_id is not None:
            db.execute("UPDATE interests SET last_sparked_at = ? WHERE id = ?", (stamp, card.interest_id))
        db.execute(
            "INSERT INTO sparks (kind, interest_id, session_id, opener, sent_at) VALUES (?, ?, ?, ?, ?)",
            (card.kind, card.interest_id, session_id, opener, stamp),
        )
    return session_id, opener


def load(ctx: Context) -> Skill:
    db = ctx.db
    migrate(db)

    @tool(AddInterestArgs)
    def add_interest(args: AddInterestArgs) -> str:
        """Note a topic the user enjoys, or a goal they're working on. Solfish uses these to start
        conversations and to check in on progress."""
        topic = " ".join(args.topic.split())
        if not topic:
            raise ToolError("The topic is empty.")
        row = db.execute(
            "SELECT id FROM interests WHERE lower(topic) = lower(?) AND kind = ?", (topic, args.kind)
        ).fetchone()
        if row is not None:
            with db:
                db.execute("UPDATE interests SET weight = weight + 0.5, status = 'active' WHERE id = ?",
                           (row["id"],))
            return f"{args.kind.capitalize()} #{row['id']} already existed; gave it more weight."
        with db:
            cur = db.execute("INSERT INTO interests (topic, kind, evidence) VALUES (?, ?, ?)",
                             (topic, args.kind, args.evidence.strip()))
        return f"Saved {args.kind} #{cur.lastrowid}."

    @tool(NoArgs)
    def list_interests(_: NoArgs) -> str:
        """The user's active goals (🎯) and interests, strongest first."""
        return describe(db)

    @tool(InterestIdArgs, needs_approval=True)
    def mute_interest(args: InterestIdArgs) -> str:
        """Stop starting conversations about an interest or goal (the user is tired of it, or done)."""
        with db:
            cur = db.execute("UPDATE interests SET status = 'muted' WHERE id = ?", (args.id,))
        if cur.rowcount == 0:
            raise ToolError(f"No interest #{args.id}.")
        return f"Muted #{args.id}."

    return Skill(
        name="spark",
        instructions=(
            "You keep track of the user's interests and goals, and you start conversations "
            "yourself. The aim is to get to know them, help them grow, and find things to discuss "
            "and learn together.\n"
            "- When they ask you to do something, be brief and get it done.\n"
            "- When they're chatting, discussing or learning, talk like a curious friend: react "
            "to what they said, share your own view, and often end with one follow-up question "
            "(one, never a list). Don't interrogate; sometimes just answer.\n"
            "- Save what you learn: topics they enjoy with add_interest, things they want to get "
            "better at with add_interest kind=goal, other facts about them with remember. If they're "
            "tired of a topic or done with a goal, mute it.\n"
            "- A session that starts with '[spark] ...' was opened by you: the message after it was "
            "sent to them unprompted. Carry on from it naturally."
        ),
        tools=[add_interest, list_interests, mute_interest],
    )
