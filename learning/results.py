"""The result log. Every agent turn is recorded with its outcome.

Outcomes today: replied, check_failed (one row per blocked reply, with the check's
feedback), handed_off. Later tools add ordered, delivered and refused, matched to
the chat once the real result is known. The night study reads this log.
"""
import json
import time

from kit.db import Db

OUTCOMES = ("replied", "check_failed", "handed_off", "ordered", "delivered", "refused")

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
  id INTEGER PRIMARY KEY,
  agent TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  step INTEGER,
  stage TEXT,
  outcome TEXT NOT NULL,
  lessons TEXT,
  detail TEXT,
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS results_chat ON results (chat_id);
"""


class Results:
    def __init__(self, db=None):
        self.db = db if db is not None else Db()
        self.db.script(SCHEMA)

    def record(self, chat_id, outcome, *, agent="frontdesk", step=None, stage=None, lessons=(), detail=None):
        """Add one row. lessons: ids of the lessons the reply was given."""
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO results (agent, chat_id, step, stage, outcome, lessons, detail, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (agent, chat_id, step, stage, outcome, json.dumps(list(lessons)),
                 json.dumps(detail, ensure_ascii=False) if detail is not None else None, time.time()))
            return cur.lastrowid

    def for_chat(self, chat_id):
        with self.db.tx() as c:
            rows = c.execute("SELECT * FROM results WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["lessons"] = json.loads(d["lessons"]) if d["lessons"] else []
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out
