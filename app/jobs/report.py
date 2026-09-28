"""
Report job - builds the downloadable files off the request thread.
==================================================================
Same seam as parse and calculate: `_run_reports` is what becomes a Celery task.

Why a job rather than a synchronous endpoint - measured on a 400-line BOQ:

    HTML     68 ms
    Excel   768 ms
    Word  1,604 ms      (renders matplotlib charts and a 3D preview)

~2.4 s for all three, and it scales with model size. Borderline today, well past
comfortable on a real project.

The engine is re-run to rebuild the ProjectDataModel the generators need, rather
than that object being cached between requests. It costs ~0.5 s without
sensitivity, and it means a report always reflects the CURRENT wizard config -
so a report can never quietly describe a configuration the user has since
changed.
"""

from __future__ import annotations

import threading
import traceback

from celery import shared_task

from app import blobs, store
from app.services.calculation import CalculationError, execute_run
from app.services.reports import REPORT_KINDS, generate
from app.services.run_config import build_run_config


def celery_enabled() -> bool:
    """Read at call time, not import time, so tests can toggle it."""
    from app import celery_app
    return celery_app.ENABLED


@shared_task(name='clear.reports', bind=True, ignore_result=True)
def reports_task(self, run_id: str, kinds: list[str]) -> None:
    """Celery entry point. Both arguments are safe to put in the broker."""
    _run_reports(run_id, kinds)


def enqueue_reports(run_id: str, kinds: list[str]) -> None:
    """Generate the named reports in the background.

    Queued when a broker is configured, threaded otherwise. Report state is
    tracked separately from the run's own status (store.set_report_status), so
    a queued report never makes a finished calculation look unfinished.
    """
    if celery_enabled():
        reports_task.delay(run_id, list(kinds))
        return
    threading.Thread(target=_run_reports, args=(run_id, kinds),
                     daemon=True).start()


def _run_reports(run_id: str, kinds: list[str]) -> None:
    """The task body. Safe to move verbatim into a Celery worker."""
    run = store.get(run_id)
    if run is None:
        return

    store.set_report_status(run_id, 'generating', kinds=kinds)
    try:
        elements_df = store.get_frame(run_id)
        if elements_df is None or elements_df.empty:
            raise CalculationError('No quantities to report on.')

        elements_list, _ = store.get_elements(run_id)
        config = build_run_config(run, sensitivity=True)

        result = execute_run(
            elements_df, store.get_geometry(run_id), config,
            source_type=run.source_type or 'csv',
            has_real_eids=bool(run.has_real_eids),
            elements_list=elements_list)

        # The quantities export needs BOTH the full extraction and what survived
        # Step 2, so it can show what was left out as well as what counted.
        from app.services.calculation import _apply_filters
        filtered = _apply_filters(elements_df, config)

        project_name = str((run.project or {}).get('name') or 'report')
        manifest = dict(store.get_reports(run_id).get('files') or {})
        for kind in kinds:
            if kind not in REPORT_KINDS:
                continue
            entry = generate(run_id, kind, result, project_name=project_name,
                             full_elements_df=elements_df,
                             filtered_elements_df=filtered)
            manifest[kind] = entry

        store.save_reports(run_id, manifest)

    except CalculationError as exc:
        store.set_report_status(run_id, 'failed', error=str(exc))
    except Exception as exc:
        print(f'[report] run {run_id} FAILED: {exc}')
        traceback.print_exc()
        store.set_report_status(run_id, 'failed', error=str(exc))


def delete_report(run_id: str, kind: str) -> None:
    """Remove one generated report and drop it from the manifest."""
    reports = store.get_reports(run_id)
    files = dict(reports.get('files') or {})
    entry = files.pop(kind, None)
    if entry:
        try:
            (blobs.BLOB_ROOT / entry['key']).unlink(missing_ok=True)
        except OSError:
            pass
        store.save_reports(run_id, files)
