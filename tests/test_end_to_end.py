"""
End-to-end: the whole wizard, through the real HTTP API.
========================================================
The other harnesses test one layer each. This one drives the entire journey the
way the browser does - upload, choose, materials, transport, data check,
calculate, reports, dashboard, geometry - and asserts the figures stay
consistent from one end to the other.

What it is really guarding: every step is individually verified, but a result
can still be wrong if two correct steps disagree about what they are passing.
So the assertions here are mostly RECONCILIATIONS - the dashboard total equals
the results total equals the sum of the breakdowns equals the sum of the rows.

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_end_to_end.py
"""

import io
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]

_TMP = Path(tempfile.mkdtemp(prefix='clear_e2e_'))
# Against Postgres when CLEAR_TEST_DATABASE_URL is set, throwaway SQLite
# otherwise. The suite must pass on BOTH: SQLite is what a developer runs,
# Postgres is what is deployed, and they differ in ways that matter
# (types, transaction behaviour, JSON handling).
os.environ['DATABASE_URL'] = (os.environ.get('CLEAR_TEST_DATABASE_URL')
                              or f'sqlite:///{(_TMP / "e2e.db").as_posix()}')
# Local disk, always — unless run_suite.py is explicitly pointing the
# whole suite at object storage. Without this a developer's .env, which
# may hold REAL production credentials, would silently make every
# harness write test junk into a live bucket.
os.environ.setdefault('STORAGE_BACKEND', 'local')
os.environ['BLOB_DIR'] = str(_TMP / 'blobs')

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PROJECT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

AREA = 4800.0

# Mixed on purpose: concrete, modelled steel by mass, PT, precast hollowcore
# and multiple levels, so filters and every material bucket are exercised.
BOQ = b"""e_id,Category,Material,Description,Volume(m3),Mass(kg),Count,Level
C_020,Slab/Floor,Concrete,L1 flat slab 300thk,320,,1,Level 1
C_020,Slab/Floor,Concrete,Hollowcore plank 200,180,,1,Level 2
C_020,Column,Concrete,RC column 400x400,48,,24,Level 1
C_020,Wall,Concrete,Core wall 300,140,,4,Level 1
C_020,Foundation/Footing,Concrete,Pile caps,95,,14,Level 0
C_020,Stair,Concrete,Stair flights,14,,8,Level 1
R_004,Beam,Steel Section,Steel UB 457x191,,42000,18,Level 1
R_005,Slab/Floor,Rebar,Slab reinforcement,,68000,1,Level 1
PT_032,Slab/Floor,Post Tensioning,PT strand,,9000,1,Level 1
"""

client = TestClient(app)
problems: list[str] = []


def check(ok: bool, msg: str):
    if not ok:
        problems.append(msg)
    return ok


def wait(run_id: str, want: str, timeout=240) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f'/api/runs/{run_id}/status').json()
        if s['status'] in (want, 'failed'):
            return s
        time.sleep(0.2)
    return {'status': 'timeout'}


def main() -> int:
    init_db()

    # ── 1. Upload ───────────────────────────────────────────────────────
    r = client.post('/api/uploads',
                    files={'file': ('project.csv', io.BytesIO(BOQ))},
                    data={'project_name': 'End-to-end Project', 'area': str(AREA),
                          'stage': 'Detailed Design', 'location': 'Dublin',
                          'client': 'ACME'})
    check(r.status_code == 200, f'upload returned {r.status_code}')
    run_id = r.json()['run_id']
    s = wait(run_id, 'parsed')
    check(s['status'] == 'parsed', f'parse ended as {s["status"]}')
    n_elements = s['n_elements']
    print(f'  + upload → parsed, {n_elements} elements')

    # Project details must survive: GIA is required for the whole assessment.
    els = client.get(f'/api/runs/{run_id}/elements').json()
    check(els['project']['area'] == AREA, 'GIA did not persist')
    check(els['project']['name'] == 'End-to-end Project', 'project name lost')
    check(len(els['elements'][0]) == 20,
          f'element contract has {len(els["elements"][0])} fields, expected 20')
    print(f'  + project details persisted; 20-field element contract intact')

    # ── 2. Choose elements ──────────────────────────────────────────────
    g = client.get(f'/api/runs/{run_id}/groups').json()
    check(len(g['groups']) > 0, 'no element groups')
    check(len(g['levels']) >= 3, f'expected several levels, got {g["levels"]}')
    client.put(f'/api/runs/{run_id}/selection',
               json={'excluded_categories': ['Stair'], 'excluded_levels': [],
                     'excluded_groups': []})
    back = client.get(f'/api/runs/{run_id}/groups').json()['selection']
    check(back['excluded_categories'] == ['Stair'], 'selection did not persist')
    print(f'  + {len(g["groups"])} groups, {len(g["levels"])} levels; '
          f'excluded Stair and it persisted')

    # ── 3. Materials ────────────────────────────────────────────────────
    m = client.get(f'/api/runs/{run_id}/materials').json()
    rows = m['rows']
    check(len(rows) > 0, 'no material rows')
    check(all('Stair' != r['cat'] for r in rows),
          'an excluded category still appears in Materials')
    conc = [r for r in rows if not r['is_steel']]
    client.put(f'/api/runs/{run_id}/materials', json={
        'defaults': {'concrete_grade': '32/40 MPa', 'ggbs_pct': 0},
        'rows': {r['key']: {'grade': '32/40 MPa', 'ggbs': 0,
                            'rebarRate': r['default_rebar'], 'ptRate': 0}
                 for r in conc},
        'epd_overrides': {}})
    print(f'  + {len(rows)} material rows, Stair correctly absent')

    # ── 4. Transport & waste ────────────────────────────────────────────
    r = client.put(f'/api/runs/{run_id}/transport', json={
        'distances': {'in_situ': {'sea': 0, 'road': 45}},
        'waste_pct': {'Concrete': 6, 'Rebar': 5,
                      'Steel Section': 1, 'Post Tensioning': 1.5},
        'a5a_factor': 30.0})
    check(r.status_code == 200, f'transport save returned {r.status_code}')
    bad = client.put(f'/api/runs/{run_id}/transport',
                     json={'distances': {}, 'waste_pct': {}, 'a5a_factor': 9999})
    check(bad.status_code == 422, 'an out-of-range A5a was accepted')
    print('  + transport/waste saved; A5a of 9999 rejected with 422')

    # ── 5. Data check ───────────────────────────────────────────────────
    dc = client.get(f'/api/runs/{run_id}/data-check').json()
    check(len(dc['concrete']) > 0, 'data check shows no concrete factors')
    check(dc['a5a']['factor'] == 30.0,
          f'data check A5a is {dc["a5a"]["factor"]}, expected the 30.0 override')
    check(abs(dc['a5a']['total_tonnes'] - 30.0 * AREA / 1000) < 0.01,
          'A5a total does not match factor x area')
    print(f'  + data check: {len(dc["concrete"])} concrete + {len(dc["steel"])} '
          f'steel factors; A5a {dc["a5a"]["factor"]} → {dc["a5a"]["total_tonnes"]} t')

    # ── 6. Calculate ────────────────────────────────────────────────────
    r = client.post(f'/api/runs/{run_id}/calculate')
    check(r.status_code == 202, f'calculate returned {r.status_code}')
    s = wait(run_id, 'complete')
    check(s['status'] == 'complete', f'calculation ended as {s["status"]}')
    res = client.get(f'/api/runs/{run_id}/results').json()
    total = res['total_ton']
    check(total > 0, 'total is zero')
    print(f'  + calculated: {total} tCO₂e · {res["per_sqm"]} kg/m² · {res["rating"]}')

    # ── 7. Everything reconciles ────────────────────────────────────────
    dash = client.get(f'/api/runs/{run_id}/dashboard').json()
    check(abs(dash['total_ton'] - total) < 1e-6,
          'dashboard total differs from results total')

    stage_sum = sum(res['stage_emissions'].values())
    check(abs(stage_sum - total) < 0.02,
          f'stages sum to {stage_sum}, total is {total}')

    for by in ('material', 'category', 'level'):
        b = client.get(f'/api/runs/{run_id}/results/breakdown?by={by}').json()
        summed = sum(x['total_ton'] for x in b['breakdown'])
        check(abs(summed - total) < 0.02,
              f'{by} breakdown sums to {summed}, total is {total}')

    rows_res = client.get(f'/api/runs/{run_id}/results/rows').json()
    row_sum = sum(x['total_kg'] for x in rows_res['rows']) / 1000
    check(abs(row_sum - total) < 0.02,
          f'result rows sum to {row_sum}, total is {total}')

    # An excluded category must be absent from the priced rows entirely.
    check(all('Stair' not in x['category'] for x in rows_res['rows']),
          'an excluded category was still priced')

    # Per-m2 must be the total over the GIA, not something else.
    check(abs(res['per_sqm'] - total * 1000 / AREA) < 0.1,
          f'per-m² {res["per_sqm"]} != {total * 1000 / AREA:.1f}')
    print(f'  + reconciles: stages, 3 breakdowns and {rows_res["total"]} rows '
          f'all sum to {total} t; per-m² checks out')

    # ── 8. Decarbonisation ──────────────────────────────────────────────
    dec = dash['decarbonisation']
    ggbs = [a['key'] for a in dec['actions'] if 'ggbs' in a['key']]
    check(len(ggbs) <= 1, f'both GGBS variants counted: {ggbs}')
    check(all(a['open'] for a in dec['actions']),
          'a closed lever was ranked as an action')
    check(dec['combined_ton'] <= total,
          f'opportunity {dec["combined_ton"]} exceeds the total {total}')
    print(f'  + decarbonisation: {len(dec["actions"])} actions, '
          f'{dec["combined_ton"]} t ({dec["combined_pct"]}%), GGBS counted once')

    # ── 9. Reports ──────────────────────────────────────────────────────
    r = client.post(f'/api/runs/{run_id}/reports',
                    params=[('kinds', k) for k in ('html', 'excel', 'word', 'boq')])
    check(r.status_code == 202, f'reports returned {r.status_code}')
    deadline = time.time() + 300
    rep = {}
    while time.time() < deadline:
        rep = client.get(f'/api/runs/{run_id}/reports').json()
        if rep['status'] in ('ready', 'failed'):
            break
        time.sleep(0.3)
    check(rep['status'] == 'ready', f'report generation ended as {rep["status"]}')
    for kind in ('html', 'excel', 'word', 'boq'):
        check(kind in rep['files'], f'{kind} report missing')
        d = client.get(f'/api/runs/{run_id}/reports/{kind}/download')
        check(d.status_code == 200, f'{kind} download returned {d.status_code}')
        check(len(d.content) > 2000, f'{kind} download is suspiciously small')
        if kind != 'html':
            check(zipfile.is_zipfile(io.BytesIO(d.content)),
                  f'{kind} is not a valid OOXML file')
    # The HTML report must quote the same total the API does.
    html = client.get(f'/api/runs/{run_id}/reports/html/download').text
    check(f'{total:.2f}' in html or str(int(total)) in html,
          'the HTML report does not quote the API total')
    print(f'  + 4 reports generated and downloadable; HTML quotes the same total')

    # ── 10. Geometry ────────────────────────────────────────────────────
    geo = client.get(f'/api/runs/{run_id}/geometry').json()
    check('elements' in geo, 'geometry endpoint returned no elements key')
    check(geo['elements'] == [], 'a spreadsheet run produced 3D geometry')
    check('note' in geo, 'no explanation for the absent geometry')
    print('  + geometry: correctly empty for a spreadsheet, with an explanation')

    # ── 11. Recalculating is stable ─────────────────────────────────────
    client.post(f'/api/runs/{run_id}/calculate')
    wait(run_id, 'complete')
    again = client.get(f'/api/runs/{run_id}/results').json()
    check(abs(again['total_ton'] - total) < 1e-6,
          f'recalculating changed the total: {total} → {again["total_ton"]}')
    rows2 = client.get(f'/api/runs/{run_id}/results/rows').json()
    check(rows2['total'] == rows_res['total'],
          f'recalculating changed the row count: '
          f'{rows_res["total"]} → {rows2["total"]}')
    print(f'  + recalculating is idempotent: same total, same {rows2["total"]} rows')

    # ── 12. Error handling ──────────────────────────────────────────────
    bad = client.post('/api/uploads', files={'file': ('x.txt', io.BytesIO(b'x'))})
    check(bad.status_code == 400, f'a .txt upload returned {bad.status_code}')
    missing = client.get('/api/runs/nope/results')
    check(missing.status_code == 404, f'unknown run returned {missing.status_code}')
    no_area = client.post('/api/uploads',
                          files={'file': ('p.csv', io.BytesIO(BOQ))},
                          data={'project_name': 'No area'})
    nid = no_area.json()['run_id']
    wait(nid, 'parsed')
    calc = client.post(f'/api/runs/{nid}/calculate')
    check(calc.status_code == 422,
          f'calculating without GIA returned {calc.status_code}, expected 422')
    print('  + rejects: bad file type 400, unknown run 404, missing GIA 422')

    if problems:
        print('\nPROBLEMS:')
        for p in problems[:25]:
            print('   ', p)
        print(f'\nEND-TO-END FAILED ({len(problems)})')
        return 1

    print('\nEND-TO-END PASSED - the whole wizard, and every figure reconciles')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
