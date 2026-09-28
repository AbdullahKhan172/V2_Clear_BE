"""
Data Check: does the review screen agree with the calculation?
==============================================================
Step 5 is headed "the emission factors your results will be based on", so its
only job is to be true. The legacy wizard hardcoded these tables in HTML and two
of its 41 concrete values had already drifted from the catalogue CSV:

    8/10 MPa @ 25% GGBS   HTML 0.103   CSV 0.076
    8/10 MPa @ 50% GGBS   HTML 0.092   CSV 0.055

This asserts every number shown comes from the CSV, and that the per-m3 figure
uses the same density the engine does.

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_data_check.py
"""

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]

_TMP = Path(tempfile.mkdtemp(prefix='clear_dc_'))
# Against Postgres when CLEAR_TEST_DATABASE_URL is set, throwaway SQLite
# otherwise. The suite must pass on BOTH: SQLite is what a developer runs,
# Postgres is what is deployed, and they differ in ways that matter
# (types, transaction behaviour, JSON handling).
os.environ['DATABASE_URL'] = (os.environ.get('CLEAR_TEST_DATABASE_URL')
                              or f'sqlite:///{(_TMP / "dc.db").as_posix()}')
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
from app.services.data_check import build_data_check  # noqa: E402
from app.services.extraction import extract_quantities  # noqa: E402

# Steel is quantified by MASS, not volume - a steel row with volume 0 and no
# mass carries no quantity and is dropped, which is correct behaviour.
BOQ = b"""e_id,Category,Material,Description,Volume(m3),Mass(kg),Count
C_020,Slab/Floor,Concrete,L1 flat slab,100,,1
C_020,Column,Concrete,Column 400x400,12,,8
R_004,Beam,Steel Section,Steel UB 305x165,,5000,4
"""


def main() -> int:
    init_db()
    from catalogue import CONCRETE_GRADE_MAP, EmissionCatalogue
    cat = EmissionCatalogue()

    src = _TMP / 'boq.csv'
    src.write_bytes(BOQ)
    result = extract_quantities(str(src), '.csv')
    run = _run_with_upload(filename='boq.csv', ext='.csv', source=src,
                       project={'name': 'DC', 'area': 2000.0, 'stage': 'Tender'})
    store.save_extraction(run.id, result)
    rows = store.material_rows(run.id)

    problems: list[str] = []

    # ── 1. Every concrete/GGBS pair matches the CSV ─────────────────────
    # Drive one row through every grade/GGBS combination the catalogue has.
    target = next(r for r in rows if not r['is_steel'])
    checked = 0
    for (grade, ggbs), eid in sorted(CONCRETE_GRADE_MAP.items()):
        materials = {'defaults': {'concrete_grade': f'{grade} MPa',
                                  'ggbs_pct': ggbs},
                     'rows': {target['key']: {'grade': f'{grade} MPa',
                                              'ggbs': ggbs}},
                     'epd_overrides': {}}
        dc = build_data_check(store.get(run.id), [target], materials, {})
        entry = dc['concrete'][0]
        expected = cat.get_emission_factor(eid)['a1_a3']
        if abs((entry['a1_a3_kg'] or 0) - expected) > 1e-9:
            problems.append(f'{grade} @ {ggbs}%: shown {entry["a1_a3_kg"]}, '
                            f'CSV {expected} ({eid})')
        # Per-m3 must use the engine's 2400 density, or review != run.
        if abs(entry['a1_a3_m3'] - round(expected * 2400, 1)) > 0.05:
            problems.append(f'{grade} @ {ggbs}%: m3 {entry["a1_a3_m3"]} != '
                            f'{round(expected * 2400, 1)}')
        checked += 1
    print(f'  + all {checked} grade/GGBS combinations match the catalogue CSV')

    # The two values the legacy HTML got wrong, named explicitly so a future
    # regression is unmistakable.
    for grade, ggbs, stale in (('8/10', 25, 0.103), ('8/10', 50, 0.092)):
        real = cat.get_emission_factor(CONCRETE_GRADE_MAP[(grade, ggbs)])['a1_a3']
        if abs(real - stale) < 1e-9:
            problems.append(f'{grade}@{ggbs}% now equals the stale HTML value')
    print(f'  + the two values the legacy HTML had wrong are served from the CSV '
          f'(0.076 and 0.055, not 0.103 / 0.092)')

    # ── 2. GGBS saving is against the SAME grade at 0% ──────────────────
    # Set it per row, not as a global default: a row whose file supplied an e_id
    # already carries its own grade and GGBS, and that wins over the default -
    # matching the legacy seeding at web_ui.html:1461.
    materials = {'defaults': {'concrete_grade': '32/40 MPa', 'ggbs_pct': 0},
                 'rows': {r['key']: {'grade': '32/40 MPa', 'ggbs': 50}
                          for r in rows if not r['is_steel']},
                 'epd_overrides': {}}
    dc = build_data_check(store.get(run.id), rows, materials, {})
    c50 = next(c for c in dc['concrete'] if c['ggbs'] == 50)
    kg50 = cat.get_emission_factor(CONCRETE_GRADE_MAP[('32/40', 50)])['a1_a3']
    kg0 = cat.get_emission_factor(CONCRETE_GRADE_MAP[('32/40', 0)])['a1_a3']
    if c50['saving_pct'] != round((1 - kg50 / kg0) * 100):
        problems.append(f'saving {c50["saving_pct"]}% != '
                        f'{round((1 - kg50 / kg0) * 100)}%')
    print(f'  + 32/40 at 50% GGBS shows a {c50["saving_pct"]}% saving vs the '
          f'same grade at 0%')

    # ── 3. Steel shows what was ASSIGNED, not the default ───────────────
    steel_row = next(r for r in rows if r['is_steel'])
    materials = {'defaults': {}, 'rows': {steel_row['key']: {'steelEid': 'R_020'}},
                 'epd_overrides': {}}
    dc = build_data_check(store.get(run.id), rows, materials, {})
    if not any(s['e_id'] == 'R_020' for s in dc['steel']):
        problems.append(f'assigned R_020 not shown: '
                        f'{[s["e_id"] for s in dc["steel"]]}')
    r020 = next(s for s in dc['steel'] if s['e_id'] == 'R_020')
    if abs(r020['a1_a3_kg'] - cat.get_emission_factor('R_020')['a1_a3']) > 1e-9:
        problems.append('assigned steel factor value wrong')
    print(f'  + an assigned steel factor is reported ({r020["label"][:34]}, '
          f'{r020["a1_a3_kg"]} kgCO2e/kg), not the default')

    # ── 4. EPD override replaces the value and is flagged ───────────────
    materials = {'defaults': {'concrete_grade': '32/40 MPa', 'ggbs_pct': 0},
                 'rows': {},
                 'epd_overrides': {'Concrete|||32/40|||0':
                                   {'a1_a3': 250.0, 'source': 'Ecocem',
                                    'mat_type': 'Concrete'}}}
    dc = build_data_check(store.get(run.id), rows, materials, {})
    c = next(c for c in dc['concrete'] if c['ggbs'] == 0)
    if not c['is_epd'] or c['a1_a3_m3'] != 250.0:
        problems.append(f'EPD override not applied: {c}')
    if dc['coverage']['with_epd'] < 1:
        problems.append('EPD not counted in coverage')
    print(f'  + a supplier EPD replaces the value (250.0 kgCO2e/m3) and counts '
          f'toward coverage ({dc["coverage"]["pct"]}%)')

    # ── 5. A5a and stage ────────────────────────────────────────────────
    dc = build_data_check(store.get(run.id), rows,
                          {'defaults': {}, 'rows': {}, 'epd_overrides': {}},
                          {'a5a_factor': 35.0})
    if dc['a5a']['total_tonnes'] != round(35.0 * 2000 / 1000, 2):
        problems.append(f'A5a total {dc["a5a"]["total_tonnes"]} != 70.0')
    if not dc['a5a']['is_override']:
        problems.append('A5a override not flagged')
    if dc['stage']['code'] != 'S3' or not dc['stage']['epd_expected']:
        problems.append(f'stage wrong: {dc["stage"]}')
    print(f'  + A5a 35 x 2000 m2 = {dc["a5a"]["total_tonnes"]} t, flagged as an '
          f'override; stage resolves to {dc["stage"]["code"]} '
          f'(EPDs expected: {dc["stage"]["epd_expected"]})')

    if problems:
        print('\nPROBLEMS:')
        for p in problems[:20]:
            print('   ', p)
        print(f'\nDATA CHECK FAILED ({len(problems)})')
        return 1

    print('\nDATA CHECK PASSED - every factor shown is read from the catalogue '
          'CSV and matches what the engine will apply')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
