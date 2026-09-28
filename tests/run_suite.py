"""
Run every harness — against SQLite, or against a real Postgres.
===============================================================
    python webapp/backend/tests/run_suite.py
    python webapp/backend/tests/run_suite.py --postgres postgresql://clear:clear@127.0.0.1:5432/clear
    python webapp/backend/tests/run_suite.py --s3        # with S3_* in the environment

SQLite and a local directory are what a developer runs; Postgres and object
storage are what is deployed. They differ in ways that reach this application —
column types, transaction behaviour, and the fact that an object store has no
directories, no atomic rename, and reports "missing" in three different shapes.
So "it passes locally" is not evidence that a deployment works. This is how you
get that evidence, and the two flags combine: `--postgres URL --s3` is the
deployed shape.

In --postgres mode each harness gets a FRESH schema, rebuilt by running the real
Alembic migrations. That is deliberate: it tests the migration path on every
run, rather than only the models, so a migration that drifts from the models
fails here rather than on a deploy.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
BACKEND = TESTS.parent
PROJECT = BACKEND.parents[1]
PY = sys.executable


def _reset_postgres(url: str) -> None:
    """Drop and rebuild the schema through Alembic, exactly as a deploy would."""
    from sqlalchemy import create_engine, text

    sys.path.insert(0, str(BACKEND))
    from app.db import _normalise

    engine = create_engine(_normalise(url), future=True)
    with engine.begin() as conn:
        # DROP SCHEMA rather than dropping tables: it also clears
        # alembic_version, so `upgrade head` starts from nothing every time.
        conn.execute(text('DROP SCHEMA public CASCADE'))
        conn.execute(text('CREATE SCHEMA public'))
    engine.dispose()

    env = {**os.environ, 'DATABASE_URL': url, 'PYTHONIOENCODING': 'utf-8'}
    r = subprocess.run(['alembic', 'upgrade', 'head'], cwd=BACKEND, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f'alembic upgrade failed:\n{r.stdout}\n{r.stderr}')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--postgres', metavar='URL',
                    help='run against this Postgres instead of SQLite')
    ap.add_argument('--s3', action='store_true',
                    help='run against S3 object storage instead of local '
                         'disk (reads S3_* from the environment)')
    args = ap.parse_args()

    harnesses = sorted(TESTS.glob('test_*.py'))
    label = (('Postgres' if args.postgres else 'SQLite')
             + ' + ' + ('S3' if args.s3 else 'local disk'))
    print(f'Running {len(harnesses)} harnesses against {label}\n')

    passed = skipped = failed = 0
    failures: list[tuple[str, str]] = []

    for path in harnesses:
        name = path.stem
        env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}

        # Local disk unless asked otherwise, and set explicitly rather than
        # left to the harness: a developer's .env may hold REAL production
        # credentials, and an ambient variable must never be what decides
        # whether tests write into a live bucket.
        env['STORAGE_BACKEND'] = 's3' if args.s3 else 'local'

        if args.postgres:
            # Only the DB-backed harnesses care; the rest ignore the variable.
            _reset_postgres(args.postgres)
            env['CLEAR_TEST_DATABASE_URL'] = args.postgres

        started = time.perf_counter()
        r = subprocess.run([PY, str(path)], cwd=PROJECT, env=env,
                           capture_output=True, text=True)
        took = time.perf_counter() - started
        out = (r.stdout or '') + (r.stderr or '')
        tail = [ln for ln in out.strip().splitlines() if ln.strip()]
        verdict = tail[-1] if tail else '(no output)'

        if 'SKIPPED' in verdict:
            skipped += 1
            print(f'  skip  {name:<26} {took:5.1f}s')
        elif r.returncode == 0 and ('PASSED' in verdict or 'VERIFIED' in verdict):
            passed += 1
            print(f'  ok    {name:<26} {took:5.1f}s')
        else:
            failed += 1
            failures.append((name, out))
            print(f'  FAIL  {name:<26} {took:5.1f}s')

    print(f'\n{label}: {passed} passed, {skipped} skipped, {failed} failed')

    for name, out in failures:
        print(f'\n{"=" * 70}\n{name}\n{"=" * 70}')
        print('\n'.join(out.strip().splitlines()[-25:]))

    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
