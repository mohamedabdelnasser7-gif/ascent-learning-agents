"""Lessons: one line each, with stage tags and a score.

A lesson is never rewritten. The database refuses any change to a lesson's text or
tags, and a retired lesson stays retired. To change what a lesson says, retire it
and add a new one. The store starts empty; the morning review and the night study
fill it.
"""
import re
import time

from kit.db import Db

MAX_PICK = 8
MAX_LEN = 300
ALL = "all"  # tag for a lesson that fits every stage

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  stages TEXT NOT NULL,
  score REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
  source TEXT,
  added_at REAL NOT NULL,
  retired_at REAL,
  retired_why TEXT
);
CREATE TRIGGER IF NOT EXISTS lessons_never_rewritten
BEFORE UPDATE OF text, stages ON lessons
BEGIN SELECT RAISE(ABORT, 'a lesson is never rewritten; retire it and add a new one'); END;
CREATE TRIGGER IF NOT EXISTS lessons_stay_retired
BEFORE UPDATE OF status ON lessons WHEN OLD.status = 'retired'
BEGIN SELECT RAISE(ABORT, 'a retired lesson stays retired'); END;
"""

_WORD = re.compile(r"[\w\u0600-\u06ff]+")


class Lessons:
    def __init__(self, db=None):
        self.db = db if db is not None else Db()
        self.db.script(SCHEMA)

    def add(self, text, stages, source="", score=0.0):
        """Add one lesson. Returns its id."""
        text = (text or "").strip()
        if not text or "\n" in text or "\r" in text:
            raise ValueError("a lesson is one line of text")
        if len(text) > MAX_LEN:
            raise ValueError(f"a lesson is {MAX_LEN} characters at most")
        tags = sorted({s.strip() for s in stages if s and s.strip()})
        if not tags or any("," in t for t in tags):
            raise ValueError("a lesson needs at least one stage tag")
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO lessons (text, stages, score, source, added_at) VALUES (?, ?, ?, ?, ?)",
                (text, "," + ",".join(tags) + ",", float(score), source, time.time()))
            return cur.lastrowid

    def retire(self, lesson_id, why=""):
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE lessons SET status = 'retired', retired_at = ?, retired_why = ? "
                "WHERE id = ? AND status = 'active'", (time.time(), why, lesson_id))
            return cur.rowcount == 1

    def set_score(self, lesson_id, score):
        """The score is the one number that moves, as results come in."""
        with self.db.tx() as c:
            c.execute("UPDATE lessons SET score = ? WHERE id = ?", (float(score), lesson_id))

    def active(self, stage=None):
        sql = "SELECT * FROM lessons WHERE status = 'active'"
        args = []
        if stage:
            sql += " AND (instr(stages, ?) > 0 OR instr(stages, ?) > 0)"
            args += [f",{stage},", f",{ALL},"]
        with self.db.tx() as c:
            rows = c.execute(sql + " ORDER BY id", args).fetchall()
        return [_row(r) for r in rows]

    def pick(self, stage, text, limit=MAX_PICK):
        """At most 8 active lessons that fit this stage, the ones sharing words with the text first."""
        words = {w.lower() for w in _WORD.findall(text or "") if len(w) > 2}
        found = []
        for les in self.active(stage):
            overlap = len(words & {w.lower() for w in _WORD.findall(les["text"])})
            found.append((overlap, les["score"], -les["id"], les))
        found.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
        return [t[3] for t in found[:min(limit, MAX_PICK)]]


def _row(r):
    d = dict(r)
    d["stages"] = [s for s in d["stages"].split(",") if s]
    return d
