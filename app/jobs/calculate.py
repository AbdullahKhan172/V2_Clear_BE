"""
Calculate job - runs the engine off the request thread.
=======================================================
In-process threading for now, same seam as jobs/parse.py: the body of
`_run_calculate` is exactly what becomes a Celery task.

Why it must be a job. The base calculation is quick, but sensitivity analysis
re-runs the WHOLE engine up to nine times (slab thickness, GGBS 50%, GGBS 70%,
and six member-reduction scenarios - engine.run_sensitivity_analysis). On a real
model that is the minute-plus wait, and it cannot sit on an HTTP request.

Headline results are saved BEFORE sensitivity runs, so the dashboard can render
totals, ratings and the material split immediately while the decarbonisation
figures arrive after - the same deferral trick as the 3D viewer meshes.
"""

from __future__ import annotations

import threading
import traceback

from celery import shared_task

from app import store
from app.services.calculation import CalculationError, execute_run
from app.services.run_config import build_run_config


def celery_enabled() -> bool:
    """Read at call time, not import time, so tests can toggle it."""
    from app import celery_app
    return celery_app.ENABLED


@shared_task(name='clear.calculate', bind=True, ignore_result=True)
def calculate_task(self, run_id: str, sensitivity: bool = True) -> None:
    """Celery entry point. Both arguments are safe to put in the broker."""
    _run_calculate(run_id, sensitivity)


def enqueue_calculate(run_id: str, *, sensitivity: bool = True) -> None:
    """Start the calculation in the background and return immediately.

    Queued when a broker is configured, threaded otherwise — the caller and the
    polling client cannot tell which, because `run.status` is the contract
    either way.
    """
    if celery_enabled():
        calculate_task.delay(run_id, sensitivity)
        return
    threading.Thread(target=_run_calculate, args=(run_id, sensitivity),
                     daemon=True).start()


def _run_calculate(run_id: str, sensitivity: bool = True) -> None:
    """The task body. Safe to move verbatim into a Celery worker."""
    run = store.get(run_id)
    if run is None:
        return

    store.update(run_id, status='calculating', error=None)
    try:
        elements_df = store.get_frame(run_id)
        if elements_df is None or elements_df.empty:
            raise CalculationError(
                'No quantities to calculate. Re-upload the file, or check the '
                'Choose Elements step.')

        elements_list, _ = store.get_elements(run_id)

        # Pass 1: headline numbers, no sensitivity. Saved immediately so the
        # dashboard has something to show while pass 2 runs.
        first = execute_run(
            elements_df, store.get_geometry(run_id),
            build_run_config(run, sensitivity=False),
            source_type=run.source_type or 'csv',
            has_real_eids=bool(run.has_real_eids),
            elements_list=elements_list)
        store.save_results(run_id, first)

        # Pass 2: the same run again with the decarbonisation scenarios. A
        # failure here is non-fatal - the headline result already stands.
        if sensitivity:
            try:
                full = execute_run(
                    elements_df, store.get_geometry(run_id),
                    build_run_config(run, sensitivity=True),
                    source_type=run.source_type or 'csv',
                    has_real_eids=bool(run.has_real_eids),
                    elements_list=elements_list)
                store.save_results(run_id, full)
            except Exception as exc:
                print(f'[calculate] sensitivity pass failed (non-fatal): {exc}')
                # The headline result stands; say the scenarios are unavailable
                # rather than leaving the dashboard waiting on them forever.
                store.mark_sensitivity(run_id, 'failed')

    except CalculationError as exc:
        # A rejected input: the message is written for the user.
        store.update(run_id, status='failed', error=str(exc))
    except Exception as exc:
        print(f'[calculate] run {run_id} FAILED: {exc}')
        traceback.print_exc()
        store.update(run_id, status='failed', error=str(exc))
