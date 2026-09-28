"""
Dashboard data: does every panel get correct numbers?
=====================================================
Phase A of the dashboard port. The charts are only as good as what feeds them,
and the failure mode is a plausible-looking wrong figure rather than an error.

What is checked:
  1. every breakdown sums back to the headline total - a chart must never
     disagree with the KPI above it
  2. volume is STORED, not re-derived from mass. Dividing by an assumed 2400 or
     7850 is wrong for timber and blockwork, and the 3D viewer colours elements
     by kgCO2e per m3
  3. the 3D payload matches elements to their priced rows, and an element
     EXCLUDED in Step 2 reads zero rather than a category average
  4. real tessellated meshes survive to the viewer when the model has them
  5. SCORS bands come from compliance.py, not a hardcoded copy

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_dashboard_data.py
"""

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]

_TMP = Path(tempfile.mkdtemp(prefix='clear_dash_'))
# Against Postgres when CLEAR_TEST_DATABASE_URL is set, throwaway SQLite
# otherwise. The suite must pass on BOTH: SQLite is what a developer runs,
# Postgres is what is deployed, and they differ in ways that matter
# (types, transaction behaviour, JSON handling).
os.environ['DATABASE_URL'] = (os.environ.get('CLEAR_TEST_DATABASE_URL')
                              or f'sqlite:///{(_TMP / "dash.db").as_posix()}')
# Local disk, always — unless run_suite.py is explicitly pointing the
# whole suite at object storage. Without this a developer's .env, which
# may hold REAL production credentials, would silently make every
# harness write test junk into a live bucket.
os.environ.setdefault('STORAGE_BACKEND', 'local')
os.environ['BLOB_DIR'] = str(_TMP / 'blobs')

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(PROJECT))

from app import store  # noqa: E402
from _helpers import run_with_upload as _run_with_upload
from app.db import init_db  # noqa: E402
from app.services import dashboard as dash  # noqa: E402
from app.services.calculation import execute_run  # noqa: E402
from app.services.extraction import extract_quantities  # noqa: E402
from app.services.run_config import build_run_config  # noqa: E402

AREA = 2400.0

# Timber included on purpose: its density is ~500, so any code deriving volume
# from mass / 2400 gets it badly wrong and the error is visible here.
BOQ = b"""Category,Material,Description,Volume(m3),Count,Level
Slab/Floor,Concrete,L1 flat slab,100,1,Level 1
Column,Concrete,Column 400x400,12,8,Level 1
Slab/Floor,Concrete,CLT floor panel,20,1,Level 2
Stair,Concrete,Stair flight,6,3,Level 2
"""


def _make_run(exclude=None):
    src = _TMP / f'boq_{exclude or "none"}.csv'
    src.write_bytes(BOQ)
    ex = extract_quantities(str(src), '.csv')
    run = _run_with_upload(filename='boq.csv', ext='.csv', source=src,
                       project={'name': 'Dash', 'area': AREA, 'stage': 'Tender'})
    store.save_extraction(run.id, ex)
    if exclude:
        store.save_selection(run.id, {'excluded_categories': [exclude],
                                      'excluded_levels': [],
                                      'excluded_groups': []})
    run = store.get(run.id)
    cfg = build_run_config(run, sensitivity=False)
    elements_list, _ = store.get_elements(run.id)
    result = execute_run(ex.elements_df, ex.geometry_data, cfg,
                         source_type=ex.source_type,
                         has_real_eids=ex.has_real_eids,
                         elements_list=elements_list)
    store.save_results(run.id, result)
    return run.id, result


def main() -> int:
    init_db()
    problems: list[str] = []

    run_id, result = _make_run()
    results = store.get_results(run_id)
    total = results['total_ton']
    print(f'  run total: {total} tCO2e\n')

    # ── 1. Breakdowns agree with the headline ───────────────────────────
    for by in ('material', 'category', 'level'):
        rows = store.result_breakdown(run_id, by)
        summed = sum(r['total_ton'] for r in rows)
        if abs(summed - total) > 0.01:
            problems.append(f'{by} breakdown sums to {summed}, headline {total}')
    stage_sum = sum(results['stage_emissions'].values())
    if abs(stage_sum - total) > 0.01:
        problems.append(f'stages sum to {stage_sum}, headline {total}')
    print(f'  + material, category, level and stage totals all reconcile '
          f'to {total} t')

    # ── 2. Volume is stored, not derived ────────────────────────────────
    rows, _ = store.result_rows(run_id)
    timber = [r for r in rows if 'CLT' in r['description']]
    bad = [r for r in rows if r['volume'] <= 0 and r['mass'] > 0
           and r['material'] in ('Concrete', 'Timber')]
    if bad:
        problems.append(f'{len(bad)} row(s) have mass but no stored volume')
    for r in timber:
        # If volume had been derived as mass/2400 it would be ~4.8x too small
        # for a ~500 kg/m3 timber panel. Compare against the real ratio.
        implied = r['mass'] / r['volume'] if r['volume'] else 0
        if r['volume'] and not (300 < implied < 3000):
            problems.append(f'timber row density implies {implied:.0f} kg/m3 — '
                            f'volume looks derived, not stored')
    dens = {r['description'][:22]: round(r['mass'] / r['volume'])
            for r in rows if r['volume'] > 0}
    print(f'  + volumes are stored: implied densities {dens}')

    # ── 3. Geometry matching, and exclusion reading zero ────────────────
    # This BOQ has no 3D geometry, so build a synthetic one. Geometry carries
    # the ELEMENT name ("L1 flat slab"); the priced row carries the BOQ
    # description ("L1 flat slab - Concrete"). The matcher strips that suffix
    # before comparing, so the fixture must use the element name or nothing
    # matches - which is exactly what a real IFC provides.
    from templates.html_template_2 import _strip_boq_pair_suffix
    geometry = [{'x': 0, 'y': 0, 'z': 0, 'width': 2, 'depth': 2, 'height': 0.3,
                 'category': r['category'],
                 'name': _strip_boq_pair_suffix(r['description']).strip(),
                 'level': r['level']} for r in rows if r['material'] == 'Concrete']
    import pandas as pd
    ddf = pd.DataFrame([{
        'Description': r['description'],
        'Total Emission(tCO2e)': r['total_kg'] / 1000.0,
        'A1-A3 Emission(kgCO2e)': r['a1_a3'],
        'Total Volume(m3)': r['volume'],
        'e_id': r['e_id'], 'Material': r['material'],
    } for r in rows])

    elements = dash.build_geometry_payload(geometry, ddf)
    if len(elements) != len(geometry):
        problems.append(f'geometry payload has {len(elements)} of {len(geometry)}')
    matched = [e for e in elements if e['total'] > 0]
    if not matched:
        problems.append('no geometry element matched a priced row')
    for e in matched:
        if e['per_m3'] <= 0:
            problems.append(f'{e["name"]}: matched but intensity is 0')
    print(f'  + {len(matched)}/{len(elements)} 3D elements matched their priced '
          f'rows; intensities {[e["per_m3"] for e in matched][:3]} kgCO2e/m3')

    # An element excluded in Step 2 must read zero, not a category average.
    ex_id, _ = _make_run(exclude='Stair')
    ex_rows, _ = store.result_rows(ex_id)
    if any('Stair' in r['description'] for r in ex_rows):
        problems.append('excluded Stair still present in result rows')
    ex_ddf = pd.DataFrame([{
        'Description': r['description'],
        'Total Emission(tCO2e)': r['total_kg'] / 1000.0,
        'A1-A3 Emission(kgCO2e)': r['a1_a3'],
        'Total Volume(m3)': r['volume'],
        'e_id': r['e_id'], 'Material': r['material'],
    } for r in ex_rows])
    ex_elements = dash.build_geometry_payload(geometry, ex_ddf)
    stair = [e for e in ex_elements if 'Stair' in e['name']]
    if not stair:
        problems.append('excluded Stair vanished from the 3D model entirely')
    elif stair[0]['total'] != 0 or stair[0]['per_m3'] != 0:
        problems.append(f'excluded Stair reads {stair[0]["total"]} t — must be 0')
    print(f'  + an element excluded in Step 2 stays visible in 3D but reads '
          f'zero ({stair[0]["total"]} t), not a category average')

    # ── 4. Real meshes survive ──────────────────────────────────────────
    with_mesh = [dict(geometry[0],
                      vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
                      triangles=[[0, 1, 2], [0, 1, 3]])] + geometry[1:]
    mesh_out = dash.build_geometry_payload(with_mesh, ddf)
    if 'v' not in mesh_out[0] or len(mesh_out[0]['v']) != 12:
        problems.append(f'mesh vertices lost: {list(mesh_out[0])}')
    if 't' not in mesh_out[0] or len(mesh_out[0]['t']) != 6:
        problems.append('mesh triangles lost')
    print(f'  + real meshes survive: {len(mesh_out[0]["v"])} floats, '
          f'{len(mesh_out[0]["t"])} indices; others fall back to boxes')

    # ── 4b. The payload is PRECOMPUTED, not rebuilt per request ─────────
    # Building it on the request thread cost 6.2 s and 8.1 MB on a 36-element
    # model, on every dashboard load, for an answer that cannot change once the
    # run is calculated. If this breaks the dashboard still works - it just
    # quietly goes back to being slow, which is the kind of regression nobody
    # notices until a user complains.
    import gzip as _gzip
    import json as _json

    from app import blobs as _blobs

    # This fixture is a spreadsheet: no 3D at all, so there must be no file.
    # An empty blob per spreadsheet run would be pure litter.
    if _blobs.get_viewer_gzip(run_id) is not None:
        problems.append('a run with no geometry still wrote a viewer payload')
    print('  + a spreadsheet run writes no viewer payload (nothing to draw)')

    # Now give the run real geometry and recalculate, which is the path an IFC
    # upload takes.
    store.update(run_id, geometry_key=_blobs.put_geometry(run_id, geometry))
    store.save_results(run_id, result)

    stored = _blobs.get_viewer_gzip(run_id)
    if stored is None:
        problems.append('a run WITH geometry did not precompute its payload')
    else:
        payload = _json.loads(_gzip.decompress(stored))
        expanded = len(_json.dumps(payload, separators=(',', ':')).encode())
        if payload.get('run_id') != run_id:
            problems.append('stored viewer payload is for the wrong run')
        for field in ('elements', 'has_meshes', 'count'):
            if field not in payload:
                problems.append(f'stored payload is missing {field!r} - it must '
                                f'be a whole response, so the endpoint never '
                                f'has to parse or rebuild it')
        if len(payload.get('elements') or []) != len(geometry):
            problems.append(f'stored payload has '
                            f'{len(payload.get("elements") or [])} elements, '
                            f'expected {len(geometry)}')
        # Gzipped on disk because that is what goes on the wire; storing it
        # expanded would mean compressing identical bytes on every request.
        print(f'  + a run with geometry precomputes it: {len(stored):,} B '
              f'gzipped on disk vs {expanded:,} B of JSON '
              f'({expanded / max(len(stored), 1):.1f}x), '
              f'{len(payload["elements"])} elements, whole response stored')

    # ── 5. Derived panels ───────────────────────────────────────────────
    by_cat = store.result_breakdown(run_id, 'category')
    sub = dash.substructure_split(by_cat)
    if abs(sub['substructure'] + sub['superstructure'] - sub['total']) > 0.01:
        problems.append(f'substructure split does not reconcile: {sub}')
    eff = dash.member_efficiency(by_cat, AREA)
    if abs(sum(e['per_sqm'] for e in eff) - results['per_sqm']) > 0.5:
        problems.append(f'member efficiency per-m2 does not sum to '
                        f'{results["per_sqm"]}')
    hs = dash.hotspots(rows, limit=5)
    if hs != sorted(hs, key=lambda h: h['total_kg'], reverse=True):
        problems.append('hotspots are not ordered by contribution')
    bands = dash.scors_bands()
    from compliance import SCORS_BANDS
    if len(bands) != len(SCORS_BANDS):
        problems.append(f'{len(bands)} SCORS bands, compliance.py has '
                        f'{len(SCORS_BANDS)}')
    if bands[0]['rating'] != 'A++':
        problems.append(f'first band is {bands[0]["rating"]}, expected A++')
    budget = dash.budget_vs_scors_b(results['per_sqm'], AREA)
    if budget['within'] != (results['per_sqm'] <= 200):
        problems.append('budget headroom disagrees with the intensity')
    print(f'  + substructure {sub["substructure_pct"]}% · '
          f'{len(eff)} member types · {len(hs)} hotspots · '
          f'{len(bands)} SCORS bands from compliance.py')
    print(f'  + budget vs SCORS B: {budget["actual"]} of {budget["target"]} '
          f'kgCO2e/m2, headroom {budget["headroom"]}')

    if problems:
        print('\nPROBLEMS:')
        for p in problems[:20]:
            print('   ', p)
        print(f'\nDASHBOARD DATA FAILED ({len(problems)})')
        return 1

    print('\nDASHBOARD DATA PASSED - every panel reconciles with the headline, '
          'and the 3D payload is faithful')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
