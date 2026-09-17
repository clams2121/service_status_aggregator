"""SQLite persistence. Synchronous, guarded by a lock; callers wrap in a thread if needed."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

STATUS_UP = "up"
STATUS_DOWN = "down"
STATUS_UNKNOWN = "unknown"
STATUSES = (STATUS_UP, STATUS_DOWN, STATUS_UNKNOWN)

SOURCE_REGISTER = "register"
SOURCE_SELF = "self"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS services (
  id                    INTEGER PRIMARY KEY,
  name                  TEXT    NOT NULL,
  host                  TEXT    NOT NULL,
  port                  INTEGER NOT NULL,
  health_url            TEXT    NOT NULL,
  log_path              TEXT    NOT NULL DEFAULT '',
  config_page_url       TEXT    NOT NULL DEFAULT '',
  first_registered_at   TEXT    NOT NULL,
  last_registered_at    TEXT    NOT NULL,
  registration_count    INTEGER NOT NULL DEFAULT 1,
  source                TEXT    NOT NULL DEFAULT 'register',
  last_status           TEXT    NOT NULL DEFAULT 'unknown',
  last_checked_at       TEXT,
  last_response_ms      REAL,
  last_failure_reason   TEXT,
  last_status_change_at TEXT,
  UNIQUE (name, host)
);

CREATE TABLE IF NOT EXISTS status_events (
  id          INTEGER PRIMARY KEY,
  service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
  at          TEXT    NOT NULL,
  from_status TEXT    NOT NULL,
  to_status   TEXT    NOT NULL,
  reason      TEXT
);
CREATE INDEX IF NOT EXISTS ix_status_events_service_at ON status_events(service_id, at);
"""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetimes are not accepted")
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


@dataclass(frozen=True)
class ServiceRow:
    id: int
    name: str
    host: str
    port: int
    health_url: str
    log_path: str
    config_page_url: str
    first_registered_at: datetime
    last_registered_at: datetime
    registration_count: int
    source: str
    last_status: str
    last_checked_at: datetime | None
    last_response_ms: float | None
    last_failure_reason: str | None
    last_status_change_at: datetime | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ServiceRow:
        return cls(
            id=row["id"],
            name=row["name"],
            host=row["host"],
            port=row["port"],
            health_url=row["health_url"],
            log_path=row["log_path"],
            config_page_url=row["config_page_url"],
            first_registered_at=from_iso(row["first_registered_at"]),  # type: ignore[arg-type]
            last_registered_at=from_iso(row["last_registered_at"]),  # type: ignore[arg-type]
            registration_count=row["registration_count"],
            source=row["source"],
            last_status=row["last_status"],
            last_checked_at=from_iso(row["last_checked_at"]),
            last_response_ms=row["last_response_ms"],
            last_failure_reason=row["last_failure_reason"],
            last_status_change_at=from_iso(row["last_status_change_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        def iso(dt: datetime | None) -> str | None:
            return to_iso(dt) if dt else None

        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "health_url": self.health_url,
            "log_path": self.log_path,
            "config_page_url": self.config_page_url,
            "first_registered_at": iso(self.first_registered_at),
            "last_registered_at": iso(self.last_registered_at),
            "registration_count": self.registration_count,
            "source": self.source,
            "last_status": self.last_status,
            "last_checked_at": iso(self.last_checked_at),
            "last_response_ms": self.last_response_ms,
            "last_failure_reason": self.last_failure_reason,
            "last_status_change_at": iso(self.last_status_change_at),
        }


@dataclass(frozen=True)
class StatusEvent:
    id: int
    service_id: int
    at: datetime
    from_status: str
    to_status: str
    reason: str | None


class Storage:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None, timeout=5.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    # -- lifecycle ---------------------------------------------------------

    def migrate(self) -> int:
        with self._lock:
            # executescript commits on its own; keep it outside the explicit transaction.
            self._conn.executescript(_SCHEMA)
        with self._lock, self._tx():
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
                return SCHEMA_VERSION
            current = int(row["version"])
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {current} is newer than this build ({SCHEMA_VERSION})"
                )
            # Future forward-only migration steps go here, each bumping the version.
            return current

    def ping(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextlib.contextmanager
    def _tx(self) -> Iterator[None]:
        """BEGIN IMMEDIATE / COMMIT, or ROLLBACK on error, on the shared connection."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # -- registrations -----------------------------------------------------

    def upsert_registration(
        self,
        record: dict[str, Any],
        *,
        source: str = SOURCE_REGISTER,
        now: datetime | None = None,
    ) -> tuple[ServiceRow, bool]:
        """Insert or update the row keyed on (name, host). Returns (row, created)."""
        ts = to_iso(now or utcnow())
        with self._lock, self._tx():
            existing = self._conn.execute(
                "SELECT id FROM services WHERE name = ? AND host = ?",
                (record["name"], record["host"]),
            ).fetchone()
            if existing is None:
                cur = self._conn.execute(
                    """
                    INSERT INTO services
                      (name, host, port, health_url, log_path, config_page_url,
                       first_registered_at, last_registered_at, registration_count, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        record["name"],
                        record["host"],
                        record["port"],
                        record["health_url"],
                        record["log_path"],
                        record["config_page_url"],
                        ts,
                        ts,
                        source,
                    ),
                )
                service_id = int(cur.lastrowid or 0)
                created = True
            else:
                service_id = int(existing["id"])
                self._conn.execute(
                    """
                    UPDATE services SET
                      port = ?, health_url = ?, log_path = ?, config_page_url = ?,
                      last_registered_at = ?, registration_count = registration_count + 1,
                      source = ?
                    WHERE id = ?
                    """,
                    (
                        record["port"],
                        record["health_url"],
                        record["log_path"],
                        record["config_page_url"],
                        ts,
                        source,
                        service_id,
                    ),
                )
                created = False
            row = self._conn.execute(
                "SELECT * FROM services WHERE id = ?", (service_id,)
            ).fetchone()
        return ServiceRow.from_row(row), created

    def list_services(self) -> list[ServiceRow]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM services ORDER BY name, host").fetchall()
        return [ServiceRow.from_row(r) for r in rows]

    def get_service(self, name: str, host: str) -> ServiceRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM services WHERE name = ? AND host = ?", (name, host)
            ).fetchone()
        return ServiceRow.from_row(row) if row else None

    def remove(self, name: str, host: str) -> bool:
        with self._lock, self._tx():
            cur = self._conn.execute(
                "DELETE FROM services WHERE name = ? AND host = ?", (name, host)
            )
            return cur.rowcount > 0

    # -- health checks -----------------------------------------------------

    def record_check(
        self,
        service_id: int,
        *,
        status: str,
        checked_at: datetime,
        response_ms: float | None,
        failure_reason: str | None,
    ) -> str | None:
        """Store a poll result. Returns the previous status if this is a transition, else None."""
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        ts = to_iso(checked_at)
        with self._lock, self._tx():
            row = self._conn.execute(
                "SELECT last_status FROM services WHERE id = ?", (service_id,)
            ).fetchone()
            if row is None:
                return None
            previous = str(row["last_status"])
            changed = previous != status
            self._conn.execute(
                """
                UPDATE services SET
                  last_status = ?, last_checked_at = ?, last_response_ms = ?,
                  last_failure_reason = ?,
                  last_status_change_at = CASE WHEN ? THEN ? ELSE last_status_change_at END
                WHERE id = ?
                """,
                (status, ts, response_ms, failure_reason, changed, ts, service_id),
            )
            if changed:
                self._conn.execute(
                    "INSERT INTO status_events (service_id, at, from_status, to_status, reason)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (service_id, ts, previous, status, failure_reason),
                )
        return previous if changed else None

    def list_events(self, service_id: int | None = None, limit: int = 100) -> list[StatusEvent]:
        with self._lock:
            if service_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM status_events ORDER BY at DESC, id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM status_events WHERE service_id = ?"
                    " ORDER BY at DESC, id DESC LIMIT ?",
                    (service_id, limit),
                ).fetchall()
        return [
            StatusEvent(
                r["id"],
                r["service_id"],
                from_iso(r["at"]),
                r["from_status"],
                r["to_status"],
                r["reason"],  # type: ignore[arg-type]
            )
            for r in rows
        ]

    def prune_events(self, older_than: datetime) -> int:
        with self._lock, self._tx():
            cur = self._conn.execute(
                "DELETE FROM status_events WHERE at < ?", (to_iso(older_than),)
            )
            return cur.rowcount
