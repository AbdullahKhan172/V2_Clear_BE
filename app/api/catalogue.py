"""
Catalogue endpoint - the reference data Step 3's dropdowns are built from.
=========================================================================
Everything here is derived from data/A1_A5_Emission_Catalogue.csv and the
constants in src/core/catalogue.py. It does not depend on a run, changes only
when the catalogue CSV changes, and is safe to cache for a session.

The two factor lists come from web_helpers._steel_factor_catalog() and
._full_factor_catalog() - already free of Flask, already memoised, and already
the exact shape the legacy dropdowns consume. Reused, not reimplemented.
"""

from __future__ import annotations

from fastapi import APIRouter

from app import core_bridge  # noqa: F401  - puts src/core on sys.path

from calculations import (A5A_EMISSION_FACTOR_KGCO2E_PER_SQM,
                          A5A_MAX_KGCO2E_PER_SQM, A5A_MIN_KGCO2E_PER_SQM)
from catalogue import (CATALOGUE_VERSION, CONCRETE_GRADES, DEFAULT_PT_EID,
                       DEFAULT_STEEL_EID, DEFAULT_STEEL_SECTION_EID,
                       DEFAULT_WASTE_FACTORS, GGBS_MAX_BY_GRADE, GGBS_OPTIONS,
                       PT_TYPES, REBAR_TYPES, SEAI_TRANSPORT_TABLE,
                       STEEL_SECTION_TYPES)
from web_helpers import _full_factor_catalog, _steel_factor_catalog

# Materials that carry a site-waste allowance (SEAI Table 4). Kept in this order
# because it is the order the engine reads them in a5w_waste_pcts.
WASTE_MATERIALS = ('Concrete', 'Rebar', 'Steel Section', 'Post Tensioning')

router = APIRouter(prefix='/api', tags=['catalogue'])


@router.get('/catalogue')
async def get_catalogue():
    """Reference data for the Materials step.

    `full_factors` is every catalogue entry, grouped (Concrete - In-situ /
    Precast / Blocks, Steel - Structural / Other, Post Tensioning, Timber).
    `inc_reinf` on an entry means the factor ALREADY prices its reinforcement -
    the UI must then close the rebar and PT inputs, or that steel is counted
    twice.

    `ggbs_max_by_grade` carries the IS EN 206 limits: C40/50 tops out at 50%,
    and C30/37 has no GGBS variants in the IGBC catalogue at all. Without it the
    UI would offer mixes that silently fall back to 0% GGBS.
    """
    steel = _steel_factor_catalog()
    return {
        'catalogue_version': CATALOGUE_VERSION,
        'concrete_grades': CONCRETE_GRADES,
        'ggbs_options': GGBS_OPTIONS,
        'ggbs_max_by_grade': GGBS_MAX_BY_GRADE,
        # Named type dropdowns - the label the user picks resolves to an e_id
        # server-side, so the browser never has to know catalogue codes.
        'rebar_types': sorted(REBAR_TYPES),
        'section_types': sorted(STEEL_SECTION_TYPES),
        'pt_types': sorted(PT_TYPES),
        'defaults': {
            'concrete_grade': '32/40 MPa',
            'ggbs_pct': 0,
            'rebar_type': next((k for k, v in REBAR_TYPES.items()
                                if v == DEFAULT_STEEL_EID), ''),
            'section_type': next((k for k, v in STEEL_SECTION_TYPES.items()
                                  if v == DEFAULT_STEEL_SECTION_EID), ''),
            'pt_type': next((k for k, v in PT_TYPES.items()
                             if v == DEFAULT_PT_EID), ''),
        },
        # Grouped steel factors for the per-row dropdown, plus e_id -> name so a
        # factor carried in the uploaded file still renders with a real label.
        'steel_factors': steel['groups'],
        'steel_factor_names': steel['names'],
        'full_factors': _full_factor_catalog(),

        # ── Step 4 reference data ───────────────────────────────────────
        # All EIGHT SEAI Table 2 families, from the authoritative table in
        # catalogue.py. The legacy wizard only rendered six of them (blockwork
        # and sheet/coil steel were omitted), so those two silently took their
        # defaults with no way to adjust them. Serving the full table closes
        # that gap without changing any default.
        'transport_defaults': [
            {'key': key, 'label': v['label'], 'scenario': v['scenario'],
             'road': v['road'], 'sea': v['sea']}
            for key, v in SEAI_TRANSPORT_TABLE.items()
        ],
        # Waste as a PERCENT, which is what the user types. The catalogue stores
        # multipliers (1.05), so convert here rather than making the UI do it.
        'waste_defaults': {
            m: round((DEFAULT_WASTE_FACTORS.get(m, 1.0) - 1.0) * 100, 3)
            for m in WASTE_MATERIALS
        },
        'a5a': {
            'default': A5A_EMISSION_FACTOR_KGCO2E_PER_SQM,
            'min': A5A_MIN_KGCO2E_PER_SQM,
            'max': A5A_MAX_KGCO2E_PER_SQM,
        },
    }
