"""
Parity harness: extracted service vs the legacy Flask implementation.
=====================================================================
Runs the SAME four ingest paths (ready BOQ, Revit takeoff, scratch spreadsheet,
IFC) through BOTH the legacy `web_app` routes and the new
`app.services.extraction.extract_quantities`, and asserts the element contract
and every value are identical.

This is the safety net for the port: the legacy app stays in the repo precisely
so this diff can be run. Any divergence here means the extraction changed
behaviour and must be fixed before building on top of it.

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_extraction_parity.py
"""

import io
import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]
# These harnesses never write a blob, but they DO import `app`, which loads
# webapp/backend/.env - and that file may hold real production credentials.
# Pinning the backend makes reaching a live bucket impossible rather than
# merely unlikely. run_suite.py --s3 is the one way to opt in.
os.environ.setdefault('STORAGE_BACKEND', 'local')

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PROJECT))

from app.services.extraction import extract_quantities  # noqa: E402

# ── Fixtures: one per ingest path ────────────────────────────────────────────
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
"Wall: Core Wall 250",Concrete Cast-in-Place,45.00 m3,4
"""

SCRATCH_BOQ = b"""Item Ref,Work Description,Element Group,Take-off amount,U/M
A1,300thk RC flat slab to Level 1,Slab,120.5,m3
A2,RC columns 400x400,Column,2.4,m3
A3,High yield reinforcement to slab,Rebar,9.25,tonnes
A4,Structural steel UC 203x203x46,Beam,5200,kg
A5,CLT floor panel 200mm,Slab,18.0,m3
"""

# Compared fields: everything the frontend consumes. elements_df / geometry are
# in-process artefacts, not part of the wire contract.
COMPARED = ('source_type', 'n_elements', 'categories',
            'default_rates', 'has_real_eids', 'elements')


def _write(tmp: Path, name: str, data: bytes) -> Path:
    p = tmp / name
    p.write_bytes(data)
    return p


def _legacy(data: bytes, filename: str) -> dict:
    """Drive the real legacy Flask routes exactly as the browser does."""
    import web_app
    c = web_app.app.test_client()
    r = c.post('/upload', data={'file': (io.BytesIO(data), filename)},
               content_type='multipart/form-data')
    run_id = r.get_json()['run_id']
    body = c.post('/extract', json={'run_id': run_id}).get_json()
    assert 'error' not in body, f"legacy extract failed: {body.get('error')}"
    return {k: body[k] for k in COMPARED}


def _new(path: Path) -> dict:
    res = extract_quantities(str(path), path.suffix.lower())
    d = res.to_public_dict()
    return {k: d[k] for k in COMPARED}


def _diff(label: str, legacy: dict, new: dict) -> list[str]:
    """Return human-readable differences; empty list means identical."""
    problems = []
    for key in COMPARED:
        a, b = legacy[key], new[key]
        if key == 'elements':
            if len(a) != len(b):
                problems.append(f'{label}.elements: len {len(a)} vs {len(b)}')
                continue
            for i, (ra, rb) in enumerate(zip(a, b)):
                if set(ra) != set(rb):
                    problems.append(
                        f'{label}.elements[{i}]: key mismatch '
                        f'{set(ra) ^ set(rb)}')
                    continue
                for k in ra:
                    if ra[k] != rb[k]:
                        problems.append(
                            f'{label}.elements[{i}].{k}: {ra[k]!r} vs {rb[k]!r}')
        elif a != b:
            problems.append(f'{label}.{key}: {a!r} vs {b!r}')
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

    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix='parity_'))

    cases = [
        ('ready',   READY_BOQ,   'boq.csv'),
        ('revit',   REVIT_RAW,   'takeoff.csv'),
        ('scratch', SCRATCH_BOQ, 'contractor.csv'),
    ]

    # The IFC fixture is generated on demand so the suite carries no binary.
    ifc_path = tmp / 'sample.ifc'
    try:
        _make_sample_ifc(ifc_path)
        cases.append(('ifc', ifc_path.read_bytes(), 'model.ifc'))
    except Exception as exc:                       # pragma: no cover
        print(f'  ! IFC fixture skipped ({exc})')

    all_problems = []
    for label, data, filename in cases:
        path = _write(tmp, filename, data)
        legacy = _legacy(data, filename)
        new = _new(path)
        problems = _diff(label, legacy, new)
        all_problems += problems
        status = 'MATCH' if not problems else f'{len(problems)} DIFF'
        print(f'  {"+" if not problems else "x"} {label:8} '
              f'source={new["source_type"]:4} n={new["n_elements"]:3} '
              f'elements={len(new["elements"]):3}  -> {status}')

    if all_problems:
        print('\nDIFFERENCES:')
        for p in all_problems[:40]:
            print('   ', p)
        print(f'\nPARITY FAILED ({len(all_problems)} differences)')
        return 1

    print('\nPARITY PASSED - extracted service matches legacy exactly')
    return 0


def _make_sample_ifc(out: Path) -> None:
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


if __name__ == '__main__':
    raise SystemExit(main())
