"""PostgreSQL persistence. SQLite is accepted only by the offline import command."""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql, IntegrityError, OperationalError
from psycopg.rows import dict_row


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def database_url() -> str:
    value = os.environ.get("QB_DATABASE_URL", "")
    if not value.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("Set QB_DATABASE_URL to a PostgreSQL connection URL; SQLite is not a runtime backend")
    return value


def schema_name(value: str | None = None) -> str:
    name = value or os.environ.get("QB_DATABASE_SCHEMA", "question_bank")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
        raise ValueError("Invalid QB_DATABASE_SCHEMA")
    return name


class Connection(psycopg.Connection):
    @property
    def in_transaction(self) -> bool:
        return self.info.transaction_status != psycopg.pq.TransactionStatus.IDLE

    def begin_write(self) -> None:
        # Preserve existing optimistic concurrency/receipt guarantees across
        # workers, not only within one Python RLock. Lock released on rollback.
        self.execute("BEGIN")
        self.execute("SELECT pg_advisory_xact_lock(hashtextextended(current_schema(), 0))")


def connect(root: str | Path, *, database_schema: str | None = None) -> Connection:
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ("assets", "inputs", "exports"):
        (root / name).mkdir(exist_ok=True)
    conn = Connection.connect(database_url(), autocommit=True, row_factory=dict_row, connect_timeout=10,
                              options="-c lock_timeout=15000 -c statement_timeout=120000")
    try:
        name = schema_name(database_schema)
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (name,))
            # CREATE SCHEMA IF NOT EXISTS still requires database-level CREATE
            # privilege even when the schema exists; skip it for restricted
            # accounts that already own the schema.
            if conn.execute("SELECT to_regnamespace(%s) AS nsp", (name,)).fetchone()["nsp"] is None:
                conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(name)))
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(name)))
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
            for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                version = int(path.name.split('_')[0])
                script = path.read_text()
                digest = hashlib.sha256(script.encode()).hexdigest()
                applied = conn.execute("SELECT checksum FROM schema_migrations WHERE version=%s", (version,)).fetchone()
                if applied:
                    if applied['checksum'] != digest:
                        raise RuntimeError(f"Migration checksum mismatch: {path.name}")
                    continue
                conn.execute(script)
                conn.execute("INSERT INTO schema_migrations VALUES (%s,%s,%s)", (version,digest,utc_now()))
        return conn
    except BaseException:
        conn.close()
        raise


def is_initialized() -> bool:
    with psycopg.connect(database_url(), autocommit=True) as conn:
        return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (schema_name()+'.items',)).fetchone()[0]


@contextmanager
def temporary_schema():
    """Isolated PostgreSQL preflight, never write into the selected business bank."""
    name = 'qb_preflight_' + uuid.uuid4().hex
    try:
        yield name
    finally:
        with psycopg.connect(database_url(), autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))
