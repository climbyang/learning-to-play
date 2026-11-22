"""SQLite persistence: app sessions, quota credits, events, key-value state."""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ltp.store")

SCHEMA_VERSION = 3
_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"  # fixed width -> lexicographic order == time order

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL,
    title TEXT NOT NULL,
    counting INTEGER NOT NULL,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,          -- last heartbeat while open
    closed INTEGER NOT NULL DEFAULT 0,
    end_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_end ON sessions(end_utc);
CREATE TABLE IF NOT EXISTS credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,              -- local business day YYYY-MM-DD
    minutes REAL NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('free', 'quiz', 'parent')),
    note TEXT NOT NULL DEFAULT '',
    ref TEXT,                       -- quiz reward batch id (idempotency)
    ts_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_credits_day ON credits(day);
CREATE UNIQUE INDEX IF NOT EXISTS ux_credits_ref ON credits(ref) WHERE ref IS NOT NULL;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_events_kind_ts ON events(kind, ts_utc);
"""


_KV_SCHEMA = "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS quiz_attempts (
    id TEXT PRIMARY KEY,
    device TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('regular', 'challenge')),
    template_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    snapshot TEXT NOT NULL,         -- json
    answer TEXT NOT NULL,           -- json
    option_order TEXT NOT NULL DEFAULT '',  -- json or empty
    day TEXT NOT NULL,
    issued_utc TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('issued', 'correct', 'exhausted', 'skipped', 'expired')),
    wrong_count INTEGER NOT NULL DEFAULT 0,
    hints_used INTEGER NOT NULL DEFAULT 0,
    last_wrong_utc TEXT,
    reward_lost INTEGER NOT NULL DEFAULT 0,
    finished_utc TEXT,
    nominal_minutes REAL NOT NULL DEFAULT 0,
    credited_minutes REAL NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_attempt_issued ON quiz_attempts(device, tier) WHERE state='issued';
CREATE UNIQUE INDEX IF NOT EXISTS ux_attempt_challenge_day ON quiz_attempts(device, day) WHERE tier='challenge';
CREATE INDEX IF NOT EXISTS ix_attempt_template ON quiz_attempts(template_id, state, finished_utc);
"""


def _statements(script: str) -> list[str]:
    # Plain split is safe: the scripts contain no semicolons inside literals or comments.
    # (executescript is avoided because it would implicitly COMMIT an open transaction.)
    return [x.strip() for x in script.split(";") if x.strip()]


class SchemaTooNewError(RuntimeError):
    """The database was written by a newer version of this program."""


def _ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(timezone.utc).strftime(_TS_FMT)


def _parse(s: str) -> datetime:
    return datetime.strptime(s, _TS_FMT).replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Credit:
    minutes: float
    source: str
    note: str
    ts: datetime


@dataclass(frozen=True)
class Event:
    ts: datetime
    kind: str
    detail: str


_SCHEMA_V3 = """
ALTER TABLE quiz_attempts ADD COLUMN progress TEXT NOT NULL DEFAULT '';
ALTER TABLE quiz_attempts ADD COLUMN rev INTEGER NOT NULL DEFAULT 0;
"""


@dataclass(frozen=True)
class Attempt:
    id: str
    device: str
    tier: str
    template_id: str
    instance_id: str
    content_hash: str
    snapshot: Any
    answer: Any
    option_order: Any  # parsed json list, or None when the question has no options
    day: date
    issued_utc: datetime
    state: str
    wrong_count: int = 0
    hints_used: int = 0
    last_wrong_utc: datetime | None = None
    reward_lost: int = 0
    finished_utc: datetime | None = None
    nominal_minutes: float = 0.0
    credited_minutes: float = 0.0
    outcome: str = ""
    progress: Any = None  # parsed json (passage-set progress), or None
    rev: int = 0  # bumped by update_attempt on every call


_ATTEMPT_COLS = tuple(f.name for f in fields(Attempt))
_ATTEMPT_SELECT = ", ".join(_ATTEMPT_COLS)
_JSON_COLS = ("snapshot", "answer", "option_order", "progress")
_EMPTY_IF_NONE_COLS = ("option_order", "progress")
_TS_COLS = ("issued_utc", "last_wrong_utc", "finished_utc")


def _attempt_to_db(col: str, v: Any) -> Any:
    if col in _EMPTY_IF_NONE_COLS:
        return "" if v is None else json.dumps(v, ensure_ascii=False)
    if col in _JSON_COLS:
        return json.dumps(v, ensure_ascii=False)
    if col == "day":
        return v.isoformat()
    if col in _TS_COLS:
        return None if v is None else _ts(v)
    return v


def _attempt_from_row(row: tuple) -> Attempt:
    kw: dict[str, Any] = {}
    for col, v in zip(_ATTEMPT_COLS, row):
        if col in _EMPTY_IF_NONE_COLS:
            kw[col] = json.loads(v) if v else None
        elif col in _JSON_COLS:
            kw[col] = json.loads(v)
        elif col == "day":
            kw[col] = date.fromisoformat(v)
        elif col in _TS_COLS:
            kw[col] = None if v is None else _parse(v)
        else:
            kw[col] = v
    return Attempt(**kw)


class Store:
    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._in_txn = False
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        # Only kv exists before the version check: a newer DB must be rejected before other DDL.
        self._db.execute(_KV_SCHEMA)
        raw = self.get_kv("schema_version")
        current = int(raw) if raw is not None else 0
        if current > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"database schema v{current} is newer than supported v{SCHEMA_VERSION}")
        if current == SCHEMA_VERSION:
            return
        steps = [(1, _SCHEMA), (2, _SCHEMA_V2), (3, _SCHEMA_V3)]
        with self.transaction():
            for version, script in steps:
                if current < version:
                    for stmt in _statements(script):
                        self._db.execute(stmt)
            self.set_kv("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Explicit write transaction; rolls back and re-raises on error."""
        if self._in_txn:
            raise RuntimeError("nested transaction")
        self._db.execute("BEGIN IMMEDIATE")
        self._in_txn = True
        try:
            yield
        except BaseException as exc:
            self._rollback_chained(exc)
            raise
        else:
            try:
                self._db.execute("COMMIT")
            except BaseException as exc:
                # A failed COMMIT leaves the connection inside the transaction.
                self._rollback_chained(exc)
                raise
        finally:
            self._in_txn = False

    def _rollback_chained(self, original: BaseException) -> None:
        """Roll back after `original`; if the rollback itself fails, chain it onto
        `original` (as its cause) so the caller still sees the original error."""
        if not self._db.in_transaction:
            return
        try:
            self._db.execute("ROLLBACK")
        except sqlite3.Error as rb_exc:
            log.error("rollback failed after %r: %s", original, rb_exc)
            original.__cause__ = rb_exc

    # -- sessions ---------------------------------------------------------
    def open_session(self, app_id: str, title: str, counting: bool, ts: datetime) -> int:
        cur = self._db.execute(
            "INSERT INTO sessions(app_id, title, counting, start_utc, end_utc) VALUES (?,?,?,?,?)",
            (app_id, title, int(counting), _ts(ts), _ts(ts)))
        return int(cur.lastrowid)

    def heartbeat(self, session_id: int, ts: datetime) -> None:
        self._db.execute("UPDATE sessions SET end_utc=? WHERE id=? AND closed=0 AND end_utc<?",
                         (_ts(ts), session_id, _ts(ts)))

    def close_session(self, session_id: int, ts: datetime, reason: str) -> None:
        self.heartbeat(session_id, ts)
        self._db.execute("UPDATE sessions SET closed=1, end_reason=? WHERE id=? AND closed=0",
                         (reason, session_id))

    def close_dangling(self) -> int:
        """Close sessions left open by a crash at their last heartbeat."""
        cur = self._db.execute(
            "UPDATE sessions SET closed=1, end_reason='restart' WHERE closed=0")
        return cur.rowcount

    def open_session_id(self) -> int | None:
        row = self._db.execute(
            "SELECT id FROM sessions WHERE closed=0 ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else int(row[0])

    def used_seconds(self, start: datetime, end: datetime) -> float:
        """Seconds of counting sessions overlapping [start, end)."""
        rows = self._db.execute(
            "SELECT start_utc, end_utc FROM sessions "
            "WHERE counting=1 AND end_utc>? AND start_utc<?", (_ts(start), _ts(end))).fetchall()
        total = 0.0
        for s, e in rows:
            a, b = max(_parse(s), start), min(_parse(e), end)
            if b > a:
                total += (b - a).total_seconds()
        return total

    # -- credits ----------------------------------------------------------
    def add_credit(self, day: date, minutes: float, source: str, note: str,
                   ts: datetime, ref: str | None = None) -> None:
        self._db.execute(
            "INSERT INTO credits(day, minutes, source, note, ref, ts_utc) VALUES (?,?,?,?,?,?)",
            (day.isoformat(), float(minutes), source, note, ref, _ts(ts)))

    def credits(self, day: date) -> list[Credit]:
        rows = self._db.execute(
            "SELECT minutes, source, note, ts_utc FROM credits WHERE day=? ORDER BY id",
            (day.isoformat(),)).fetchall()
        return [Credit(float(m), s, n, _parse(t)) for m, s, n, t in rows]

    # -- events -----------------------------------------------------------
    def log_event(self, kind: str, detail: str, ts: datetime) -> None:
        self._db.execute("INSERT INTO events(ts_utc, kind, detail) VALUES (?,?,?)",
                         (_ts(ts), kind, detail))

    def count_events(self, kind: str, since: datetime) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM events WHERE kind=? AND ts_utc>=?",
                               (kind, _ts(since))).fetchone()
        return int(row[0])

    def recent_events(self, limit: int = 10) -> list[Event]:
        rows = self._db.execute(
            "SELECT ts_utc, kind, detail FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Event(_parse(t), k, d) for t, k, d in rows]

    def last_event(self, kind: str) -> Event | None:
        row = self._db.execute(
            "SELECT ts_utc, kind, detail FROM events WHERE kind=? ORDER BY id DESC LIMIT 1",
            (kind,)).fetchone()
        return None if row is None else Event(_parse(row[0]), row[1], row[2])

    # -- key/value --------------------------------------------------------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return default if row is None else str(row[0])

    def set_kv(self, key: str, value: str) -> None:
        self._db.execute("INSERT INTO kv(key, value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def del_kv(self, key: str) -> None:
        self._db.execute("DELETE FROM kv WHERE key=?", (key,))

    # -- quiz attempts ----------------------------------------------------
    def insert_attempt(self, a: Attempt) -> None:
        marks = ",".join("?" * len(_ATTEMPT_COLS))
        self._db.execute(
            f"INSERT INTO quiz_attempts({_ATTEMPT_SELECT}) VALUES ({marks})",
            tuple(_attempt_to_db(c, getattr(a, c)) for c in _ATTEMPT_COLS))

    def get_attempt(self, attempt_id: str) -> Attempt | None:
        row = self._db.execute(
            f"SELECT {_ATTEMPT_SELECT} FROM quiz_attempts WHERE id=?", (attempt_id,)).fetchone()
        return None if row is None else _attempt_from_row(row)

    def open_attempt(self, device: str, tier: str) -> Attempt | None:
        row = self._db.execute(
            f"SELECT {_ATTEMPT_SELECT} FROM quiz_attempts "
            "WHERE device=? AND tier=? AND state='issued'", (device, tier)).fetchone()
        return None if row is None else _attempt_from_row(row)

    def update_attempt(self, attempt_id: str, **changes: Any) -> None:
        bad = [k for k in changes if k not in _ATTEMPT_COLS or k in ("id", "rev")]
        if bad:
            raise ValueError(f"unknown attempt fields: {bad}")
        if not changes:
            return
        sets = ", ".join([*(f"{k}=?" for k in changes), "rev = rev + 1"])
        self._db.execute(
            f"UPDATE quiz_attempts SET {sets} WHERE id=?",
            (*(_attempt_to_db(k, v) for k, v in changes.items()), attempt_id))

    def attempts_on(self, day: date) -> list[Attempt]:
        rows = self._db.execute(
            f"SELECT {_ATTEMPT_SELECT} FROM quiz_attempts WHERE day=? ORDER BY issued_utc, id",
            (day.isoformat(),)).fetchall()
        return [_attempt_from_row(r) for r in rows]

    def last_seen_by_template(self, device: str, tier: str,
                              since: datetime) -> dict[str, datetime]:
        """Latest finish time per template of the device's terminal attempts (answer revealed)."""
        rows = self._db.execute(
            "SELECT template_id, MAX(finished_utc) FROM quiz_attempts "
            "WHERE device=? AND tier=? AND state!='issued' AND finished_utc>=? "
            "GROUP BY template_id", (device, tier, _ts(since))).fetchall()
        return {tid: _parse(ts) for tid, ts in rows}

    def attempts_since(self, device: str, tier: str, since: datetime) -> list[Attempt]:
        """A device's attempts of a tier issued since `since`, oldest first."""
        rows = self._db.execute(
            f"SELECT {_ATTEMPT_SELECT} FROM quiz_attempts WHERE device=? AND tier=? "
            "AND issued_utc>=? ORDER BY issued_utc, id", (device, tier, _ts(since))).fetchall()
        return [_attempt_from_row(r) for r in rows]

    def count_attempts(self, device: str, day: date, *, state: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM quiz_attempts WHERE device=? AND day=?"
        args: list[Any] = [device, day.isoformat()]
        if state is not None:
            sql += " AND state=?"
            args.append(state)
        return int(self._db.execute(sql, args).fetchone()[0])

    def wrong_total(self, device: str, day: date) -> int:
        row = self._db.execute(
            "SELECT COALESCE(SUM(wrong_count), 0) FROM quiz_attempts WHERE device=? AND day=?",
            (device, day.isoformat())).fetchone()
        return int(row[0])

    def expire_before(self, day: date, ts: datetime) -> int:
        """Expire still-issued attempts from days before `day`."""
        cur = self._db.execute(
            "UPDATE quiz_attempts SET state='expired', finished_utc=? "
            "WHERE state='issued' AND day<?", (_ts(ts), day.isoformat()))
        return cur.rowcount

    def quiz_credit_minutes(self, day: date) -> float:
        row = self._db.execute(
            "SELECT COALESCE(SUM(minutes), 0) FROM credits WHERE day=? AND source='quiz'",
            (day.isoformat(),)).fetchone()
        return float(row[0])

    def last_attempt(self, device: str, tier: str) -> Attempt | None:
        """Most recently issued attempt of a device and tier, any state, any day."""
        row = self._db.execute(
            f"SELECT {_ATTEMPT_SELECT} FROM quiz_attempts WHERE device=? AND tier=? "
            "ORDER BY issued_utc DESC, rowid DESC LIMIT 1", (device, tier)).fetchone()
        return None if row is None else _attempt_from_row(row)
