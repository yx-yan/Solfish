from __future__ import annotations

from datetime import datetime, timedelta

from conftest import FakeClient, message, text

from solfish.session import SessionStore
from solfish.skills import open_db, spark

NOW = datetime(2026, 11, 9, 20, 0)


def store_with(settings, *interests: tuple[str, float, datetime | None]) -> SessionStore:
    store = SessionStore(open_db(settings))
    store.db.executescript(spark.SCHEMA)
    for topic, weight, sparked in interests:
        store.db.execute(
            "INSERT INTO interests (topic, evidence, weight, last_sparked_at) VALUES (?, ?, ?, ?)",
            (topic, f"likes {topic}", weight, sparked and sparked.isoformat(timespec="seconds")),
        )
    store.db.commit()
    return store


def test_pick_prefers_weight_but_rests_recent_topics(settings):
    db = store_with(settings, ("rust", 2.0, NOW - timedelta(hours=6)), ("go", 1.0, None),
                    ("zig", 3.0, NOW - timedelta(hours=200))).db
    assert spark.pick(db, NOW)["topic"] == "zig"  # rested long enough to count fully
    db.execute("UPDATE interests SET status = 'muted' WHERE topic = 'zig'")
    assert spark.pick(db, NOW)["topic"] == "go"  # rust at 2.0 x 6/72 loses to a fresh 1.0


def test_pick_nothing(settings):
    assert spark.pick(store_with(settings).db, NOW) is None
    assert spark.pick(store_with(settings, ("rust", 1.0, NOW)).db, NOW) is None  # just sparked


def test_start_seeds_a_session_the_user_can_answer(settings):
    store = store_with(settings, ("MLX quantization", 1.0, None))
    client = FakeClient(message(text("Did 4-bit hold up on your Mac?")))
    session_id, opener = spark.start(store, client, settings, "telegram", now=NOW)

    assert opener == "Did 4-bit hold up on your Mac?"
    assert "tools" not in client.requests[0]
    assert "MLX quantization" in client.requests[0]["messages"][0]["content"]
    history = store.load(session_id)
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"].startswith("[spark] MLX quantization")
    assert store.get(session_id)["title"].startswith("[spark] MLX quantization")
    row = store.db.execute("SELECT * FROM sparks").fetchone()
    assert row["session_id"] == session_id and row["opener"] == opener
    assert spark.pick(store.db, NOW) is None  # rests until it's fresh again
