"""
Parse job - runs extraction off the request thread.
===================================================
In-process threading for now. This is the seam (with app/store.py) where
infrastructure lands later: the body of `_run_parse` is exactly what becomes a
Celery task, unchanged. Keeping the job boundary here from day one means the
frontend's polling contract (enqueue -> poll status -> fetch result) is already
correct and will not change when Celery arrives.

Why a job at all, even in-process: IFC parsing plus tessellation takes minutes on
a real model. The legacy app blocked the /extract request for up to 120 seconds
waiting on this (web_app.py:517-524), which cannot work over a network.

The gemini key is passed as an argument rather than read from the run row: it is
a user credential and is deliberately never persisted.
"""

from __future__ import annotations

import threading
import traceback

from celery import shared_task

from app import store
from app.services.extraction import build_viewer_meshes, extract_quantities
from app.uploads import local_upload


def celery_enabled() -> bool:
    """Read at call time, not import time, so tests can toggle it."""
    from app import celery_app
    return celery_app.ENABLED


@shared_task(name='clear.parse', bind=True, ignore_result=True)
def parse_task(self, run_id: str) -> None:
    """Celery entry point. Routed to the `heavy` queue.

    Takes ONLY a run id. The Gemini key is fetched from app/secrets rather than
    travelling as an argument, because arguments are serialised into the broker
    and repeated in failure tracebacks.
    """
    from app import secrets
    _run_parse(run_id, secrets.take(run_id))


def enqueue_parse(run_id: str, gemini_key: str | None = None) -> None:
    """Start parsing `run_id` in the background and return immediately.

    With a broker this queues a task; without one it runs in a thread, exactly
    as it always has. The caller cannot tell the difference, which is the point:
    a developer needs no Redis to open the app.
    """
    from app import secrets

    if celery_enabled():
        # Stashed BEFORE queueing: a worker can pick the task up immediately,
        # and must not find the secret missing because we were still writing it.
        secrets.stash(run_id, gemini_key)
        parse_task.delay(run_id)
        return

    threading.Thread(target=_run_parse, args=(run_id, gemini_key),
                     daemon=True).start()


def _run_parse(run_id: str, gemini_key: str | None = None) -> None:
    """The task body. Safe to move verbatim into a Celery worker."""
    run = store.get(run_id)
    if run is None:
        return

    store.update(run_id, status='parsing', error=None)
    try:
        # The upload may live in object storage, and the parsers need a real
        # path - so it is fetched to a temp file for the duration of the parse
        # and removed again afterwards. This is the line that lets the worker
        # run on a machine that has never seen the web server's disk.
        with local_upload(run) as source:
            result = extract_quantities(
                str(source), run.ext,
                gemini_key=gemini_key,
                defer_meshes=True,
            )

            # Built INSIDE the with-block, and before persisting.
            #
            # Inside, because tessellation reads the IFC again through the
            # processor - so the temp file must still exist. Moving this out
            # breaks only IFC uploads, and only the viewer: the kind of bug
            # that reaches production.
            #
            # Before persisting, because save_extraction writes the geometry in
            # one shot. Tessellation is deferred during extraction only so it
            # does not hold up quantities. Never fatal - the viewer falls back
            # to box geometry, and no quantity depends on it.
            build_viewer_meshes(result)

        # Writes blobs first, flips status to 'parsed' last - so a client that
        # sees 'parsed' can always read everything behind it.
        store.save_extraction(run_id, result)

    except Exception as exc:
        # Full traceback server-side; the client gets the message only.
        print(f'[parse] run {run_id} FAILED: {exc}')
        traceback.print_exc()
        store.update(run_id, status='failed', error=str(exc))
