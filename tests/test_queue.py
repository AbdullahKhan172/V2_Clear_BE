"""
The task queue: does work leave the request thread, and does the key stay out?
=============================================================================
Three claims, and the third is the one that would be expensive to get wrong:

  DISPATCH   with a broker configured the three jobs are QUEUED; without one
             they run in a thread, exactly as before. A developer must not need
             Redis to open the app, and the caller must not be able to tell
             which happened.

  ROUTING    parsing goes to its own queue. A 200 MB IFC holds a worker for
             minutes, and on a shared queue it would sit in front of every
             report download in the system.

  SECRECY    the user's Gemini key is NEVER a task argument. Celery serialises
             arguments into the broker, keeps them in the queued message, and
             repeats them in failure tracebacks - which is where a credential is
             most likely to be read by someone who should not have it.

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_queue.py
"""

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]

_TMP = Path(tempfile.mkdtemp(prefix='clear_queue_'))
os.environ['DATABASE_URL'] = (os.environ.get('CLEAR_TEST_DATABASE_URL')
                              or f'sqlite:///{(_TMP / "q.db").as_posix()}')
# Local disk, always — unless run_suite.py is explicitly pointing the
# whole suite at object storage. Without this a developer's .env, which
# may hold REAL production credentials, would silently make every
# harness write test junk into a live bucket.
os.environ.setdefault('STORAGE_BACKEND', 'local')
os.environ['BLOB_DIR'] = str(_TMP / 'blobs')

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(PROJECT))

REDIS_URL = os.environ.get('CLEAR_TEST_REDIS_URL', 'redis://127.0.0.1:6379/0')


def main() -> int:
    problems: list[str] = []

    def check(ok: bool, label: str, detail: str = '') -> None:
        print(f'  {"+" if ok else "x"} {label}' + (f' — {detail}' if detail else ''))
        if not ok:
            problems.append(f'{label}: {detail}')

    from app import celery_app
    from app.jobs import calculate, parse, report

    # ── 1. Configuration ────────────────────────────────────────────────
    names = {parse.parse_task.name, calculate.calculate_task.name,
             report.reports_task.name}
    check(names == {'clear.parse', 'clear.calculate', 'clear.reports'},
          'all three jobs are registered as tasks', ', '.join(sorted(names)))

    routes = celery_app.celery.conf.task_routes or {}
    parse_q = (routes.get('clear.parse') or {}).get('queue')
    calc_q = (routes.get('clear.calculate') or {}).get('queue')
    check(parse_q == celery_app.QUEUE_HEAVY and parse_q != calc_q,
          'parsing has its own queue, away from the light work',
          f'parse={parse_q} calculate={calc_q}')

    # The database is the source of truth for status; a result backend would be
    # a second, weaker copy of it that can disagree.
    check(celery_app.celery.conf.task_ignore_result is True,
          'results are ignored - run.status is the contract')
    check(celery_app.celery.conf.task_acks_late is True
          and celery_app.celery.conf.worker_prefetch_multiplier == 1,
          'a worker killed mid-task re-queues it rather than losing it',
          f'acks_late={celery_app.celery.conf.task_acks_late} '
          f'prefetch={celery_app.celery.conf.worker_prefetch_multiplier}')

    # ── 2. Dispatch switches on the broker, not on anything else ────────
    import threading

    started_threads: list = []
    real_thread = threading.Thread

    class _SpyThread(real_thread):
        def start(self):
            started_threads.append(self)          # do NOT actually run it

    queued: list = []

    def _spy_delay(*args, **kwargs):
        queued.append((args, kwargs))

    for module, task_attr in ((parse, 'parse_task'),
                              (calculate, 'calculate_task'),
                              (report, 'reports_task')):
        getattr(module, task_attr).delay = _spy_delay
        module.threading.Thread = _SpyThread

    was_enabled = celery_app.ENABLED
    try:
        # --- no broker: threads, exactly as before -----------------------
        celery_app.ENABLED = False
        parse.enqueue_parse('run-a', gemini_key='x')
        calculate.enqueue_calculate('run-a')
        report.enqueue_reports('run-a', ['excel'])
        check(len(started_threads) == 3 and not queued,
              'with no broker, all three run in threads',
              f'{len(started_threads)} threads, {len(queued)} queued')

        # --- broker: queued ----------------------------------------------
        started_threads.clear()
        celery_app.ENABLED = True
        parse.enqueue_parse('run-b', gemini_key='SECRET-VALUE')
        calculate.enqueue_calculate('run-b', sensitivity=False)
        report.enqueue_reports('run-b', ['word'])
        check(len(queued) == 3 and not started_threads,
              'with a broker, all three are queued instead',
              f'{len(queued)} queued, {len(started_threads)} threads')

        # ── 3. The credential is not in the arguments ───────────────────
        flat = repr(queued)
        check('SECRET-VALUE' not in flat,
              'the Gemini key is NOT a task argument',
              'found in the queued payload' if 'SECRET-VALUE' in flat else '')

        parse_args = queued[0][0]
        check(parse_args == ('run-b',),
              'the parse task carries only a run id', repr(parse_args))
    finally:
        celery_app.ENABLED = was_enabled
        for module in (parse, calculate, report):
            module.threading.Thread = real_thread

    # ── 4. The secret's own round trip, against a real Redis ────────────
    print()
    try:
        import redis as _redis
        _redis.Redis.from_url(REDIS_URL).ping()
        have_redis = True
    except Exception as exc:
        have_redis = False
        print(f'  - no Redis at {REDIS_URL} ({type(exc).__name__}); '
              f'skipping the round-trip check')

    if have_redis:
        os.environ['REDIS_URL'] = REDIS_URL
        import importlib

        import app.secrets as secrets_mod
        celery_app.REDIS_URL = REDIS_URL
        celery_app.ENABLED = True
        importlib.reload(secrets_mod)

        try:
            client = _redis.Redis.from_url(REDIS_URL)
            check(secrets_mod.stash('run-c', 'TOP-SECRET') is True,
                  'a key can be stashed for its task')
            raw = client.get('clear:secret:run-c')
            check(raw == b'TOP-SECRET', 'it is there for the worker to collect')
            ttl = client.ttl('clear:secret:run-c')
            check(0 < ttl <= secrets_mod.TTL_SECONDS,
                  'and it expires on its own if the task never runs',
                  f'ttl={ttl}s')

            check(secrets_mod.take('run-c') == 'TOP-SECRET',
                  'the task reads it')
            check(client.get('clear:secret:run-c') is None,
                  'and it is gone immediately after - one task, one read')
            check(secrets_mod.take('run-c') is None,
                  'a second read finds nothing')

            check(secrets_mod.stash('run-d', None) is False,
                  'an empty key stashes nothing', 'the commonest case')
            secrets_mod.stash('run-e', 'ABANDONED')
            secrets_mod.discard('run-e')
            check(client.get('clear:secret:run-e') is None,
                  'an abandoned upload drops its key')
        finally:
            celery_app.ENABLED = was_enabled
            os.environ.pop('REDIS_URL', None)

    if problems:
        print(f'\nQUEUE CHECK FAILED ({len(problems)})')
        for p in problems[:20]:
            print('   ', p)
        return 1

    print('\nQUEUE PASSED - work leaves the request thread either way, heavy '
          'parsing has its own lane, and the credential never enters the broker')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
