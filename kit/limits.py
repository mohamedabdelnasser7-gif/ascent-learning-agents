"""Limits on model calls, counted in SQLite so they hold across restarts.

Per customer: LIMIT_CUSTOMER_CALLS model calls (default 20) in LIMIT_CUSTOMER_SECONDS (default 600).
Overall: LIMIT_DAILY_CALLS model calls (default 1000) in the last 24 hours.
Every model call counts: drafts, the voice note and the handoff summary.

The webhook checks before it queues a message, and every model call checks again, so a
burst queued before the first answer still stops at the cap. Over a limit, the customer
gets one fixed line, once per window.
"""
import time

from kit import env
from kit.db import Db

DAY = 24 * 60 * 60
DEFAULT_CUSTOMER_CALLS = 20
DEFAULT_CUSTOMER_SECONDS = 600
DEFAULT_DAILY_CALLS = 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY,
  customer TEXT NOT NULL,
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS model_calls_customer ON model_calls (customer, at);
CREATE INDEX IF NOT EXISTS model_calls_at ON model_calls (at);
CREATE TABLE IF NOT EXISTS limit_notices (
  customer TEXT PRIMARY KEY,
  at REAL NOT NULL
);
"""


class OverLimit(Exception):
    """A model call was refused because a limit is reached. which: "customer" or "daily"."""

    def __init__(self, which):
        super().__init__(which)
        self.which = which


def key(channel, customer_id):
    return f"{channel}:{customer_id}"


def _int_env(name, default):
    try:
        value = int(env.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


class Limits:
    def __init__(self, db=None, per_customer=None, window=None, daily=None, clock=time.time):
        self.db = db if db is not None else Db()
        self.db.script(SCHEMA)
        self.per_customer = per_customer or _int_env("LIMIT_CUSTOMER_CALLS", DEFAULT_CUSTOMER_CALLS)
        self.window = window or _int_env("LIMIT_CUSTOMER_SECONDS", DEFAULT_CUSTOMER_SECONDS)
        self.daily = daily or _int_env("LIMIT_DAILY_CALLS", DEFAULT_DAILY_CALLS)
        self.clock = clock

    def _over(self, c, customer, now):
        n = c.execute("SELECT COUNT(*) FROM model_calls WHERE customer = ? AND at > ?",
                      (customer, now - self.window)).fetchone()[0]
        if n >= self.per_customer:
            return "customer"
        n = c.execute("SELECT COUNT(*) FROM model_calls WHERE at > ?", (now - DAY,)).fetchone()[0]
        if n >= self.daily:
            return "daily"
        return None

    def over(self, customer):
        """None, or the limit the customer is at: "customer" or "daily"."""
        now = self.clock()
        with self.db.tx() as c:
            return self._over(c, customer, now)

    def take(self, customer):
        """Count one model call for the customer, or raise OverLimit without counting it."""
        now = self.clock()
        with self.db.tx() as c:
            which = self._over(c, customer, now)
            if which:
                raise OverLimit(which)
            c.execute("INSERT INTO model_calls (customer, at) VALUES (?, ?)", (customer, now))
            c.execute("DELETE FROM model_calls WHERE at <= ?", (now - DAY,))

    def notice_due(self, customer, which):
        """True at most once per window: the customer window, or a day for the daily limit."""
        now = self.clock()
        span = self.window if which == "customer" else DAY
        with self.db.tx() as c:
            row = c.execute("SELECT at FROM limit_notices WHERE customer = ?", (customer,)).fetchone()
            if row is not None and row[0] > now - span:
                return False
            c.execute("INSERT OR REPLACE INTO limit_notices VALUES (?, ?)", (customer, now))
            return True

    def meter(self, model, customer):
        return Metered(model, self, customer)


class Metered:
    """A model whose every call is counted against the customer's limits first."""

    def __init__(self, model, limits, customer):
        self.model, self.limits, self.customer = model, limits, customer

    def chat(self, *args, **kwargs):
        self.limits.take(self.customer)
        return self.model.chat(*args, **kwargs)
