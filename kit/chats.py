"""Saved chats: every step of every chat, so a chat can resume and be replayed.

Steps are numbered from 1 inside each chat. Besides the messages the model sees
(user, assistant, tool), a chat holds "event" steps: stage changes and failed
send checks. That way branch() can rebuild the stage and the failure count at
any step.

Media bytes are never stored, only a sha256 and the mime type.
"""
import json
import time
import uuid

from kit.db import Db

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'asking',
  failed_checks INTEGER NOT NULL DEFAULT 0,
  parent_id TEXT,
  branched_at INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chats_customer ON chats (channel, customer_id, parent_id);
CREATE TABLE IF NOT EXISTS steps (
  chat_id TEXT NOT NULL REFERENCES chats (id),
  n INTEGER NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'event')),
  content TEXT,
  tool_calls TEXT,
  tool_call_id TEXT,
  name TEXT,
  media TEXT,
  at REAL NOT NULL,
  PRIMARY KEY (chat_id, n)
);
CREATE TABLE IF NOT EXISTS seen_messages (
  message_id TEXT PRIMARY KEY,
  at REAL NOT NULL
);
"""

FIRST_STAGE = "asking"


class Chats:
    def __init__(self, db=None):
        self.db = db if db is not None else Db()
        self.db.script(SCHEMA)

    # chats

    def open(self, channel, customer_id):
        """The customer's current chat on this channel, or a new one. Branches are never reopened."""
        with self.db.tx() as c:
            row = c.execute(
                "SELECT id FROM chats WHERE channel = ? AND customer_id = ? AND parent_id IS NULL "
                "ORDER BY created_at DESC LIMIT 1", (channel, customer_id)).fetchone()
            if row:
                return row["id"]
            return self._new(c, channel, customer_id)

    def _new(self, c, channel, customer_id, stage=FIRST_STAGE, failed=0, parent=None, at_step=None):
        chat_id = uuid.uuid4().hex
        now = time.time()
        c.execute("INSERT INTO chats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (chat_id, channel, customer_id, stage, failed, parent, at_step, now, now))
        return chat_id

    def get(self, chat_id):
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row is None:
            raise KeyError(chat_id)
        return dict(row)

    def stage(self, chat_id):
        return self.get(chat_id)["stage"]

    def set_stage(self, chat_id, stage):
        """Move the chat and record the move as an event step. No-op if already there."""
        if self.stage(chat_id) == stage:
            return
        with self.db.tx() as c:
            c.execute("UPDATE chats SET stage = ?, updated_at = ? WHERE id = ?", (stage, time.time(), chat_id))
            self._add(c, chat_id, "event", content=json.dumps({"stage": stage}))

    def failed_checks(self, chat_id):
        return self.get(chat_id)["failed_checks"]

    def add_failed_check(self, chat_id, feedback):
        """Count one failed send check in this chat. Returns the new count."""
        with self.db.tx() as c:
            c.execute("UPDATE chats SET failed_checks = failed_checks + 1, updated_at = ? WHERE id = ?",
                      (time.time(), chat_id))
            self._add(c, chat_id, "event", content=json.dumps({"check_failed": feedback}, ensure_ascii=False))
            return c.execute("SELECT failed_checks FROM chats WHERE id = ?", (chat_id,)).fetchone()[0]

    # steps

    def add_step(self, chat_id, role, content=None, tool_calls=None, tool_call_id=None, name=None, media=None):
        """Save one step. Returns its number."""
        with self.db.tx() as c:
            n = self._add(c, chat_id, role, content, tool_calls, tool_call_id, name, media)
            c.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (time.time(), chat_id))
            return n

    def _add(self, c, chat_id, role, content=None, tool_calls=None, tool_call_id=None, name=None, media=None):
        n = c.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM steps WHERE chat_id = ?", (chat_id,)).fetchone()[0]
        c.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            chat_id, n, role, content,
            json.dumps(tool_calls) if tool_calls else None,
            tool_call_id, name,
            json.dumps(media) if media else None,
            time.time()))
        return n

    def steps(self, chat_id, upto=None):
        sql = "SELECT * FROM steps WHERE chat_id = ?"
        args = [chat_id]
        if upto is not None:
            sql += " AND n <= ?"
            args.append(upto)
        with self.db.tx() as c:
            rows = c.execute(sql + " ORDER BY n", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tool_calls"] = json.loads(d["tool_calls"]) if d["tool_calls"] else None
            d["media"] = json.loads(d["media"]) if d["media"] else None
            out.append(d)
        return out

    def messages(self, chat_id, upto=None, last=40):
        """The chat as OpenAI-style messages, to resume it. Events are left out.

        Keeps the last `last` messages and never starts on a tool reply cut off
        from its call. Media that was sent earlier shows as a short note.
        """
        msgs = []
        for s in self.steps(chat_id, upto):
            if s["role"] == "event":
                continue
            m = {"role": s["role"], "content": s["content"]}
            if s["media"]:
                notes = [MEDIA_NOTE.get(x.get("kind"), "") for x in s["media"]]
                m["content"] = " ".join([n for n in notes if n] + [s["content"] or ""]).strip()
            if s["tool_calls"]:
                m["tool_calls"] = s["tool_calls"]
            if s["role"] == "tool":
                m["tool_call_id"] = s["tool_call_id"]
            msgs.append(m)
        msgs = msgs[-last:]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    # branch and replay

    def branch(self, chat_id, at_step):
        """Copy the chat up to and including step `at_step` into a new chat.

        The original is left untouched. The copy's stage and failed-check count
        are rebuilt from the event steps it carries.
        """
        src = self.get(chat_id)
        steps = self.steps(chat_id, upto=at_step)
        stage, failed = FIRST_STAGE, 0
        for s in steps:
            if s["role"] == "event":
                ev = json.loads(s["content"])
                if "stage" in ev:
                    stage = ev["stage"]
                if "check_failed" in ev:
                    failed += 1
        with self.db.tx() as c:
            new_id = self._new(c, src["channel"], src["customer_id"], stage, failed, chat_id, at_step)
            for s in steps:
                c.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                    new_id, s["n"], s["role"], s["content"],
                    json.dumps(s["tool_calls"]) if s["tool_calls"] else None,
                    s["tool_call_id"], s["name"],
                    json.dumps(s["media"]) if s["media"] else None,
                    s["at"]))
        return new_id

    # webhook retries

    def first_seen(self, message_id):
        """True the first time a channel message id is seen, False on every retry."""
        with self.db.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO seen_messages VALUES (?, ?)", (message_id, time.time()))
            return cur.rowcount == 1


MEDIA_NOTE = {
    "image": "(The customer sent a photo earlier. It is not kept.)",
    "audio": "(The customer sent a voice note.)",
}
