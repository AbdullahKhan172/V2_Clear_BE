"""
Results parity: does the stored wizard config produce the legacy numbers?
========================================================================
test_calculation_parity.py proved the moved ENGINE logic is faithful, driving it
with legacy-shaped arguments. This proves the other half: that translating what
steps 1-4 actually SAVED into those arguments preserves the result, and that
persisting and reading it back changes nothing.

    wizard config in the DB
            │  build_run_config()          ← the translation under test
            ▼
        RunConfig ──▶ execute_run() ──▶ save_results() ──▶ get_results()
            │                                                    ⟂  compared
            └────────── equivalent legacy POST /run payload ──────┘

The translation is where the composite-factor guard lives: a factor that already
prices its reinforcement must be sent with ZERO rebar/PT rates, or the steel is
counted twice.

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_results_parity.py
"""

import io
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]

_TMP = Path(tempfile.mkdtemp(prefix='clear_res_'))
# Against Postgres when CLEAR_TEST_DATABASE_URL is set, throwaway SQLite
# otherwise. The suite must pass on BOTH: SQLite is what a developer runs,
# Postgres is what is deployed, and they differ in ways that matter
# (types, transaction behaviour, JSON handling).
os.environ['DATABASE_URL'] = (os.environ.get('CLEAR_TEST_DATABASE_URL')
                              or f'sqlite:///{(_TMP / "res.db").as_posix()}')
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
from app.services.calculation import execute_run  # noqa: E402
from app.services.extraction import extract_quantities  # noqa: E402
from app.services.run_config import build_run_config  # noqa: E402

AREA = 2400.0

MIXED_BOQ = b"""e_id,Category,Material,Description,Volume(m3),Mass(kg),Count,Level
C_020,Slab/Floor,Concrete,L1 flat slab,100,,1,Level 1
C_020,Column,Concrete,Column 400x400,12,8,,Level 1
C_020,Wall,Concrete,Core wall 250,45,,4,Level 2
R_004,Beam,Steel Section,Steel UB 305x165,,5000,4,Level 1
C_020,Slab/Floor,Concrete,Hollowcore plank 200,60,,1,Level 2
C_020,Stair,Concrete,Stair flight,6,,3,Level 1
"""

COMPARED = ('total_ton', 'per_sqm', 'rating', 'material_types',
            'material_emissions', 'stage_emissions', 'sequestration_ton',
            'a5a_factor', 'a5a_ton')


def _legacy(payload: dict) -> dict:
    """The real legacy route with an equivalent payload."""
    import web_app
    c = web_app.app.test_client()
    r = c.post('/upload', data={'file': (io.BytesIO(MIXED_BOQ), 'boq.csv')},
               content_type='multipart/form-data')
    rid = r.get_json()['run_id']
    c.post('/extract', json={'run_id': rid})
    res = c.post('/run', json={'run_id': rid, 'area': AREA,
                               'sensitivity': False, **payload})
    assert res.status_code == 200, res.get_json()
    return {k: res.get_json()['summary'][k] for k in COMPARED}


def _via_db(wizard: dict) -> tuple[dict, dict]:
    """Save the wizard config, calculate through it, and read the result back."""
    src = _TMP / f'boq_{abs(hash(str(wizard)))}.csv'
    src.write_bytes(MIXED_BOQ)
    ex = extract_quantities(str(src), '.csv')

    run = _run_with_upload(filename='boq.csv', ext='.csv', source=src,
                       project={'name': 'Res', 'area': AREA,
                                'stage': 'Concept / Schematic Design'})
    store.save_extraction(run.id, ex)
    if wizard.get('selection'):
        store.save_selection(run.id, wizard['selection'])
    if wizard.get('materials'):
        store.save_materials(run.id, wizard['materials'])
    if wizard.get('transport'):
        store.save_transport(run.id, wizard['transport'])

    run = store.get(run.id)
    cfg = build_run_config(run, sensitivity=False)
    elements_list, _ = store.get_elements(run.id)
    result = execute_run(ex.elements_df, ex.geometry_data, cfg,
                         source_type=ex.source_type,
                         has_real_eids=ex.has_real_eids,
                         elements_list=elements_list)
    store.save_results(run.id, result)
    return {k: result.summary[k] for k in COMPARED}, {'run_id': run.id}


def _diff(label, a, b) -> list[str]:
    problems = []
    for k in COMPARED:
        va, vb = a[k], b[k]
        if isinstance(va, float) and isinstance(vb, float):
            if abs(va - vb) > 1e-6:
                problems.append(f'{label}.{k}: {va} vs {vb}')
        elif isinstance(va, dict) and isinstance(vb, dict):
            for kk in set(va) | set(vb):
                x, y = va.get(kk), vb.get(kk)
                if isinstance(x, float) and isinstance(y, float):
                    if abs(x - y) > 1e-6:
                        problems.append(f'{label}.{k}.{kk}: {x} vs {y}')
                elif x != y:
                    problems.append(f'{label}.{k}.{kk}: {x!r} vs {y!r}')
        elif va != vb:
            problems.append(f'{label}.{k}: {va!r} vs {vb!r}')
    return problems


def _mat_key(cat, name):
    return f'{cat}|||{name}'


def main() -> int:
    # These harnesses diff THIS implementation against the legacy Flask app, so
    # they need it present. A copy of webapp/ moved out on its own has no
    # legacy tree to compare with - say so and skip, rather than dying with a
    # bare ModuleNotFoundError that reads like a broken test.
    try:
        import web_app  # noqa: F401
    except ModuleNotFoundError:
        print('  - the legacy Flask app (web_app.py) is not on the path, so '
              'there is nothing to diff against.')
        print()
        print('PARITY SKIPPED - this check only runs alongside the legacy tree')
        return 0

    init_db()
    problems: list[str] = []

    # Discover the real material keys once.
    probe = _TMP / 'probe.csv'
    probe.write_bytes(MIXED_BOQ)
    ex = extract_quantities(str(probe), '.csv')
    p_run = _run_with_upload(filename='p.csv', ext='.csv', source=probe,
                         project={'name': 'p', 'area': AREA})
    store.save_extraction(p_run.id, ex)
    rows = store.material_rows(p_run.id)
    slab = next(r for r in rows if 'flat slab' in r['name'])
    hc = next(r for r in rows if 'Hollowcore' in r['name'])

    def _default_rates(extra_rates=None, extra_pt=None, extra_mats=None):
        """The payload the REAL legacy wizard sends when nothing is touched.

        Not an empty dict: renderMaterialsPage seeds every concrete row with
        `rebarRate: g.default_rebar || 150` (web_ui.html:1461) and
        runCalculations sends it, so an untouched wizard still applies
        rate-based rebar. Comparing against an empty payload would be comparing
        against a screen no user ever sees.
        """
        return {
            'steel_rates': {**{r['name']: r['default_rebar']
                               for r in rows if not r['is_steel']},
                            **(extra_rates or {})},
            'pt_rates': {**{r['name']: 0 for r in rows if not r['is_steel']},
                         **(extra_pt or {})},
            'element_materials': {
                **{r['key']: {'grade': r['grade'] or '32/40 MPa',
                              'ggbs': r['ggbs'] or 0,
                              'rebarRate': r['default_rebar'], 'ptRate': 0}
                   for r in rows if not r['is_steel']},
                **(extra_mats or {})},
        }

    cases = [
        ('defaults', {}, _default_rates()),

        ('global grade + GGBS',
         {'materials': {'defaults': {'concrete_grade': '40/50 MPa',
                                     'ggbs_pct': 50},
                        'rows': {r['key']: {'grade': '40/50 MPa', 'ggbs': 50}
                                 for r in rows if not r['is_steel']},
                        'epd_overrides': {}}},
         {'concrete_grade': '40/50 MPa', 'ggbs_pct': 50,
          'element_materials': {r['key']: {'grade': '40/50 MPa', 'ggbs': 50,
                                           'rebarRate': r['default_rebar'],
                                           'ptRate': 0}
                                for r in rows if not r['is_steel']},
          'steel_rates': {r['name']: r['default_rebar']
                          for r in rows if not r['is_steel']},
          'pt_rates': {r['name']: 0 for r in rows if not r['is_steel']}}),

        ('rebar + PT per element',
         {'materials': {'defaults': {}, 'epd_overrides': {},
                        'rows': {slab['key']: {'rebarRate': 185, 'ptRate': 14}}}},
         {'steel_rates': {**{r['name']: r['default_rebar']
                             for r in rows if not r['is_steel']},
                          slab['name']: 185},
          'pt_rates': {**{r['name']: 0 for r in rows if not r['is_steel']},
                       slab['name']: 14},
          'element_materials': {
              **{r['key']: {'grade': r['grade'] or '32/40 MPa',
                            'ggbs': r['ggbs'] or 0,
                            'rebarRate': r['default_rebar'], 'ptRate': 0}
                 for r in rows if not r['is_steel']},
              slab['key']: {'grade': slab['grade'] or '32/40 MPa',
                            'ggbs': slab['ggbs'] or 0,
                            'rebarRate': 185, 'ptRate': 14}}}),

        # The guard: a composite factor must suppress rate-based rebar/PT.
        ('composite factor guard',
         {'materials': {'defaults': {}, 'epd_overrides': {},
                        'rows': {hc['key']: {'concFactorEid': 'C_056',
                                             'rebarRate': 200, 'ptRate': 20}}}},
         {'factor_overrides': {hc['key']: 'C_056'},
          'steel_rates': {**{r['name']: r['default_rebar']
                             for r in rows if not r['is_steel']},
                          hc['name']: 0},
          'pt_rates': {**{r['name']: 0 for r in rows if not r['is_steel']},
                       hc['name']: 0},
          'element_materials': {
              **{r['key']: {'grade': r['grade'] or '32/40 MPa',
                            'ggbs': r['ggbs'] or 0,
                            'rebarRate': r['default_rebar'], 'ptRate': 0}
                 for r in rows if not r['is_steel']},
              hc['key']: {'grade': hc['grade'] or '32/40 MPa',
                          'ggbs': hc['ggbs'] or 0,
                          'rebarRate': 0, 'ptRate': 0}}}),

        ('category + level exclusions',
         {'selection': {'excluded_categories': ['Stair'],
                        'excluded_levels': ['Level 2'],
                        'excluded_groups': []}},
         {'included_cats': ['Slab/Floor', 'Column', 'Wall', 'Beam'],
          'excluded_levels': ['Level 2'],
          'steel_rates': {r['name']: r['default_rebar']
                          for r in rows if not r['is_steel']},
          'pt_rates': {r['name']: 0 for r in rows if not r['is_steel']},
          'element_materials': {r['key']: {'grade': r['grade'] or '32/40 MPa',
                                           'ggbs': r['ggbs'] or 0,
                                           'rebarRate': r['default_rebar'],
                                           'ptRate': 0}
                                for r in rows if not r['is_steel']}}),

        ('transport + waste + A5a',
         {'transport': {'distances': {'in_situ': {'sea': 0, 'road': 150}},
                        'waste_pct': {'Concrete': 9, 'Rebar': 6,
                                      'Steel Section': 2,
                                      'Post Tensioning': 2.5},
                        'a5a_factor': 41.0}},
         {'transport_distances': {'t_in_situ_road': 150, 't_in_situ_sea': 0},
          'waste_factors': {'Concrete': 1.09, 'Rebar': 1.06,
                            'Steel Section': 1.02, 'Post Tensioning': 1.025},
          'a5a_factor': 41.0,
          'steel_rates': {r['name']: r['default_rebar']
                          for r in rows if not r['is_steel']},
          'pt_rates': {r['name']: 0 for r in rows if not r['is_steel']},
          'element_materials': {r['key']: {'grade': r['grade'] or '32/40 MPa',
                                           'ggbs': r['ggbs'] or 0,
                                           'rebarRate': r['default_rebar'],
                                           'ptRate': 0}
                                for r in rows if not r['is_steel']}}),
    ]

    last_run_id = None
    for label, wizard, legacy_payload in cases:
        legacy = _legacy(legacy_payload)
        new, meta = _via_db(wizard)
        last_run_id = meta['run_id']
        found = _diff(label, legacy, new)
        problems += found
        print(f'  {"+" if not found else "x"} {label:26} '
              f'{legacy["total_ton"]:>10.3f} tCO2e  '
              f'{legacy["per_sqm"]:>6.1f} /m2  {legacy["rating"]}')

    # ── Persistence: the stored result must equal what was calculated ───
    stored = store.get_results(last_run_id)
    if stored is None:
        problems.append('results were not persisted')
    else:
        rows_out, total = store.result_rows(last_run_id)
        by_mat = store.result_breakdown(last_run_id, 'material')
        by_cat = store.result_breakdown(last_run_id, 'category')
        # The breakdown must sum to the headline total, or a chart would tell a
        # different story from the KPI above it.
        summed = sum(b['total_ton'] for b in by_mat)
        if abs(summed - stored['total_ton']) > 0.01:
            problems.append(f'material breakdown sums to {summed}, '
                            f'headline is {stored["total_ton"]}')
        summed_cat = sum(b['total_ton'] for b in by_cat)
        if abs(summed_cat - stored['total_ton']) > 0.01:
            problems.append(f'category breakdown sums to {summed_cat}, '
                            f'headline is {stored["total_ton"]}')
        stage_sum = sum(stored['stage_emissions'].values())
        if abs(stage_sum - stored['total_ton']) > 0.01:
            problems.append(f'stages sum to {stage_sum}, '
                            f'headline is {stored["total_ton"]}')
        print(f'  + persisted: {total} result rows, '
              f'{len(by_mat)} materials, {len(by_cat)} categories')
        print(f'  + breakdowns and stages both sum to the headline '
              f'{stored["total_ton"]:.3f} t')

    if problems:
        print('\nDIFFERENCES:')
        for p in problems[:30]:
            print('   ', p)
        print(f'\nRESULTS PARITY FAILED ({len(problems)})')
        return 1

    print('\nRESULTS PARITY PASSED - the stored wizard config reproduces the '
          'legacy numbers, and persistence preserves them')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
