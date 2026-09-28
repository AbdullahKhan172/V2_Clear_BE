"""
Materials parity: do saved Step-3 settings produce the legacy numbers?
======================================================================
Step 3 chooses which emission factor every element gets, so a mistake here does
not fail - it returns the wrong tonnes. This pins the translation from stored
settings to engine arguments.

What is checked:
  1. KEY FORMAT   the material key splits into (cat, name) exactly the way the
                  engine does (web_app.py:756), or per-element grades silently
                  stop applying
  2. GRADE + GGBS a per-row grade/GGBS override resolves to the same e_id, and
                  changing GGBS actually changes the carbon
  3. RATES        per-row rebar and PT rates reach the BOQ as real rows
  4. GUARD        a factor that already includes reinforcement suppresses
                  rate-based rebar, so steel is never counted twice

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_materials_parity.py
"""

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]

_TMP = Path(tempfile.mkdtemp(prefix='clear_mat_'))
# Against Postgres when CLEAR_TEST_DATABASE_URL is set, throwaway SQLite
# otherwise. The suite must pass on BOTH: SQLite is what a developer runs,
# Postgres is what is deployed, and they differ in ways that matter
# (types, transaction behaviour, JSON handling).
os.environ['DATABASE_URL'] = (os.environ.get('CLEAR_TEST_DATABASE_URL')
                              or f'sqlite:///{(_TMP / "mat.db").as_posix()}')
# Local disk, always — unless run_suite.py is explicitly pointing the
# whole suite at object storage. Without this a developer's .env, which
# may hold REAL production credentials, would silently make every
# harness write test junk into a live bucket.
os.environ.setdefault('STORAGE_BACKEND', 'local')
os.environ['BLOB_DIR'] = str(_TMP / 'blobs')

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(PROJECT))

import pandas as pd  # noqa: E402

from app import store  # noqa: E402
from _helpers import run_with_upload as _run_with_upload
from app.db import init_db  # noqa: E402
from app.services.extraction import extract_quantities  # noqa: E402

BOQ = b"""Category,Material,Description,Volume(m3),Count,Level
Slab/Floor,Concrete,L1 flat slab,100,1,Level 1
Column,Concrete,Column 400x400,12,8,Level 1
Wall,Concrete,Core wall 250,45,4,Level 1
Slab/Floor,Concrete,Hollowcore plank 200,60,1,Level 2
"""


def _run_engine(df, *, grade='32/40', ggbs=0, member_eids=None,
                steel_rates=None, pt_rates=None):
    """The engine, driven the way web_app.py's /run drives it."""
    from catalogue import EmissionCatalogue
    from engine import prepare_boq_from_ifc, run_calculations_from_boq
    from web_helpers import _build_distances

    cat = EmissionCatalogue()
    boq = prepare_boq_from_ifc(
        df, concrete_grade=grade, ggbs_pct=ggbs,
        steel_rates_dict=steel_rates or {},
        waste_factors={'Concrete': 1.05, 'Rebar': 1.05,
                       'Steel Section': 1.01, 'Post Tensioning': 1.015},
        catalogue=cat, pt_rates_dict=pt_rates or {},
        member_eids=member_eids or {})
    res = run_calculations_from_boq(
        boq, {'name': 'mat'}, _build_distances({}), 2000.0, cat)
    return boq, round(res.metrics['total_emission_ton'], 6)


def main() -> int:
    init_db()
    src = _TMP / 'boq.csv'
    src.write_bytes(BOQ)

    result = extract_quantities(str(src), '.csv')
    run = _run_with_upload(filename='boq.csv', ext='.csv', source=src)
    store.save_extraction(run.id, result)
    df = result.elements_df

    rows = store.material_rows(run.id)
    print(f'  {len(rows)} material rows\n')

    problems: list[str] = []

    # ── 1. Key format must survive the engine's split ───────────────────
    for r in rows:
        if r['is_steel']:
            continue
        cat, name = r['key'].split('|||', 1)
        if cat != r['cat'] or name != r['name']:
            problems.append(f"key {r['key']!r} splits to ({cat!r}, {name!r}), "
                            f"expected ({r['cat']!r}, {r['name']!r})")
    print(f'  + key format splits to (cat, name) for all '
          f'{sum(1 for r in rows if not r["is_steel"])} concrete rows')

    # ── 2. Global grade + GGBS ──────────────────────────────────────────
    _, base = _run_engine(df, grade='32/40', ggbs=0)
    _, ggbs50 = _run_engine(df, grade='32/40', ggbs=50)
    if not ggbs50 < base:
        problems.append(f'50% GGBS ({ggbs50}) should be below 0% ({base})')
    print(f'  + global GGBS 0% -> {base:.3f} t   50% -> {ggbs50:.3f} t '
          f'({(1 - ggbs50 / base) * 100:.1f}% saving)')

    # ── 3. Per-row grade override reaches only that row ─────────────────
    from catalogue import EmissionCatalogue
    cat_obj = EmissionCatalogue()
    target = next(r for r in rows if r['cat'] == 'Column')
    eid_40 = cat_obj.get_concrete_eid('40/50', 0)
    member_eids = {target['key']: eid_40, target['name']: eid_40}
    boq_ov, total_ov = _run_engine(df, grade='32/40', ggbs=0,
                                   member_eids=member_eids)

    col_eids = set(boq_ov[(boq_ov['Category'] == 'Column')
                          & (boq_ov['Material'] == 'Concrete')]['e_id'])
    slab_eids = set(boq_ov[(boq_ov['Category'] == 'Slab/Floor')
                           & (boq_ov['Material'] == 'Concrete')]['e_id'])
    if col_eids != {eid_40}:
        problems.append(f'column override did not apply: {col_eids}')
    if eid_40 in slab_eids:
        problems.append(f'column override leaked to slabs: {slab_eids}')
    print(f'  + per-row override: columns -> {col_eids}, '
          f'slabs unchanged -> {slab_eids}')

    # ── 4. Rebar + PT rates become real BOQ rows ────────────────────────
    slab = next(r for r in rows if r['cat'] == 'Slab/Floor'
                and 'flat slab' in r['name'])
    boq_r, total_r = _run_engine(
        df, steel_rates={slab['key']: 200, slab['name']: 200},
        pt_rates={slab['key']: 15, slab['name']: 15})
    rebar_rows = boq_r[boq_r['Description'].str.contains('Rebar', na=False)]
    pt_rows = boq_r[boq_r['Description'].str.contains('PT Steel', na=False)]
    if rebar_rows.empty:
        problems.append('rebar rate produced no BOQ row')
    if pt_rows.empty:
        problems.append('PT rate produced no BOQ row')
    print(f'  + rates -> {len(rebar_rows)} rebar rows, {len(pt_rows)} PT rows, '
          f'total {total_r:.3f} t')

    # ── 5. includes_reinforcement suppresses rate-based rebar ───────────
    # C_056 (precast hollowcore) already prices its reinforcement. Assigning it
    # must NOT also add a rate-based rebar row, or the steel is double counted.
    hc = next(r for r in rows if 'Hollowcore' in r['name'])
    boq_hc, _ = _run_engine(
        df, member_eids={hc['key']: 'C_056', hc['name']: 'C_056'},
        steel_rates={hc['key']: 200, hc['name']: 200})
    hc_rebar = boq_hc[(boq_hc['Category'] == 'Slab/Floor')
                      & boq_hc['Description'].str.contains(hc['name'], na=False)
                      & boq_hc['Description'].str.contains('Rebar', na=False)]
    if not hc_rebar.empty:
        problems.append('composite factor still generated rate-based rebar - '
                        'reinforcement would be double counted')
    print(f'  + composite factor C_056 suppressed rate-based rebar '
          f'({len(hc_rebar)} rows, expected 0)')

    # ── 5b. An UNTOUCHED auto-routed factor must SURVIVE ────────────────
    # Extraction routes a hollowcore plank to its precast factor. If the user
    # never opens Materials, build_run_config must not hand the engine a
    # grade-derived e_id for that row: the engine reads any member_eids entry as
    # a deliberate user assignment and then skips BOTH its precast auto-routing
    # and the composite-factor guard above. That regression re-priced hollowcore
    # as in-situ concrete AND added the reinforcement the factor already
    # includes - on a real model, a 6.4% error in the headline total.
    from app.services.calculation import execute_run
    from app.services.run_config import build_run_config

    run_obj = store.get(run.id)
    auto_cfg = build_run_config(run_obj, sensitivity=False)
    auto_cfg.area = 2000.0        # the fixture run carries no project details
    hc_keys = [k for k in (auto_cfg.element_materials or {}) if 'Hollowcore' in k]
    if hc_keys:
        problems.append(
            f'auto-routed hollowcore row was given a grade override '
            f'({hc_keys}) - its precast factor will be discarded')
    if auto_cfg.steel_rates.get(hc['name'], 0) != 0:
        problems.append('auto-routed composite factor kept a non-zero rebar '
                        'rate - the reported rate is not the one applied')

    auto_res = execute_run(
        df, [], auto_cfg, source_type=run_obj.source_type,
        has_real_eids=bool(run_obj.has_real_eids), elements_list=result.elements)
    auto_boq = auto_res.boq_df
    hc_mask = auto_boq['Description'].astype(str).str.contains(
        'Hollowcore plank 200', na=False)
    hc_eids = set(auto_boq.loc[hc_mask, 'e_id'].astype(str))
    if 'C_056' not in hc_eids:
        problems.append(f'untouched hollowcore lost its precast factor: '
                        f'{hc_eids or "{}"} (expected C_056)')
    if any('Rebar' in d for d in auto_boq.loc[hc_mask, 'Description'].astype(str)):
        problems.append('untouched composite factor generated rate-based rebar')
    print(f'  + untouched hollowcore keeps {sorted(hc_eids)} with no added rebar')

    # ── 6. Settings survive save + reload ───────────────────────────────
    payload = {
        'defaults': {'concrete_grade': '32/40 MPa', 'ggbs_pct': 25,
                     'rebar_type': 'Ireland Average Reinforcing Steel'},
        'rows': {target['key']: {'grade': '40/50 MPa', 'ggbs': 0,
                                 'rebarRate': 325, 'ptRate': 0}},
        'epd_overrides': {'Concrete|||32/40|||25':
                          {'a1_a3': 285.0, 'source': 'Ecocem',
                           'mat_type': 'Concrete', 'grade': '32/40',
                           'ggbs': 25, 'e_id': None}},
    }
    store.save_materials(run.id, payload)
    back = store.get_materials(run.id)
    if back['defaults'] != payload['defaults']:
        problems.append(f'defaults changed: {back["defaults"]}')
    if back['rows'] != payload['rows']:
        problems.append(f'rows changed: {back["rows"]}')
    if back['epd_overrides'] != payload['epd_overrides']:
        problems.append(f'epd changed: {back["epd_overrides"]}')
    print('  + defaults, row overrides and EPD survive save/reload')

    if problems:
        print('\nPROBLEMS:')
        for p in problems[:20]:
            print('   ', p)
        print(f'\nMATERIALS PARITY FAILED ({len(problems)})')
        return 1

    print('\nMATERIALS PARITY PASSED - settings resolve to the same factors, '
          'rates and guards as the legacy app')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
