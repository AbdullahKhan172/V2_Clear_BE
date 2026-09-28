"""
Vendor integrity: is the copied engine still the same engine?
=============================================================
webapp/backend/vendor/ holds a byte-for-byte copy of the calculation engine so
that webapp/ can be moved or deployed without the rest of the repository. A
second copy of numerical code is a drift risk, and drift here does not raise -
it returns a plausible wrong number. This is the check that makes the copy safe.

Three things are asserted:

  1. COMPLETE   every module the backend imports from the engine is present in
                the vendor tree, and the catalogue CSV is where catalogue.py
                will look for it (its own parents[2]/data, which is why the
                vendor mirrors the original layout instead of flattening it)
  2. IDENTICAL  while the original tree is still in the repository, every
                vendored file matches it byte for byte
  3. LIVE       core_bridge actually resolved to the vendor copy, and the
                engine imports and calculates from there

(2) reports "nothing to compare" rather than failing once the original tree is
gone - at that point the vendor copy simply IS the engine, and this degrades to
a completeness check.

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_vendor.py
"""

import hashlib
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

VENDOR = BACKEND / 'vendor'

# Every engine file the backend actually reaches for, as a vendor-relative path.
# Listed explicitly rather than globbed: the point is to notice when something
# new is imported and nobody copied it.
REQUIRED = [
    'src/core/calculations.py',
    'src/core/catalogue.py',
    'src/core/compliance.py',
    'src/core/engine.py',
    'src/core/stages.py',
    'src/core/steel_rates.py',
    'src/core/steel_sections.py',
    'src/core/utils.py',
    'src/ingest/boq_parser.py',
    'src/ingest/csv_processor.py',
    'src/ingest/ifc_processor.py',
    'templates/__init__.py',
    'templates/dashboard_styles.py',
    'templates/html_template_2.py',
    'reports/__init__.py',
    'reports/excel_report.py',
    'reports/html_report.py',
    'reports/word_report.py',
    'web_helpers.py',
    'data/A1_A5_Emission_Catalogue.csv',
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    problems: list[str] = []

    if not VENDOR.is_dir():
        print('  ! no vendor/ directory - the backend runs against the '
              'original tree. Nothing to verify.')
        return 0

    # ── 1. Completeness ─────────────────────────────────────────────────
    missing = [rel for rel in REQUIRED if not (VENDOR / rel).is_file()]
    for rel in missing:
        problems.append(f'vendor is missing {rel}')
    print(f'  {"+" if not missing else "x"} {len(REQUIRED) - len(missing)}/'
          f'{len(REQUIRED)} required engine files present')

    # catalogue.py resolves its CSV as parents[2]/data/... - prove that lands
    # inside the vendor tree rather than escaping back to the original.
    cat_py = VENDOR / 'src' / 'core' / 'catalogue.py'
    if cat_py.is_file():
        expected_csv = cat_py.resolve().parents[2] / 'data' / \
            'A1_A5_Emission_Catalogue.csv'
        if not expected_csv.is_file():
            problems.append(
                f'catalogue.py will look for its CSV at {expected_csv}, which '
                f'does not exist - the vendor layout has been flattened')
        elif VENDOR.resolve() not in expected_csv.parents:
            problems.append(
                f'catalogue.py resolves its CSV to {expected_csv}, OUTSIDE the '
                f'vendor tree - a moved copy would have no catalogue')
        else:
            print('  + catalogue.py resolves its CSV inside vendor/')

    # ── 2. Byte-identical to the original, while the original exists ────
    original_of = {
        'web_helpers.py': PROJECT / 'web_helpers.py',
        'data/A1_A5_Emission_Catalogue.csv':
            PROJECT / 'data' / 'A1_A5_Emission_Catalogue.csv',
    }
    for rel in REQUIRED:
        original_of.setdefault(rel, PROJECT / rel)

    compared = drifted = 0
    for rel in REQUIRED:
        v, o = VENDOR / rel, original_of[rel]
        if not v.is_file() or not o.is_file():
            continue
        compared += 1
        if _sha(v) != _sha(o):
            drifted += 1
            problems.append(
                f'{rel} DIFFERS from {o.relative_to(PROJECT)} - the engine has '
                f'been changed in one place and not the other')

    if compared:
        print(f'  {"+" if not drifted else "x"} {compared} files compared to '
              f'the original tree, {drifted} drifted')
    else:
        print('  - original tree not present; nothing to compare against')

    # ── 3. The bridge really resolved to the vendor, and it works ───────
    from app import core_bridge

    if not core_bridge.IS_VENDORED:
        problems.append(
            f'core_bridge resolved to {core_bridge.ENGINE_ROOT}, not the '
            f'vendor copy - the tests are not exercising what would ship')
    else:
        print(f'  + core_bridge resolved to vendor/')

    from catalogue import CATALOGUE_VERSION, EmissionCatalogue
    cat = EmissionCatalogue()
    n = len(cat.df)
    if n <= 0:
        problems.append('the vendored catalogue loaded zero rows')
    print(f'  + vendored engine loads: catalogue {CATALOGUE_VERSION}, '
          f'{n} factors')

    # Prove the vendored modules are the ones actually imported, not the
    # originals that happen to be earlier on sys.path.
    import engine as _engine
    if VENDOR.resolve() not in Path(_engine.__file__).resolve().parents:
        problems.append(
            f'`engine` imported from {_engine.__file__}, which is not in the '
            f'vendor tree')
    else:
        print('  + `engine` imported from the vendor tree')

    if problems:
        print('\nPROBLEMS:')
        for p in problems[:20]:
            print('   ', p)
        print(f'\nVENDOR CHECK FAILED ({len(problems)})')
        return 1

    print('\nVENDOR CHECK PASSED - the copied engine is complete, byte-identical '
          'to the original, and the one actually being imported')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
