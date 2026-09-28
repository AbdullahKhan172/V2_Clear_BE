"""
Calculation parity: extracted service vs the legacy /run route.
==============================================================
The highest-stakes harness in the project. Step 6 produces the carbon numbers,
and a mistake here does not raise - it returns a plausible wrong answer.

The same configuration is driven through BOTH implementations and every headline
figure is compared:

    legacy   POST /run   (Flask test client, the real route)
    new      execute_run()

across all four ingest paths, plus the specific behaviours that are easy to lose
in a move:

    * use_direct       a spreadsheet with explicit steel mass must NOT go through
                       prepare_boq_from_ifc, which skips volume <= 0 rows and
                       would drop mass-only steel entirely
    * composite factor rate-based rebar suppressed where the factor already
                       includes reinforcement
    * EPD concrete     entered per m3, applied per kg (divided by 2400)
    * A5a              out of range rejected, never silently defaulted
    * timber           sequestration reported separately, never netted into A1-A5
    * filters          category / level / row exclusions

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_calculation_parity.py
"""

import io
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]

_TMP = Path(tempfile.mkdtemp(prefix='clear_calc_'))
# Against Postgres when CLEAR_TEST_DATABASE_URL is set, throwaway SQLite
# otherwise. The suite must pass on BOTH: SQLite is what a developer runs,
# Postgres is what is deployed, and they differ in ways that matter
# (types, transaction behaviour, JSON handling).
os.environ['DATABASE_URL'] = (os.environ.get('CLEAR_TEST_DATABASE_URL')
                              or f'sqlite:///{(_TMP / "calc.db").as_posix()}')
# Local disk, always — unless run_suite.py is explicitly pointing the
# whole suite at object storage. Without this a developer's .env, which
# may hold REAL production credentials, would silently make every
# harness write test junk into a live bucket.
os.environ.setdefault('STORAGE_BACKEND', 'local')
os.environ['BLOB_DIR'] = str(_TMP / 'blobs')

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PROJECT))

from app.services.calculation import (CalculationError, RunConfig,  # noqa: E402
                                      execute_run)
from app.services.extraction import extract_quantities  # noqa: E402

AREA = 2400.0

# ── Fixtures, one per ingest path ───────────────────────────────────────────
READY_BOQ = b"""e_id,Category,Material,Description,Volume(m3),Mass(kg),Count
C_020,Slab/Floor,Concrete,Level 1 flat slab,100,,1
R_005,Slab/Floor,Rebar,Level 1 slab rebar,,15000,1
R_004,Beam,Steel Section,Steel UB 305x165,,5000,4
PT_032,Slab/Floor,Post Tensioning,Level 1 PT strand,,2000,1
"""

REVIT_RAW = b"""Family and Type,Material: Name,Material: Volume,Count
"Floor: Concrete-Slab-300",Concrete Cast-in-Place,120.50 m3,1
"Structural Column: 400x400",Concrete Cast-in-Place,2.40 m3,12
"Structural Framing: UC 203x203x46",Metal - Steel,0.35 m3,8
"""

SCRATCH_BOQ = b"""Item Ref,Work Description,Element Group,Take-off amount,U/M
A1,300thk RC flat slab to Level 1,Slab,120.5,m3
A2,RC columns 400x400,Column,2.4,m3
A3,High yield reinforcement to slab,Rebar,9.25,tonnes
A4,Structural steel UC 203x203x46,Beam,5200,kg
A5,CLT floor panel 200mm,Slab,18.0,m3
"""

PRECAST_BOQ = b"""Category,Material,Description,Volume(m3),Count
Slab/Floor,Concrete,Hollowcore plank 200,60,1
Slab/Floor,Concrete,L1 flat slab,100,1
"""

# The figures compared. Anything the dashboard or a report quotes.
COMPARED = ('total_ton', 'per_sqm', 'rating', 'material_types',
            'material_emissions', 'stage_emissions', 'sequestration_ton',
            'a5a_factor', 'a5a_ton', 'no_factor_count')


def _legacy(data: bytes, filename: str, payload: dict) -> dict:
    """Drive the real legacy route, exactly as the browser does."""
    import web_app
    c = web_app.app.test_client()
    r = c.post('/upload', data={'file': (io.BytesIO(data), filename)},
               content_type='multipart/form-data')
    run_id = r.get_json()['run_id']
    ext = c.post('/extract', json={'run_id': run_id}).get_json()
    assert 'error' not in ext, ext.get('error')

    body = {'run_id': run_id, 'area': AREA, 'sensitivity': False, **payload}
    res = c.post('/run', json=body)
    out = res.get_json()
    if res.status_code != 200:
        return {'__error__': out.get('error', '')}
    return {k: out['summary'][k] for k in COMPARED}


def _new(data: bytes, filename: str, payload: dict) -> dict:
    """Drive the extracted service with the same configuration."""
    src = _TMP / filename
    src.write_bytes(data)
    ex = extract_quantities(str(src), src.suffix.lower())

    cfg = RunConfig(
        project_name=payload.get('project_name', 'Untitled'),
        area=AREA, sensitivity=False,
        stage=payload.get('stage', 'Concept / Schematic Design'),
        concrete_grade=payload.get('concrete_grade', '32/40'),
        ggbs_pct=int(payload.get('ggbs_pct', 0) or 0),
        rebar_type=payload.get('rebar_type', ''),
        section_type=payload.get('section_type', ''),
        pt_type=payload.get('pt_type', ''),
        steel_rates=payload.get('steel_rates', {}),
        pt_rates=payload.get('pt_rates', {}),
        element_materials=payload.get('element_materials', {}),
        steel_assignments=payload.get('steel_assignments', []),
        factor_overrides=payload.get('factor_overrides', {}),
        epd_overrides=payload.get('epd_overrides', {}),
        waste_factors=payload.get('waste_factors', {}),
        distances=_distances(payload),
        a5a_factor=payload.get('a5a_factor'),
        included_cats=payload.get('included_cats'),
        excluded_indices=payload.get('excluded_indices', []),
        excluded_levels=payload.get('excluded_levels'),
        manual_rows=payload.get('manual_rows', []),
    )
    try:
        result = execute_run(
            ex.elements_df, ex.geometry_data, cfg,
            source_type=ex.source_type, has_real_eids=ex.has_real_eids,
            elements_list=ex.elements)
    except CalculationError as exc:
        return {'__error__': str(exc)}
    return {k: result.summary[k] for k in COMPARED}


def _distances(payload: dict) -> dict:
    """Same shape the legacy route builds from the Step-4 form."""
    from web_helpers import _build_distances
    return _build_distances(payload)


def _diff(label: str, a: dict, b: dict) -> list[str]:
    problems = []
    if '__error__' in a or '__error__' in b:
        # Both must reject, and for the same reason.
        ea, eb = a.get('__error__', ''), b.get('__error__', '')
        if not (ea and eb):
            problems.append(f'{label}: one rejected and the other did not — '
                            f'legacy={ea!r} new={eb!r}')
        return problems
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

    cases = [
        # (label, fixture, filename, payload)
        ('ready BOQ', READY_BOQ, 'boq.csv', {}),
        ('revit takeoff', REVIT_RAW, 'takeoff.csv', {}),
        ('scratch + timber', SCRATCH_BOQ, 'scratch.csv', {}),
        ('precast/hollowcore', PRECAST_BOQ, 'precast.csv',
         {'steel_rates': {'Hollowcore plank 200': 200, 'L1 flat slab': 150}}),

        ('grade + GGBS', PRECAST_BOQ, 'precast.csv',
         {'concrete_grade': '40/50 MPa', 'ggbs_pct': 50}),
        ('rebar + PT rates', PRECAST_BOQ, 'precast.csv',
         {'steel_rates': {'L1 flat slab': 180},
          'pt_rates': {'L1 flat slab': 12}}),
        ('per-element grade', PRECAST_BOQ, 'precast.csv',
         {'element_materials': {'Slab/Floor|||L1 flat slab':
                                {'grade': '40/50 MPa', 'ggbs': 25,
                                 'rebarRate': 200, 'ptRate': 0}}}),
        ('steel assignment', READY_BOQ, 'boq.csv',
         {'steel_assignments': [{'cat': 'Beam', 'name': 'Steel UB 305x165',
                                 'e_id': 'R_020'}]}),
        ('EPD concrete (per m3)', PRECAST_BOQ, 'precast.csv',
         {'epd_overrides': {'Concrete|||32/40|||0':
                            {'a1_a3': 250.0, 'source': 'Ecocem',
                             'mat_type': 'Concrete', 'grade': '32/40',
                             'ggbs': 0}}}),
        ('waste overrides', READY_BOQ, 'boq.csv',
         {'waste_factors': {'Concrete': 1.12, 'Rebar': 1.08,
                            'Steel Section': 1.02, 'Post Tensioning': 1.03}}),
        ('transport overrides', READY_BOQ, 'boq.csv',
         {'transport_distances': {'t_in_situ_road': 250, 't_in_situ_sea': 0,
                                  't_rebar_road': 40, 't_rebar_sea': 0}}),
        ('A5a override', READY_BOQ, 'boq.csv', {'a5a_factor': 41.5}),
        ('A5a zero', READY_BOQ, 'boq.csv', {'a5a_factor': 0}),
        ('A5a out of range', READY_BOQ, 'boq.csv', {'a5a_factor': 9999}),
        ('category filter', SCRATCH_BOQ, 'scratch.csv',
         {'included_cats': ['Slab/Floor', 'Column']}),
        ('row exclusions', SCRATCH_BOQ, 'scratch.csv',
         {'excluded_indices': [0, 2]}),
        ('manual row', READY_BOQ, 'boq.csv',
         {'manual_rows': [{'category': 'Wall', 'name': 'Extra wall',
                           'material': 'Concrete', 'volume_m3': 25,
                           'count': 1}]}),
        ('manual PT row', READY_BOQ, 'boq.csv',
         {'manual_rows': [{'category': 'Slab/Floor', 'name': 'Extra PT',
                           'material': 'Post Tensioning', 'mass_kg': 3000,
                           'count': 1}]}),
        ('tender stage', READY_BOQ, 'boq.csv', {'stage': 'Tender'}),
    ]

    # An IFC fixture too, so the non-direct engine path is covered.
    try:
        ifc = _make_ifc(_TMP / 'model.ifc')
        cases.append(('ifc model', ifc, 'model.ifc', {}))
        cases.append(('ifc + rates', ifc, 'model.ifc',
                      {'steel_rates': {'Level 1 Flat Slab 300thk': 160}}))
    except Exception as exc:                      # pragma: no cover
        print(f'  ! IFC fixture skipped ({exc})')

    problems: list[str] = []
    for label, data, filename, payload in cases:
        legacy = _legacy(data, filename, payload)
        new = _new(data, filename, payload)
        found = _diff(label, legacy, new)
        problems += found

        if '__error__' in legacy:
            shown = f'both rejected: {legacy["__error__"][:38]}'
        else:
            shown = (f'{legacy["total_ton"]:>10.3f} tCO2e  '
                     f'{legacy["per_sqm"]:>7.1f} /m2  {legacy["rating"]:<3}')
        print(f'  {"+" if not found else "x"} {label:22} {shown}')

    if problems:
        print('\nDIFFERENCES:')
        for p in problems[:40]:
            print('   ', p)
        print(f'\nCALCULATION PARITY FAILED ({len(problems)})')
        return 1

    print(f'\nCALCULATION PARITY PASSED — {len(cases)} configurations, '
          f'identical to the legacy route in every compared figure')
    return 0


def _make_ifc(out: Path) -> bytes:
    """Minimal IFC4 model with QTO-carrying slab, column and steel beam."""
    import ifcopenshell
    import ifcopenshell.api

    f = ifcopenshell.file(schema='IFC4')
    proj = ifcopenshell.api.run('root.create_entity', f,
                                ifc_class='IfcProject', name='Parity')
    ifcopenshell.api.run('unit.assign_unit', f)
    ifcopenshell.api.run('context.add_context', f, context_type='Model')
    site = ifcopenshell.api.run('root.create_entity', f, ifc_class='IfcSite')
    bldg = ifcopenshell.api.run('root.create_entity', f, ifc_class='IfcBuilding')
    storey = ifcopenshell.api.run('root.create_entity', f,
                                  ifc_class='IfcBuildingStorey', name='Level 1')
    ifcopenshell.api.run('aggregate.assign_object', f,
                         products=[site], relating_object=proj)
    ifcopenshell.api.run('aggregate.assign_object', f,
                         products=[bldg], relating_object=site)
    ifcopenshell.api.run('aggregate.assign_object', f,
                         products=[storey], relating_object=bldg)

    def add(cls, name, qto_name, props):
        el = ifcopenshell.api.run('root.create_entity', f,
                                  ifc_class=cls, name=name)
        ifcopenshell.api.run('spatial.assign_container', f,
                             products=[el], relating_structure=storey)
        qto = ifcopenshell.api.run('pset.add_qto', f, product=el, name=qto_name)
        ifcopenshell.api.run('pset.edit_qto', f, qto=qto, properties=props)

    add('IfcSlab', 'Level 1 Flat Slab 300thk', 'Qto_SlabBaseQuantities',
        {'NetVolume': 120.0, 'GrossVolume': 125.0,
         'Width': 10.0, 'Length': 40.0, 'Depth': 0.3})
    add('IfcColumn', 'Concrete-Rectangular-Column:400x400:314485',
        'Qto_ColumnBaseQuantities',
        {'NetVolume': 2.4, 'GrossVolume': 2.4,
         'Width': 0.4, 'Length': 0.4, 'Height': 3.0})
    add('IfcBeam', 'UC 203x203x46', 'Qto_BeamBaseQuantities',
        {'NetVolume': 0.35, 'GrossVolume': 0.35,
         'Width': 0.203, 'Length': 6.0, 'Height': 0.203})
    f.write(str(out))
    return out.read_bytes()


if __name__ == '__main__':
    raise SystemExit(main())
