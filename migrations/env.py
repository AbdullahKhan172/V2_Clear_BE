"""
Alembic environment.
====================
Deliberately takes BOTH its target schema and its connection URL from the
application, not from alembic.ini:

    target schema   app.models via Base.metadata
    connection      app.db.DATABASE_URL

So `alembic.ini` carries no credentials, and a migration can never be generated
against a different database than the one the app talks to. On a host that
injects its own `DATABASE_URL` (Railway, Render, Heroku) this file needs no
configuration at all — `app.db` already normalises the provider's URL into the
driver SQLAlchemy 2 expects.

Run from webapp/backend:

    alembic upgrade head              apply everything
    alembic revision --autogenerate -m "what changed"
    alembic downgrade -1              undo the last one
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# migrations/env.py -> parents[1] is webapp/backend, which holds the app package.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db import DATABASE_URL, Base  # noqa: E402
from app import models  # noqa: E402,F401  - registers the mappers on Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The app's URL wins over anything in alembic.ini, always.
config.set_main_option('sqlalchemy.url', DATABASE_URL.replace('%', '%%'))

target_metadata = Base.metadata

# SQLite cannot ALTER most things in place; batch mode rewrites the table
# instead. Harmless on Postgres, and it means the same migration file runs
# against a developer's SQLite and the deployed Postgres.
_RENDER_AS_BATCH = DATABASE_URL.startswith('sqlite')


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it — for review, or for a DBA."""
    context.configure(
        url=config.get_main_option('sqlalchemy.url'),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
        compare_type=True,
        render_as_batch=_RENDER_AS_BATCH,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix='sqlalchemy.',
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without this, a column whose TYPE changed is silently ignored by
            # autogenerate - the migration looks clean and the schema drifts.
            compare_type=True,
            render_as_batch=_RENDER_AS_BATCH,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
