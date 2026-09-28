"""
Selection parity: does the new filtering remove the same rows as the legacy app?
===============================================================================
Step 2 decides what gets counted, so a filtering bug does not crash - it just
quietly returns the wrong tonnes of CO2e. This harness pins the behaviour.

Two things are checked, for several selections:

  1. ROW PARITY   the rows surviving the new key-based filter are exactly the
                  rows surviving the legacy positional filter (web_app.py:645-668)
  2. CARBON PARITY the two filtered frames produce identical carbon numbers

The legacy UI sends positional row numbers; the new UI sends group keys, which
the server expands. This proves the two arrive at the same place.

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_selection_parity.py
"""

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]

_TMP = Path(tempfile.mkdtemp(prefix='clear_sel_'))
# Against Postgres when CLEAR_TEST_DATABASE_URL is set, throwaway SQLite
# otherwise. The suite must pass on BOTH: SQLite is what a developer runs,
# Postgres is what is deployed, and they differ in ways that matter
# (types, transaction behaviour, JSON handling).
os.environ['DATABASE_URL'] = (os.environ.get('CLEAR_TEST_DATABASE_URL')
                              or f'sqlite:///{(_TMP / "sel.db").as_posix()}')
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

# Mixed categories, levels and materials so category, level and per-group
# exclusions all have something to bite on.
BOQ = b"""e_id,Category,Material,Description,Volume(m3),Mass(kg),Count,Level
C_020,Slab/Floor,Concrete,L1 flat slab,100,,1,Level 1
C_020,Slab/Floor,Concrete,L2 flat slab,90,,1,Level 2
C_020,Column,Concrete,Column 400x400,12,,8,Level 1
C_020,Column,Concrete,Column 400x400,12,,8,Level 2
C_020,Wall,Concrete,Core wall 250,45,,4,Level 1
R_005,Slab/Floor,Rebar,L1 slab rebar,,15000,1,Level 1
R_004,Beam,Steel Section,Steel UB 305x165,,5000,4,Level 2
C_020,Stair,Concrete,Stair flight,6,,3,Level 1
"""


def _legacy_filter(elements_df: pd.DataFrame, included_cats, excluded_indices,
                   excluded_levels) -> pd.DataFrame:
    """web_app.py:645-668, verbatim in behaviour."""
    cat_col = next((c for c in ('Category', 'category')
                    if c in elements_df.columns), None)
    level_col = next((c for c in ('level', 'Level')
                      if c in elements_df.columns), None)

    mask = pd.Series([True] * len(elements_df), index=elements_df.index)

    if included_cats is not None and cat_col:
        mask = mask & elements_df[cat_col].astype(str).isin(set(included_cats))

    if excluded_levels and level_col:
        mask = mask & ~elements_df[level_col].astype(str).isin(set(excluded_levels))

    if excluded_indices:
        order = list(elements_df.index)
        pos_to_idx = {i: df_idx for i, df_idx in enumerate(order)}
        pos = {int(i) for i in excluded_indices}
        drop = {pos_to_idx[i] for i in pos if i in pos_to_idx}
        if drop:
            mask = mask & ~elements_df.index.isin(drop)

    return elements_df[mask]


def _new_filter(run_id: str, elements_df: pd.DataFrame,
                selection: dict) -> pd.DataFrame:
    """Key-based selection resolved to rows, then dropped positionally."""
    excluded = store.resolve_excluded_indices(run_id, selection)
    if not excluded:
        return elements_df
    order = list(elements_df.index)
    drop = {order[i] for i in excluded if 0 <= i < len(order)}
    return elements_df[~elements_df.index.isin(drop)]


def _carbon(df: pd.DataFrame) -> dict:
    if df.empty:
        return {'total_ton': 0.0, 'rows': 0}
    from catalogue import EmissionCatalogue
    from engine import prepare_boq_from_ifc, run_calculations_from_boq
    from web_helpers import _build_distances

    cat = EmissionCatalogue()
    boq = prepare_boq_from_ifc(
        df, concrete_grade='32/40', ggbs_pct=25, steel_rates_dict={},
        waste_factors={'Concrete': 1.05, 'Rebar': 1.05,
                       'Steel Section': 1.01, 'Post Tensioning': 1.015},
        catalogue=cat)
    if boq.empty:
        return {'total_ton': 0.0, 'rows': 0}
    res = run_calculations_from_boq(
        boq, {'name': 'sel'}, _build_distances({}), 2000.0, cat)
    return {'total_ton': round(res.metrics['total_emission_ton'], 6),
            'rows': len(boq)}


def main() -> int:
    init_db()
    src = _TMP / 'boq.csv'
    src.write_bytes(BOQ)

    result = extract_quantities(str(src), '.csv')
    run = _run_with_upload(filename='boq.csv', ext='.csv', source=src)
    store.save_extraction(run.id, result)
    df = result.elements_df

    all_cats = sorted({g['cat'] for g in store.element_groups(run.id)})
    groups = store.element_groups(run.id)
    print(f'  {len(df)} rows · {len(all_cats)} categories · '
          f'{len(groups)} groups · {len(store.level_totals(run.id))} levels\n')

    # A group with volume, chosen explicitly. Picking groups[0] blindly lands on
    # a mass-only steel row, and prepare_boq_from_ifc skips rows with volume <= 0
    # - so the carbon check would pass without ever exercising anything.
    volumetric = next(g for g in groups if g['vol'] > 0 and not g['is_steel'])
    steel_group = next(g for g in groups if g['is_steel'])

    # (label, selection) - each expressed the NEW way, in keys not positions.
    cases = [
        ('nothing excluded', {}),
        ('drop a category', {'excluded_categories': ['Stair']}),
        ('drop two categories', {'excluded_categories': ['Stair', 'Wall']}),
        ('drop a level', {'excluded_levels': ['Level 2']}),
        ('drop a concrete group', {'excluded_groups': [volumetric['key']]}),
        ('drop a steel group', {'excluded_groups': [steel_group['key']]}),
        ('category + level + group',
         {'excluded_categories': ['Stair'], 'excluded_levels': ['Level 2'],
          'excluded_groups': [volumetric['key']]}),
    ]

    problems: list[str] = []
    for label, selection in cases:
        sel = {'excluded_categories': selection.get('excluded_categories', []),
               'excluded_levels': selection.get('excluded_levels', []),
               'excluded_groups': selection.get('excluded_groups', [])}

        # Legacy speaks in included categories and positional row numbers.
        included = [c for c in all_cats if c not in set(sel['excluded_categories'])]
        positional = store.resolve_excluded_indices(
            run.id, {'excluded_groups': sel['excluded_groups']})

        legacy = _legacy_filter(df, included, positional, sel['excluded_levels'])
        new = _new_filter(run.id, df, sel)

        if list(legacy.index) != list(new.index):
            problems.append(f'{label}: rows differ - legacy {list(legacy.index)} '
                            f'vs new {list(new.index)}')

        c_legacy, c_new = _carbon(legacy), _carbon(new)
        if c_legacy != c_new:
            problems.append(f'{label}: carbon differs - {c_legacy} vs {c_new}')

        # Saving and reloading must not change the selection.
        store.save_selection(run.id, sel)
        if store.get_selection(run.id) != sel:
            problems.append(f'{label}: selection changed across save/reload')

        print(f'  {"+" if not problems else "x"} {label:26} '
              f'kept {len(new):2}/{len(df)} rows · '
              f'{c_new["total_ton"]:>9.3f} tCO2e')

    if problems:
        print('\nDIFFERENCES:')
        for p in problems[:20]:
            print('   ', p)
        print(f'\nSELECTION PARITY FAILED ({len(problems)})')
        return 1

    print('\nSELECTION PARITY PASSED - key-based filtering matches the legacy '
          'positional filter, row for row and number for number')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
