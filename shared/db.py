"""
shared/db.py — Postgres (Neon) connection for Mycela.

Provides a connection and parameterized query helpers.
Uses psycopg2 over TLS. The statement timeout is applied per-transaction via
SET LOCAL (not as a connection startup parameter) so it works through Neon's
connection pooler as well as a direct connection.

The app user (mycelium_app) has DML permissions only.
Schema changes use DATABASE_ADMIN_URL via a separate connection.

Usage:
    from shared.db import get_db

    db = get_db()
    rows = db.fetch_all("SELECT * FROM notebook_runs WHERE status = %s", ("failed",))
    db.execute(
        "INSERT INTO notebook_runs (notebook_id, status) VALUES (%s, %s) "
        "ON CONFLICT (notebook_id, github_run_id) DO UPDATE SET status = EXCLUDED.status",
        (notebook_id, "running")
    )
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REQUIRED_ENV = ["DATABASE_URL"]
STATEMENT_TIMEOUT_MS = 30_000     # 30 seconds — enforced on every query
HEALTH_CHECK_TIMEOUT_MS = 5_000   # 5 seconds — health check must answer fast

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"

# Tables the schema must contain — used by create_tables_if_missing()
EXPECTED_TABLES = [
    "notebook_runs",
    "notebook_stage_results",
    "pipeline_definitions",
    "pipeline_run_counts",
]


def _redact_url(url: str) -> str:
    """Redact credentials and host from a connection URL for safe logging."""
    return re.sub(r"//[^@]*@[^/]*", "//***:***@***", url)


class Database:
    """
    Thin wrapper around psycopg2 providing:
    - Connection validation at startup
    - Parameterized query helpers (no raw string interpolation)
    - Statement timeout enforcement
    - Automatic reconnect on connection loss
    """

    def __init__(self, url: str):
        self._url = url
        self._conn = None
        self._warn_if_wrong_provider(url)
        self._connect()

    @staticmethod
    def _warn_if_wrong_provider(url: str):
        if "supabase.co" in url:
            logger.warning(
                "[db] DATABASE_URL appears to be Supabase — Mycela uses Neon Postgres. "
                "Verify your .env or GitHub Secrets."
            )

    def _connect(self):
        try:
            import psycopg2
            import psycopg2.extras
            # NOTE: do NOT pass statement_timeout via `options` (a startup parameter) —
            # Neon's pooler rejects startup params. It is applied per-transaction with
            # SET LOCAL in execute()/fetch_all() instead (works pooled and direct).
            self._conn = psycopg2.connect(self._url)
            self._conn.autocommit = False
            logger.info("[db] Connected to Postgres (Neon)")
        except Exception as e:
            # Never include the URL in the error — it contains credentials
            raise RuntimeError(f"[db] Failed to connect to database: {e}") from e

    def _ensure_connected(self):
        try:
            self._conn.cursor().execute("SELECT 1")
        except Exception:
            logger.warning("[db] Connection lost — reconnecting")
            self._connect()

    @staticmethod
    def _log_query(sql: str, params: tuple):
        # Truncated DEBUG log — never the full query with potentially sensitive params
        logger.debug(f"[db] {sql[:80]}... params={'<%d values>' % len(params) if params else '()'}")

    def execute(self, sql: str, params: tuple = ()) -> int:
        """
        Execute a write statement (INSERT, UPDATE, DELETE, UPSERT).
        Returns rowcount.
        Never use string interpolation — always use params tuple.
        """
        self._ensure_connected()
        self._log_query(sql, params)
        try:
            with self._conn.cursor() as cur:
                # Per-transaction timeout (pooler-safe); constant, not user input.
                cur.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
                cur.execute(sql, params)
                self._conn.commit()
                return cur.rowcount
        except Exception as e:
            self._conn.rollback()
            raise RuntimeError(f"[db] Query failed: {e}\nSQL: {sql[:200]}") from e

    def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a SELECT and return all rows as dicts."""
        self._ensure_connected()
        self._log_query(sql, params)
        try:
            import psycopg2.extras
            with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
                cur.execute(sql, params)
                rows = [dict(row) for row in cur.fetchall()]
            self._conn.rollback()  # end the read transaction cleanly (SET LOCAL)
            return rows
        except Exception as e:
            self._conn.rollback()
            raise RuntimeError(f"[db] Query failed: {e}\nSQL: {sql[:200]}") from e

    def fetch_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        """Execute a SELECT and return the first row as a dict, or None."""
        rows = self.fetch_all(sql, params)
        return rows[0] if rows else None

    def fetch_count(self, sql: str, params: tuple = ()) -> int:
        """
        Execute a COUNT query and return the integer result.
        Returns the first column of the first row as int; 0 if no rows.
        """
        row = self.fetch_one(sql, params)
        if not row:
            return 0
        return int(next(iter(row.values())))

    def health_check(self) -> bool:
        """
        Returns True if the database answers within 5 seconds. Never raises.
        Uses a short statement timeout so a hung connection cannot block the caller.
        """
        try:
            self._ensure_connected()
            with self._conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {HEALTH_CHECK_TIMEOUT_MS}")
                cur.execute("SELECT 1")
                cur.fetchone()
            self._conn.rollback()  # end the SET LOCAL transaction cleanly
            return True
        except Exception as e:
            logger.error(f"[db] Health check failed: {e}")
            return False

    def create_tables_if_missing(self) -> bool:
        """
        Run the migration SQL if any expected table is missing.
        For fresh Oracle DB instances. Returns True if migrations ran,
        False if all tables already exist or migrations failed.
        Requires admin privileges (DDL) — run with DATABASE_ADMIN_URL.
        """
        try:
            existing = {
                row["table_name"] for row in self.fetch_all(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = %s", ("public",)
                )
            }
            missing = [t for t in EXPECTED_TABLES if t not in existing]
            if not missing:
                logger.info("[db] All expected tables present — no migration needed")
                return False

            logger.warning(f"[db] Missing tables {missing} — running migrations")
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                logger.info(f"[db] Applying {migration.name}")
                with self._conn.cursor() as cur:
                    cur.execute(migration.read_text())
                self._conn.commit()
            return True
        except Exception as e:
            try:
                self._conn.rollback()
            except Exception:
                pass
            logger.error(f"[db] create_tables_if_missing failed: {e}")
            return False

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass


# ── Module-level singleton ────────────────────────────────────────────────────

_db_instance: Optional[Database] = None


def get_db() -> Database:
    """
    Returns the module-level DB instance, creating it on first call.
    Validates required environment variables before connecting.
    """
    global _db_instance

    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(
            f"[db] Missing required environment variables: {missing}\n"
            f"Check .env.example for the full list."
        )

    if _db_instance is None:
        _db_instance = Database(os.environ["DATABASE_URL"])

    return _db_instance


def reset_db():
    """Reset the singleton — used in tests to get a fresh connection."""
    global _db_instance
    if _db_instance:
        _db_instance.close()
    _db_instance = None
