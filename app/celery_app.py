"""
The task queue.
===============
    celery -A app.celery_app:celery worker --loglevel=info
    celery -A app.celery_app:celery worker --loglevel=info --pool=solo   # Windows

Three jobs run off the request thread: parsing an upload (17 s on a small model,
minutes on a real one), calculating (nine engine passes with sensitivity on),
and generating reports. They were already written as detached functions taking
nothing but a `run_id`, so moving them here is a decorator and a routing rule.

NO BROKER, NO PROBLEM
    Without REDIS_URL the app falls back to the background threads it used
    before. That is not a degraded mode - it is what a developer should run, and
    what the fourteen harnesses run against. Requiring Redis to open the app
    locally would be friction with nothing to show for it.

NO RESULT BACKEND
    `run.status` in the database is the source of truth, and always was: the
    client polls it. Celery's result backend would be a second, weaker copy of
    that state, able to disagree with it. Results are ignored.

WHAT IS *NOT* PASSED THROUGH THE BROKER
    Task arguments are serialised into Redis, and they survive in queued
    messages and in failure tracebacks. So the user's Gemini key never becomes
    one - see app/secrets.py.
"""

from __future__ import annotations

import os

from celery import Celery

# Railway and most hosts inject REDIS_URL. Its absence is the signal to run
# in-process, so nothing has to be configured to work locally.
REDIS_URL = (os.environ.get('REDIS_URL') or '').strip()
ENABLED = bool(REDIS_URL)

# Heavy work gets its own queue. A 200 MB IFC parse holds a worker for minutes,
# and without this it would sit in front of every report download in the system.
QUEUE_HEAVY = 'heavy'
QUEUE_DEFAULT = 'default'

celery = Celery('clear', broker=REDIS_URL or None)

celery.conf.update(
    # See the module docstring: the database is the source of truth for state.
    task_ignore_result=True,
    result_backend=None,

    task_serializer='json',
    accept_content=['json'],

    # Acknowledge only once the task has finished, so a worker killed mid-parse
    # (a deploy, an OOM) puts the job back rather than losing it silently. Safe
    # because every task is idempotent: save_extraction and save_results both
    # delete their rows before inserting.
    task_acks_late=True,
    # With acks_late, a crashed worker's task is only re-queued if we also
    # decline to hold more than one at a time - otherwise prefetched jobs are
    # lost with it.
    worker_prefetch_multiplier=1,

    task_default_queue=QUEUE_DEFAULT,
    task_routes={
        'clear.parse': {'queue': QUEUE_HEAVY},
        'clear.calculate': {'queue': QUEUE_DEFAULT},
        'clear.reports': {'queue': QUEUE_DEFAULT},
    },

    # Timezone-aware UTC, matching how the app stores timestamps.
    timezone='UTC',
    enable_utc=True,
)


def autodiscover() -> None:
    """Import the task modules so the worker registers them.

    Called from the worker entry point rather than at import time: the web
    process imports this module too, and it has no reason to pull in the jobs.
    """
    from app.jobs import calculate, parse, report  # noqa: F401


# A worker started with `-A app.celery_app:celery` imports this module, so the
# tasks have to be registered by the time it finishes loading.
if os.environ.get('CLEAR_CELERY_WORKER') or ENABLED:
    try:
        autodiscover()
    except Exception as exc:                       # pragma: no cover
        # The web process can run without the job modules importing cleanly;
        # a worker cannot, and will fail loudly on its own when it tries.
        print(f'[celery] task modules not loaded: {exc}')
