"""
Reports: do the four downloadable formats build, and say the right thing?
=========================================================================
Reports are what leaves the building - emailed to a client, attached to a
tender. A report that generates but quotes the wrong figure is worse than one
that fails, so this checks content, not just that a file appeared.

What is checked:
  1. all four formats generate, are non-trivial, and are the right file type
  2. the HTML dashboard quotes the same total as the calculation
  3. the Excel workbook opens and carries the BOQ
  4. the quantities export shows EXCLUDED rows, not just used ones - the whole
     point of that sheet is showing what was left out
  5. the download filename is sanitised, so a hostile project name cannot
     smuggle a path separator or CRLF into Content-Disposition

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_reports.py
"""

import os
import sys
import tempfile
import zipfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]

_TMP = Path(tempfile.mkdtemp(prefix='clear_rep_'))
# Against Postgres when CLEAR_TEST_DATABASE_URL is set, throwaway SQLite
# otherwise. The suite must pass on BOTH: SQLite is what a developer runs,
# Postgres is what is deployed, and they differ in ways that matter
# (types, transaction behaviour, JSON handling).
os.environ['DATABASE_URL'] = (os.environ.get('CLEAR_TEST_DATABASE_URL')
                              or f'sqlite:///{(_TMP / "rep.db").as_posix()}')
# Local disk, always — unless run_suite.py is explicitly pointing the
# whole suite at object storage. Without this a developer's .env, which
# may hold REAL production credentials, would silently make every
# harness write test junk into a live bucket.
os.environ.setdefault('STORAGE_BACKEND', 'local')
os.environ['BLOB_DIR'] = str(_TMP / 'blobs')

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(PROJECT))

from app import blobs, store  # noqa: E402
from _helpers import run_with_upload as _run_with_upload
from app.db import init_db  # noqa: E402
from app.services.calculation import _apply_filters, execute_run  # noqa: E402
from app.services.extraction import extract_quantities  # noqa: E402
from app.services.reports import REPORT_KINDS, generate, safe_name  # noqa: E402
from app.services.run_config import build_run_config  # noqa: E402

AREA = 2400.0

BOQ = b"""e_id,Category,Material,Description,Volume(m3),Mass(kg),Count,Level
C_020,Slab/Floor,Concrete,L1 flat slab,100,,1,Level 1
C_020,Column,Concrete,Column 400x400,12,,8,Level 1
R_004,Beam,Steel Section,Steel UB 305x165,,5000,4,Level 1
C_020,Stair,Concrete,Stair flight,6,,3,Level 2
"""


def main() -> int:
    init_db()
    problems: list[str] = []

    src = _TMP / 'boq.csv'
    src.write_bytes(BOQ)
    ex = extract_quantities(str(src), '.csv')

    # A hostile project name, to prove the filename is sanitised.
    hostile = '../../etc/passwd\r\nX-Injected: yes'
    run = _run_with_upload(filename='boq.csv', ext='.csv', source=src,
                       project={'name': hostile, 'area': AREA,
                                'stage': 'Tender'})
    store.save_extraction(run.id, ex)
    # Exclude a category so the quantities export has something to report as
    # excluded - an export that only ever shows "Used" proves nothing.
    store.save_selection(run.id, {'excluded_categories': ['Stair'],
                                  'excluded_levels': [], 'excluded_groups': []})

    run = store.get(run.id)
    config = build_run_config(run, sensitivity=False)
    elements_list, _ = store.get_elements(run.id)
    result = execute_run(ex.elements_df, ex.geometry_data, config,
                         source_type=ex.source_type,
                         has_real_eids=ex.has_real_eids,
                         elements_list=elements_list)
    store.save_results(run.id, result)
    filtered = _apply_filters(ex.elements_df, config)
    total_ton = result.summary['total_ton']
    print(f'  run total: {total_ton} tCO2e across {len(result.data.detailed_data)} '
          f'rows (Stair excluded)\n')

    # ── 1. Every format generates ───────────────────────────────────────
    manifest = {}
    for kind in REPORT_KINDS:
        entry = generate(run.id, kind, result, project_name=hostile,
                         full_elements_df=ex.elements_df,
                         filtered_elements_df=filtered)
        manifest[kind] = entry
        path = blobs.BLOB_ROOT / entry['key']
        if not path.exists() or path.stat().st_size < 1000:
            problems.append(f'{kind}: missing or suspiciously small '
                            f'({path.stat().st_size if path.exists() else 0} B)')
        print(f'  + {kind:6} {entry["size"] / 1024:8.1f} KB  {entry["filename"]}')
    store.save_reports(run.id, manifest)

    # ── 2. File types are what they claim ───────────────────────────────
    html = (blobs.BLOB_ROOT / manifest['html']['key']).read_bytes()
    if b'<html' not in html.lower() and b'<!doctype' not in html.lower():
        problems.append('html report is not HTML')
    for kind in ('excel', 'word', 'boq'):
        p = blobs.BLOB_ROOT / manifest[kind]['key']
        if not zipfile.is_zipfile(p):
            problems.append(f'{kind} is not a valid OOXML (zip) file')
    print('  + html is HTML; excel/word/boq are valid OOXML archives')

    # ── 3. The HTML dashboard quotes the calculation's own total ────────
    text = html.decode('utf-8', errors='replace')
    if f'{total_ton:.2f}' not in text and f'{total_ton:.1f}' not in text:
        # Fall back to the integer part, since the dashboard may round.
        if str(int(total_ton)) not in text:
            problems.append(f'html report does not quote the total {total_ton}')
    if result.summary['rating'] not in text:
        problems.append(f'html report does not show the rating '
                        f'{result.summary["rating"]}')
    print(f'  + html quotes the total ({total_ton} t) and the rating '
          f'({result.summary["rating"]})')

    # ── 4. The quantities export shows what was EXCLUDED ────────────────
    import openpyxl
    wb = openpyxl.load_workbook(blobs.BLOB_ROOT / manifest['boq']['key'])
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    statuses = {r[-1] for r in rows if r and r[-1]}
    names = {str(r[1]) for r in rows if r and r[1]}
    if 'Excluded' not in statuses:
        problems.append(f'quantities export shows no excluded rows: {statuses}')
    if not any('Stair' in n for n in names):
        problems.append('the excluded Stair element is missing from the export')
    if ws.max_row - 1 != len(rows):
        problems.append('quantities export row count inconsistent')
    print(f'  + quantities export lists {len(rows)} elements, statuses '
          f'{sorted(statuses)} — the excluded Stair is reported, not dropped')

    # ── 5. The download filename is safe ────────────────────────────────
    fname = manifest['html']['filename']
    for bad in ('/', '\\', '..', '\r', '\n'):
        if bad in fname:
            problems.append(f'download filename contains {bad!r}: {fname!r}')
    if safe_name('') != 'report':
        problems.append('empty project name has no fallback')
    print(f'  + hostile project name sanitised to {fname!r}')

    # ── 6. Manifest round-trips ─────────────────────────────────────────
    back = store.get_reports(run.id)
    if set(back['files']) != set(REPORT_KINDS):
        problems.append(f'manifest lost entries: {sorted(back["files"])}')
    if back['status'] != 'ready':
        problems.append(f'report status is {back["status"]!r}, expected "ready"')

    # Deleting one removes both the file and the manifest entry.
    from app.jobs.report import delete_report
    gone = blobs.BLOB_ROOT / manifest['word']['key']
    delete_report(run.id, 'word')
    if gone.exists():
        problems.append('deleted report file still on disk')
    if 'word' in store.get_reports(run.id)['files']:
        problems.append('deleted report still in the manifest')
    print('  + manifest round-trips; deleting removes both file and entry')

    if problems:
        print('\nPROBLEMS:')
        for p in problems[:20]:
            print('   ', p)
        print(f'\nREPORTS FAILED ({len(problems)})')
        return 1

    print('\nREPORTS PASSED - all four formats generate, carry the run\'s own '
          'figures, and download safely')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
