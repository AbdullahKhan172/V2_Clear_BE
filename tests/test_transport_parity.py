"""
Transport & waste parity: do Step-4 values reach the engine unchanged?
======================================================================
The risk here is arithmetic, not crashes. Waste passes through two conversions
between the screen and the engine, and A4 distance is a straight multiplier on
transport carbon - so a factor-of-100 slip produces a plausible-looking number
rather than an error.

What is checked:
  1. WASTE CHAIN    5 (percent) -> 1.05 (multiplier) -> 0.05 (fraction), and the
                    fraction actually drives A5, matching the legacy conversion
                    at web_app.py:988-991
  2. DISTANCES      nested per-family values become the flat *_sea_distance /
                    *_road_distance keys the engine reads, and changing them
                    moves A4 in the right direction
  3. DEFAULTS       a family the user never touched falls back to its SEAI
                    default - the behaviour the legacy UI relied on for the two
                    families it never rendered (blockwork, sheet steel)
  4. A5a            factor x floor area, with 0 honoured as a real choice rather
                    than treated as "unset"

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_transport_parity.py
"""

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]

_TMP = Path(tempfile.mkdtemp(prefix='clear_tr_'))
# Against Postgres when CLEAR_TEST_DATABASE_URL is set, throwaway SQLite
# otherwise. The suite must pass on BOTH: SQLite is what a developer runs,
# Postgres is what is deployed, and they differ in ways that matter
# (types, transaction behaviour, JSON handling).
os.environ['DATABASE_URL'] = (os.environ.get('CLEAR_TEST_DATABASE_URL')
                              or f'sqlite:///{(_TMP / "tr.db").as_posix()}')
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
from app.services.extraction import extract_quantities  # noqa: E402

BOQ = b"""Category,Material,Description,Volume(m3),Count
Slab/Floor,Concrete,L1 flat slab,100,1
Column,Concrete,Column 400x400,12,8
"""

AREA = 2000.0


def _engine(df, *, distances=None, waste_pct=None, a5a=None):
    """Run the engine the way /run does, and return the stage totals."""
    from catalogue import EmissionCatalogue
    from engine import prepare_boq_from_ifc, run_calculations_from_boq
    from web_helpers import _build_distances

    cat = EmissionCatalogue()
    # Start from the SEAI defaults, then overlay whatever Step 4 supplied -
    # exactly what _build_distances does for the legacy payload.
    dist = _build_distances({})
    dist.update(store.distances_to_engine(distances or {}))

    boq = prepare_boq_from_ifc(
        df, concrete_grade='32/40', ggbs_pct=0, steel_rates_dict={},
        waste_factors=store.waste_pct_to_multiplier(
            waste_pct or {'Concrete': 5, 'Rebar': 5,
                          'Steel Section': 1, 'Post Tensioning': 1.5}),
        catalogue=cat)
    res = run_calculations_from_boq(
        boq, {'name': 'tr'}, dist, AREA, cat,
        a5w_waste_pcts=store.waste_pct_to_fraction(
            waste_pct or {'Concrete': 5, 'Rebar': 5,
                          'Steel Section': 1, 'Post Tensioning': 1.5}),
        a5a_factor=a5a)
    return {k: round(float(v), 6) for k, v in res.stage_emissions.items()}


def main() -> int:
    init_db()
    src = _TMP / 'boq.csv'
    src.write_bytes(BOQ)
    result = extract_quantities(str(src), '.csv')
    run = _run_with_upload(filename='boq.csv', ext='.csv', source=src)
    store.save_extraction(run.id, result)
    df = result.elements_df

    problems: list[str] = []

    # ── 1. The waste conversion chain ───────────────────────────────────
    mult = store.waste_pct_to_multiplier({'Concrete': 5})
    frac = store.waste_pct_to_fraction({'Concrete': 5})
    if abs(mult['Concrete'] - 1.05) > 1e-12:
        problems.append(f'5% should be multiplier 1.05, got {mult["Concrete"]}')
    if abs(frac['Concrete'] - 0.05) > 1e-12:
        problems.append(f'5% should be fraction 0.05, got {frac["Concrete"]}')
    # The legacy conversion, reproduced: multiplier - 1.0, clamped at zero.
    if abs(max(mult['Concrete'] - 1.0, 0.0) - frac['Concrete']) > 1e-12:
        problems.append('fraction disagrees with the legacy multiplier-1.0 route')
    print(f'  + waste chain  5% -> {mult["Concrete"]} -> {frac["Concrete"]}'
          f'  (matches web_app.py:988-991)')

    # Waste must actually move A5, and only A5.
    base = _engine(df)
    heavy = _engine(df, waste_pct={'Concrete': 20, 'Rebar': 5,
                                   'Steel Section': 1, 'Post Tensioning': 1.5})
    if not heavy['A5'] > base['A5']:
        problems.append(f'20% waste did not raise A5 ({heavy["A5"]} vs {base["A5"]})')
    if abs(heavy['A1-A3'] - base['A1-A3']) > 1e-9:
        problems.append('waste changed A1-A3, which it must not')
    print(f'  + waste 5% -> 20% raises A5 {base["A5"]:.3f} -> {heavy["A5"]:.3f} t, '
          f'A1-A3 unchanged')

    # ── 2. Distances drive A4 ───────────────────────────────────────────
    far = _engine(df, distances={'in_situ': {'sea': 0, 'road': 200}})
    if not far['A4'] > base['A4']:
        problems.append(f'10x road distance did not raise A4 '
                        f'({far["A4"]} vs {base["A4"]})')
    flat = store.distances_to_engine({'in_situ': {'sea': 0, 'road': 200}})
    if flat != {'in_situ_sea_distance': 0.0, 'in_situ_road_distance': 200.0}:
        problems.append(f'flattening produced {flat}')
    print(f'  + in-situ road 20 -> 200 km raises A4 '
          f'{base["A4"]:.3f} -> {far["A4"]:.3f} t')

    # ── 3. Untouched families keep their SEAI defaults ──────────────────
    from calculations import SEAI_TRANSPORT_DEFAULTS
    from web_helpers import _build_distances
    dist = _build_distances({})
    dist.update(store.distances_to_engine({'in_situ': {'sea': 0, 'road': 200}}))
    for fam, (road, sea) in SEAI_TRANSPORT_DEFAULTS.items():
        if fam == 'in_situ':
            continue
        if dist.get(f'{fam}_road_distance') != road:
            problems.append(f'{fam} road default lost: '
                            f'{dist.get(f"{fam}_road_distance")} != {road}')
        if dist.get(f'{fam}_sea_distance') != sea:
            problems.append(f'{fam} sea default lost')
    print(f'  + {len(SEAI_TRANSPORT_DEFAULTS) - 1} untouched families kept their '
          f'SEAI defaults (incl. block + sheet, which the old UI never showed)')

    # ── 4. A5a ──────────────────────────────────────────────────────────
    a5a_50 = _engine(df, a5a=50.0)
    a5a_0 = _engine(df, a5a=0.0)
    expected_gap = 50.0 * AREA / 1000.0          # kg -> tonnes
    if abs((a5a_50['A5'] - a5a_0['A5']) - expected_gap) > 1e-6:
        problems.append(f'A5a delta {a5a_50["A5"] - a5a_0["A5"]:.6f} != '
                        f'{expected_gap:.6f}')
    if a5a_0['A5'] >= base['A5']:
        problems.append('A5a of 0 was not honoured as a real choice')
    print(f'  + A5a 0 -> 50 adds exactly {expected_gap:.1f} t '
          f'(50 x {AREA:.0f} m2), and 0 is honoured')

    # ── 5. Round trip ───────────────────────────────────────────────────
    payload = {
        'distances': {'in_situ': {'sea': 0.0, 'road': 35.0},
                      'timber': {'sea': 100.0, 'road': 900.0}},
        'waste_pct': {'Concrete': 7.5, 'Rebar': 5.0,
                      'Steel Section': 1.0, 'Post Tensioning': 1.5},
        'a5a_factor': 0.0,
    }
    store.save_transport(run.id, payload)
    back = store.get_transport(run.id)
    if back['distances'] != payload['distances']:
        problems.append(f'distances changed: {back["distances"]}')
    if back['waste_pct'] != payload['waste_pct']:
        problems.append(f'waste changed: {back["waste_pct"]}')
    if back['a5a_factor'] != 0.0:
        problems.append(f'a5a 0 became {back["a5a_factor"]!r} - 0 must survive as '
                        f'a real value, not collapse to None')
    print('  + distances, waste and an A5a of 0 all survive save/reload')

    if problems:
        print('\nPROBLEMS:')
        for p in problems[:20]:
            print('   ', p)
        print(f'\nTRANSPORT PARITY FAILED ({len(problems)})')
        return 1

    print('\nTRANSPORT PARITY PASSED - distances, waste and A5a reach the engine '
          'exactly as the legacy wizard delivered them')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
