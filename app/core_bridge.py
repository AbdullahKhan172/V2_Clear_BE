"""
Bridge to the calculation engine.
=================================
The engine uses flat imports (`from catalogue import ...`) and resolves its own
data file by walking up from its location. Rather than rewrite those imports -
which would mean touching ~9,400 lines of verified, test-covered numerical code
- this module puts the right folders on sys.path so every engine module resolves
unchanged.

WHERE THE ENGINE COMES FROM
---------------------------
Two locations are supported, checked in this order:

  1. webapp/backend/vendor/   a copy kept inside this project, so `webapp/` can
                              be moved or deployed on its own
  2. <project root>/          the original tree (src/, templates/, reports/,
                              data/, web_helpers.py), used when there is no
                              vendor directory

The vendor copy mirrors the original RELATIVE layout exactly - src/core,
src/ingest, templates/, reports/, data/, web_helpers.py - which is what lets the
files be copied byte-for-byte with no edits. `catalogue.py` finds its CSV via
`Path(__file__).parents[2] / 'data'`, so that layout is load-bearing, not
cosmetic: flattening it would break the catalogue lookup.

KEEPING THE COPY HONEST
-----------------------
A second copy of the engine is a drift risk, and drift in this particular code
does not raise - it returns a plausible wrong number. tests/test_vendor.py
asserts the vendored files are byte-identical to the originals whenever both are
present, so a change made to one and not the other fails loudly. Once the
original tree is retired, that check reports "nothing to compare" and the vendor
copy simply becomes the engine.

Nothing in either location is modified by this project; the legacy Flask app
keeps working against the original files, which is what lets the parity
harnesses diff the two implementations.
"""

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
# webapp/backend/app/core_bridge.py -> parents[3] is the project root. Deployed
# on its own the backend sits near the filesystem root (/app/app/...), where
# there is no parents[3] - and no original tree either, so it falls back to the
# backend itself; vendor/ is what gets used there anyway.
_parents = Path(__file__).resolve().parents
PROJECT_ROOT = _parents[3] if len(_parents) > 3 else BACKEND_ROOT

VENDOR_ROOT = BACKEND_ROOT / 'vendor'

# The vendored engine is preferred when it exists, so that in-repo runs exercise
# exactly the files a moved copy would use. Anything else would mean the thing
# that ships is not the thing that was tested.
ENGINE_ROOT = VENDOR_ROOT if (VENDOR_ROOT / 'src' / 'core').is_dir() \
    else PROJECT_ROOT
IS_VENDORED = ENGINE_ROOT == VENDOR_ROOT

SRC_CORE = ENGINE_ROOT / 'src' / 'core'
SRC_INGEST = ENGINE_ROOT / 'src' / 'ingest'

# ENGINE_ROOT carries web_helpers.py and the templates/ + reports/ packages;
# the two src folders carry the flat engine modules.
for _p in (ENGINE_ROOT, SRC_CORE, SRC_INGEST):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

__all__ = ['PROJECT_ROOT', 'BACKEND_ROOT', 'VENDOR_ROOT', 'ENGINE_ROOT',
           'IS_VENDORED', 'SRC_CORE', 'SRC_INGEST']
