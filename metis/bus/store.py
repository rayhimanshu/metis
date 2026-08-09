"""SQLite access for the bus.

Everything that mutates goes through `write()`, which opens with
`BEGIN IMMEDIATE`. That takes the write lock up front instead of optimistically
starting a read transaction and upgrading it later -- upgrading is where SQLite
raises `database is locked` under concurrency, and it is exactly the case the
lease broker has to survive.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Long enough to outlast a competing writer's transaction, short enough that a
# genuinely wedged database surfaces rather than hanging forever.
BUSY_TIMEOUT_MS = 5000


def now() -> str:
    # Milliseconds, not seconds. Several events routinely land inside the same
    # second, and second resolution makes `replay --at` unable to distinguish
    # them -- it would silently include events from after the moment asked for.
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="milliseconds")


def parse_ts(value: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(value)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    # ------------------------------------------------------------ lifecycle

    # Columns added after the first release. `CREATE TABLE IF NOT EXISTS` will
    # not alter a table that already exists, so an existing bus needs these
    # applied explicitly or it breaks on the first query that mentions them.
    MIGRATIONS: list[tuple[str, str]] = [
        ("events", "change_set TEXT"),
    ]

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        for table, definition in self.MIGRATIONS:
            column = definition.split()[0]
            existing = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def exists(self) -> bool:
        """Is there an initialised bus here?

        Not merely "is there a file". SQLite creates an empty one on the first
        connection, so a command that reads before anything has been written
        leaves a 0-byte file behind -- which then looks like a bus to every
        caller that asks, and answers no question put to it. The failure that
        produced this check was `metis watch` creating the file it then crashed
        on, with a raw traceback about a missing `runs` table.
        """
        try:
            if not self.path.is_file() or self.path.stat().st_size == 0:
                return False
        except OSError:
            return False

        try:
            with self.read() as conn:
                return conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
                ).fetchone() is not None
        except sqlite3.DatabaseError:
            return False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # --------------------------------------------------------- transactions

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Exclusive write transaction.

        `BEGIN IMMEDIATE` is what makes claim arbitration correct: two processes
        racing for the same lock key serialise here, so exactly one of them sees
        the free slot.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------------- runs

    def create_run(
        self, run_id: str, workspace: str, environment: str,
        requirement: str, max_iterations: int = 4,
    ) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO runs (id, workspace, environment, requirement,"
                " max_iterations, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'RUNNING', ?)",
                (run_id, workspace, environment, requirement, max_iterations, now()),
            )

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        with self.read() as conn:
            return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def latest_run(self) -> sqlite3.Row | None:
        with self.read() as conn:
            return conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()

    def resolve_run(self, run_id: str | None) -> sqlite3.Row:
        if not self.exists():
            raise BusError(
                f"no bus at {self.path} -- nothing has started here yet. "
                "Run `metis work` to pick something up, or `metis init-run`."
            )
        run = self.get_run(run_id) if run_id else self.latest_run()
        if not run:
            raise BusError(
                f"no such run: {run_id}" if run_id else "no runs yet -- start one with `metis init-run`"
            )
        return run

    def set_run_status(self, run_id: str, status: str) -> None:
        with self.write() as conn:
            conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))


class BusError(RuntimeError):
    pass
