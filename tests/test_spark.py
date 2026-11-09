from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from conftest import FakeClient, message, text

from solfish.session import SessionStore
from solfish.skills import open_db, spark

SATURDAY = datetime(2026, 11, 14, 20, 0)
MONDAY_AT_WORK = datetime(2026, 11, 9, 11, 0)


class First(random.Random):
    """Always takes the first option, so a test knows which kind of spark it gets."""

    def choices(self, population, weights=None, **_):
        return [population[0]]


def make_store(settings, *interests: tuple[str, str, float, datetime | None]) -> SessionStore:
    store = SessionStore(open_db(settings))
    spark.migrate(store.db)
    for topic, kind, weight, sparked in interests:
        store.db.execute(
            "INSERT INTO interests (topic, kind, evidence, weight, last_sparked_at) VALUES (?, ?, ?, ?, ?)",
            (topic, kind, f"likes {topic}", weight, sparked and sparked.isoformat(timespec="seconds")),
        )
    store.db.commit()
    return store


def say(store: SessionStore, sid: str, when: datetime, *texts: str) -> None:
    """A stored session where the user said `texts`, last touched at `when`."""
    store.create(sid, "telegram")
    store.sync(sid, [m for t in texts for m in ({"role": "user", "content": t},
                                                 {"role": "assistant", "content": [text("ok")]})])
    store.db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (when.isoformat(), sid))
    store.db.commit()


def test_pick_prefers_weight_but_rests_recent_topics(settings):
    db = make_store(settings, ("rust", "interest", 2.0, SATURDAY - timedelta(hours=6)),
                    ("go", "interest", 1.0, None),
                    ("zig", "interest", 3.0, SATURDAY - timedelta(hours=200)),
                    ("job", "goal", 5.0, None)).db
    assert spark.pick(db, SATURDAY)["topic"] == "zig"  # rested long enough to count fully
    db.execute("UPDATE interests SET status = 'muted' WHERE topic = 'zig'")
    assert spark.pick(db, SATURDAY)["topic"] == "go"  # rust at 2.0 x 6/72 loses to a fresh 1.0
    assert spark.pick(db, SATURDAY, "goal")["topic"] == "job"


def test_plan_day_is_random_within_bounds():
    rng = random.Random(1)
    for _ in range(50):
        plan = spark.plan_day(date(2026, 11, 9), rng, (5, 8), (9, 23))
        assert 5 <= len(plan) <= 8 and plan == sorted(plan)
        assert all(9 <= t.hour < 23 for t in plan)
    assert spark.plan_day(date(2026, 11, 9), rng, (0, 0), (9, 23)) == []


def test_may_send_backs_off_when_ignored_and_waits_while_chatting(settings):
    store = make_store(settings)
    db = store.db
    now = SATURDAY
    assert spark.may_send(db, now)

    def sparked(sid: str, minutes_ago: int, *reply: str) -> None:
        when = now - timedelta(minutes=minutes_ago)
        say(store, sid, when, "[spark] x", *reply)
        db.execute("INSERT INTO sparks (kind, session_id, opener, sent_at) VALUES ('checkin', ?, 'q?', ?)",
                   (sid, when.isoformat(timespec="seconds")))
        db.commit()

    sparked("a", 300, "nice question")  # answered
    sparked("b", 120)  # ignored
    sparked("c", 60)  # ignored: two in a row, so the gap is 45 min x 4 = 3 h
    assert spark.unanswered_streak(db) == 2
    assert not spark.may_send(db, now)
    assert spark.may_send(db, now + timedelta(minutes=120))

    say(store, "chat", now + timedelta(minutes=100), "busy talking")
    assert not spark.may_send(db, now + timedelta(minutes=120))  # mid-conversation
    assert spark.may_send(db, now + timedelta(minutes=130))


def test_choose_by_time_of_day(settings):
    store = make_store(settings)
    rng = random.Random(0)
    # Nothing known yet: a check-in in the evening, nothing at work.
    assert spark.choose(store.db, SATURDAY, rng).kind == "checkin"
    assert spark.choose(store.db, MONDAY_AT_WORK, rng) is None

    say(store, "a", MONDAY_AT_WORK - timedelta(days=1), "I'm reading about RAG evals")
    store.db.execute("INSERT INTO interests (topic) VALUES ('RAG')")
    work = {spark.choose(store.db, MONDAY_AT_WORK, rng).kind for _ in range(60)}
    assert work == {"interest", "followup"}  # no check-ins at work
    card = spark.choose(store.db, MONDAY_AT_WORK, rng)
    assert "at work" in card.note
    free = {spark.choose(store.db, SATURDAY, rng).kind for _ in range(80)}
    assert free == {"interest", "checkin"}  # the chat is too old to follow up on


def test_start_seeds_a_session_the_user_can_answer(settings):
    store = make_store(settings, ("MLX quantization", "interest", 1.0, None))
    say(store, "old", SATURDAY - timedelta(days=1), "4-bit was too slow yesterday")
    client = FakeClient(message(text("Did 4-bit hold up on your Mac?")))
    rng = First()
    session_id, opener = spark.start(store, client, settings, "telegram", now=SATURDAY, rng=rng)

    assert opener == "Did 4-bit hold up on your Mac?"
    request = client.requests[0]
    assert "tools" not in request
    assert "4-bit was too slow yesterday" in request["system"]
    history = store.load(session_id)
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"] == "[spark] MLX quantization"
    assert history[1]["content"] == [{"type": "text", "text": opener}]
    row = store.db.execute("SELECT * FROM sparks").fetchone()
    assert (row["kind"], row["session_id"], row["opener"]) == ("interest", session_id, opener)
    assert spark.pick(store.db, SATURDAY) is None  # rests until it's fresh again

    # The next opener sees what was already asked, and spark seeds aren't "things they said".
    client.responses.append(message(text("How's the day?")))
    spark.start(store, client, settings, "telegram", now=SATURDAY + timedelta(hours=2), rng=rng)
    system = client.requests[1]["system"]
    assert "Did 4-bit hold up" in system and "[spark]" not in system


def test_old_spark_tables_are_migrated(settings):
    db = open_db(settings)
    db.executescript("""
        CREATE TABLE interests (id INTEGER PRIMARY KEY, topic TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual',
            weight REAL NOT NULL DEFAULT 1.0, status TEXT NOT NULL DEFAULT 'active',
            last_sparked_at TEXT, created_at TEXT NOT NULL DEFAULT '');
        CREATE TABLE sparks (id INTEGER PRIMARY KEY, interest_id INTEGER NOT NULL,
            session_id TEXT NOT NULL, opener TEXT NOT NULL, sent_at TEXT NOT NULL);
        INSERT INTO interests (topic) VALUES ('MLX');
        INSERT INTO sparks (interest_id, session_id, opener, sent_at) VALUES (1, 's', 'q?', '2026-11-09');
    """)
    spark.migrate(db)
    spark.migrate(db)  # twice is fine
    assert db.execute("SELECT kind FROM interests").fetchone()[0] == "interest"
    row = db.execute("SELECT kind, interest_id, opener FROM sparks").fetchone()
    assert tuple(row) == ("interest", 1, "q?")
    db.execute("INSERT INTO sparks (kind, session_id, opener, sent_at) VALUES ('checkin', 't', 'hi', 'x')")
