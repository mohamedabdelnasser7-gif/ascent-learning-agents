"""One SQLite file for chats, results and lessons (data/ascent.db, gitignored)."""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "data" / "ascent.db"


class Db:
    """A single connection shared by threads, guarded by one lock.

    Pass ":memory:" for tests.
    """

    def __init__(self, path=None):
        path = DEFAULT_PATH if path is None else path
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()

    @contextmanager
    def tx(self):
        """One transaction: commits on success, rolls back on any error."""
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def script(self, sql):
        with self._lock:
            self.conn.executescript(sql)

    def close(self):
        with self._lock:
            self.conn.close()
