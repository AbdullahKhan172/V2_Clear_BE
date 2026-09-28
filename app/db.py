"""
Database engine and session.
============================
SQLite locally, Postgres in deployment - the only difference is DATABASE_URL.
Everything above this file is written against SQLAlchemy, so moving to Postgres
is an environment change, not a code change.

    export DATABASE_URL=postgresql+psycopg://user:pass@host/clear

A provider's own URL works too: `_normalise` below adds the driver name that
SQLAlchemy 2 needs and platforms do not supply.

Why Postgres rather than Mongo for the real deployment: runs, projects and
elements are genuinely relational (you will want "every run for this project,
ordered by kgCO2e/m2"), while the wizard config is nested and schema-fluid -
which JSON/JSONB columns cover without giving up joins or transactions.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[1]
VAR_DIR = BACKEND_ROOT / 'var'
VAR_DIR.mkdir(parents=True, exist_ok=True)

def _normalise(url: str) -> str:
    """Make a hosting provider's DATABASE_URL usable by SQLAlchemy 2.

    Railway, Heroku, Render and friends all inject `postgresql://...` (some
    still inject the older `postgres://`). SQLAlchemy reads a bare
    `postgresql://` as "use psycopg2", which is not installed and is not what
    this project depends on - so an untouched provider URL fails at import with
    a driver error rather than a connection error, which is a confusing way to
    start a deployment.

    Naming the driver explicitly is the whole fix. Any URL that already names
    one is left exactly as it is.
    """
    if url.startswith('postgres://'):           # legacy Heroku-style
        url = 'postgresql://' + url[len('postgres://'):]
    if url.startswith('postgresql://'):
        url = 'postgresql+psycopg://' + url[len('postgresql://'):]
    return url


DATABASE_URL = _normalise(os.environ.get(
    'DATABASE_URL',
    f'sqlite:///{(VAR_DIR / "clear.db").as_posix()}',
))

_is_sqlite = DATABASE_URL.startswith('sqlite')

engine = create_engine(
    DATABASE_URL,
    # The parse job runs on a background thread and writes the same run row the
    # request thread reads, which SQLite forbids by default.
    connect_args={'check_same_thread': False} if _is_sqlite else {},
    # Recycle before typical managed-Postgres idle timeouts; harmless on SQLite.
    pool_pre_ping=True,
    future=True,
)

if _is_sqlite:
    @event.listens_for(engine, 'connect')
    def _sqlite_pragmas(dbapi_conn, _record):
        """WAL lets the background job write while a request reads, instead of
        the reader hitting 'database is locked'. Postgres needs no equivalent."""
        cur = dbapi_conn.cursor()
        cur.execute('PRAGMA journal_mode=WAL')
        cur.execute('PRAGMA foreign_keys=ON')
        cur.execute('PRAGMA busy_timeout=5000')
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False,
                            future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Prepare the database for a local run. A NO-OP on Postgres.

    Two different jobs, deliberately split by backend:

      SQLite      create any missing tables. This is a developer machine or a
                  test run against a throwaway file, where waiting on a
                  migration step to look at the app is friction for no safety.

      Postgres    do nothing. The schema is owned by Alembic, and deployments
                  run `alembic upgrade head` before the app starts.

    That split matters. `create_all` against a Postgres that Alembic manages
    would build the tables WITHOUT stamping alembic_version, and the next
    migration would then try to create tables that already exist. Silence here
    is the correct behaviour, not an omission.

    The old _sync_columns helper is gone: it could only ADD nullable columns,
    which is why it said of itself that it was "NOT a migration tool".
    """
    from app import models  # noqa: F401  - registers the mappers

    if not _is_sqlite:
        return
    Base.metadata.create_all(engine)
