"""
Persistence round-trip: does storing and reloading change anything?
===================================================================
Two questions, and the second is the one that matters:

  1. Do the 20 UI fields survive the elements table unchanged?
  2. Does the DataFrame reloaded from Parquet still produce IDENTICAL carbon
     numbers to the one held in memory?

(2) exists because the DataFrame carries 17-28 columns that the UI contract does
not - Waste, Density(kg/m3), is_structural_steel, has_modeled_rebar, family,
ifc_type, width/depth, weight_kg - and several of them decide which emission
factor a row receives. A lossy round trip would not raise; it would just quietly
return different tonnes of CO2e.

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_persistence.py
"""

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]

# Point the DB and blob store at a scratch directory BEFORE app.db is imported,
# so a test run never touches the developer's real var/ directory.
_TMP = Path(tempfile.mkdtemp(prefix='clear_persist_'))
# Against Postgres when CLEAR_TEST_DATABASE_URL is set, throwaway SQLite
# otherwise. The suite must pass on BOTH: SQLite is what a developer runs,
# Postgres is what is deployed, and they differ in ways that matter
# (types, transaction behaviour, JSON handling).
os.environ['DATABASE_URL'] = (os.environ.get('CLEAR_TEST_DATABASE_URL')
                              or f'sqlite:///{(_TMP / "test.db").as_posix()}')
# Local disk, always — unless run_suite.py is explicitly pointing the
# whole suite at object storage. Without this a developer's .env, which
# may hold REAL production credentials, would silently make every
# harness write test junk into a live bucket.
os.environ.setdefault('STORAGE_BACKEND', 'local')
os.environ['BLOB_DIR'] = str(_TMP / 'blobs')

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PROJECT))

import pandas as pd  # noqa: E402

from app import store
from _helpers import run_with_upload as _run_with_upload  # noqa: E402
from app.db import init_db  # noqa: E402
from app.services.extraction import extract_quantities  # noqa: E402

READY_BOQ = b"""e_id,Category,Material,Description,Volume(m3),Mass(kg),Count
C_020,Slab/Floor,Concrete,Level 1 flat slab,100,,1
R_005,Slab/Floor,Rebar,Level 1 slab rebar,,15000,1
R_004,Beam,Steel Section,Steel UB 305x165,,5000,4
PT_032,Slab/Floor,Post Tensioning,Level 1 PT strand,,2000,1
"""

SCRATCH_BOQ = b"""Item Ref,Work Description,Element Group,Take-off amount,U/M
A1,300thk RC flat slab to Level 1,Slab,120.5,m3
A2,RC columns 400x400,Column,2.4,m3
A3,High yield reinforcement to slab,Rebar,9.25,tonnes
A4,Structural steel UC 203x203x46,Beam,5200,kg
A5,CLT floor panel 200mm,Slab,18.0,m3
"""


def _carbon(df: pd.DataFrame) -> dict:
    """Run the real engine over a frame and return the headline numbers."""
    from catalogue import EmissionCatalogue
    from engine import prepare_boq_from_ifc, run_calculations_from_boq
    from web_helpers import _build_distances

    cat = EmissionCatalogue()
    boq = prepare_boq_from_ifc(
        df, concrete_grade='32/40', ggbs_pct=25, steel_rates_dict={},
        waste_factors={'Concrete': 1.05, 'Rebar': 1.05,
                       'Steel Section': 1.01, 'Post Tensioning': 1.015},
        catalogue=cat)
    res = run_calculations_from_boq(
        boq, {'name': 'persist'}, _build_distances({}), 2000.0, cat)
    return {
        'total_ton': round(res.metrics['total_emission_ton'], 6),
        'per_sqm': round(res.metrics['total_emission_per_sqm'], 6),
        'rating': res.efficiency_rating,
        'by_material': dict(zip(res.material_types,
                                [round(v, 6) for v in res.material_emissions])),
        'by_stage': {k: round(float(v), 6) for k, v in res.stage_emissions.items()},
        'boq_rows': len(boq),
    }


def check(label: str, data: bytes, filename: str) -> list[str]:
    problems: list[str] = []

    src = _TMP / filename
    src.write_bytes(data)

    before = extract_quantities(str(src), src.suffix.lower())

    run = _run_with_upload(filename=filename, ext=src.suffix.lower(),
                           source=src, project={'name': label})
    store.save_extraction(run.id, before)

    # ── 1. The 20 UI fields ─────────────────────────────────────────────
    rows, total = store.get_elements(run.id)
    if total != len(before.elements):
        problems.append(f'{label}: stored {total} rows, extracted '
                        f'{len(before.elements)}')
    for i, (a, b) in enumerate(zip(before.elements, rows)):
        for k in a:
            av, bv = a[k], b.get(k)
            if isinstance(av, float) and isinstance(bv, float):
                if abs(av - bv) > 1e-9:
                    problems.append(f'{label}.elements[{i}].{k}: {av} vs {bv}')
            elif av != bv:
                problems.append(f'{label}.elements[{i}].{k}: {av!r} vs {bv!r}')

    # ── 2. Run summary ──────────────────────────────────────────────────
    reloaded = store.get(run.id)
    for field, expected in (('source_type', before.source_type),
                            ('n_elements', before.n_elements),
                            ('has_real_eids', before.has_real_eids),
                            ('categories', before.categories)):
        actual = getattr(reloaded, field)
        if actual != expected:
            problems.append(f'{label}.{field}: {expected!r} vs {actual!r}')

    # ── 3. The DataFrame, and the numbers it produces ───────────────────
    after_df = store.get_frame(run.id)
    if after_df is None:
        problems.append(f'{label}: no frame stored')
        return problems

    try:
        pd.testing.assert_frame_equal(before.elements_df, after_df,
                                      check_exact=True)
    except AssertionError as exc:
        problems.append(f'{label}: frame changed - {str(exc).splitlines()[0]}')

    carbon_before = _carbon(before.elements_df)
    carbon_after = _carbon(after_df)
    if carbon_before != carbon_after:
        for k in carbon_before:
            if carbon_before[k] != carbon_after[k]:
                problems.append(f'{label}.carbon.{k}: '
                                f'{carbon_before[k]!r} vs {carbon_after[k]!r}')

    print(f'  {"+" if not problems else "x"} {label:8} '
          f'rows={total:3} cols={len(after_df.columns):3} '
          f'total={carbon_before["total_ton"]:>10.3f} tCO2e  '
          f'{carbon_before["rating"]}')
    return problems


def main() -> int:
    init_db()
    problems: list[str] = []
    for label, data, filename in [
        ('ready', READY_BOQ, 'boq.csv'),
        ('scratch', SCRATCH_BOQ, 'contractor.csv'),
    ]:
        problems += check(label, data, filename)

    if problems:
        print('\nDIFFERENCES:')
        for p in problems[:30]:
            print('   ', p)
        print(f'\nPERSISTENCE FAILED ({len(problems)} differences)')
        return 1

    print('\nPERSISTENCE PASSED - rows, frame and carbon numbers all survive '
          'the round trip')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
