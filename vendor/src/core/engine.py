"""
C.L.E.A.R. v2.0 - Orchestration Engine
========================================
Connects IFC/CSV processing, quantity review, carbon calculations,
and report generation into a unified pipeline.
"""

import os
import re
import pandas as pd
from datetime import datetime

from utils import encode_file_to_base64
from catalogue import EmissionCatalogue, DEFAULT_CONCRETE_EID
from steel_rates import get_steel_rate
from steel_sections import is_structural_steel_name
from calculations import (
    process_catalogue,
    calculate_emissions,
    create_material_summary,
    calculate_metrics,
    calculate_stage_emissions,
    calculate_material_stage_emissions,
    check_has_pt_slab,
    prepare_detailed_data,
    _resolve_a5a_factor,
    A5A_EMISSION_FACTOR_KGCO2E_PER_SQM,
)
from compliance import get_efficiency_rating, get_rating_row_index

# ---------------------------------------------------------------------------
# Taxonomy element map — derived from Structural_Elements_Carbon_Taxonomy.xlsx
# Maps lowercase name/keyword fragments → (taxonomy_id, category_label, ec_rate_available)
# ec_rate_available=False flags elements that have no EC rate yet in the catalogue.
# This enriches the Category field on BOQ rows and surfaces gaps in the report.
# ---------------------------------------------------------------------------
_TAXONOMY_ELEMENT_MAP = [
    # Foundation (E001–E024)
    ('strip footing',               'E001', 'Foundation/Footing',    True),
    ('pad footing',                 'E002', 'Foundation/Footing',    True),
    ('isolated footing',            'E002', 'Foundation/Footing',    True),
    ('combined footing',            'E003', 'Foundation/Footing',    True),
    ('raft foundation',             'E004', 'Foundation/Footing',    True),
    ('mat foundation',              'E004', 'Foundation/Footing',    True),
    ('piled raft',                  'E005', 'Foundation/Footing',    True),
    ('bored pile',                  'E006', 'Pile',                  True),
    ('cfa pile',                    'E007', 'Pile',                  True),
    ('driven precast pile',         'E008', 'Pile',                  True),
    ('steel h-pile',                'E009', 'Pile',                  True),
    ('steel tubular pile',          'E010', 'Pile',                  True),
    ('sheet pile',                  'E011', 'Foundation/Footing',    True),
    ('secant pile wall',            'E012', 'Foundation/Footing',    True),
    ('contiguous pile wall',        'E013', 'Foundation/Footing',    True),
    ('diaphragm wall',              'E014', 'Wall',                  True),
    ('micropile',                   'E015', 'Pile',                  True),
    ('mini pile',                   'E015', 'Pile',                  True),
    ('pile cap',                    'E016', 'Foundation/Footing',    True),
    ('ground beam',                 'E017', 'Foundation/Footing',    True),
    ('tie beam',                    'E017', 'Foundation/Footing',    True),
    ('underpinning',                'E018', 'Foundation/Footing',    True),
    ('blinding',                    'E019', 'Foundation/Footing',    True),
    ('basement wall',               'E020', 'Wall',                  True),
    ('retaining wall',              'E020', 'Wall',                  True),
    ('basement slab',               'E021', 'Foundation/Footing',    True),
    ('ground-bearing slab',         'E022', 'Foundation/Footing',    True),
    ('ground bearing slab',         'E022', 'Foundation/Footing',    True),
    ('gabion',                      'E023', 'Foundation/Footing',    False),
    ('masonry retaining wall',      'E024', 'Foundation/Footing',    False),
    ('stone retaining wall',        'E024', 'Foundation/Footing',    False),
    # Column (E025–E034)
    ('rc column',                   'E025', 'Column',                True),
    ('precast concrete column',     'E026', 'Column',                True),
    ('steel uc',                    'E027', 'Column',                True),
    ('universal column',            'E027', 'Column',                True),
    ('shs column',                  'E028', 'Column',                True),
    ('rhs column',                  'E028', 'Column',                True),
    ('hss column',                  'E028', 'Column',                True),
    ('chs column',                  'E029', 'Column',                True),
    ('circular hollow column',      'E029', 'Column',                True),
    ('composite column',            'E030', 'Column',                False),
    ('encased column',              'E030', 'Column',                False),
    ('glulam column',               'E031', 'Column',                False),
    ('timber column',               'E031', 'Column',                False),
    ('clt column',                  'E032', 'Column',                False),
    ('cfs column',                  'E033', 'Column',                False),
    ('light gauge column',          'E033', 'Column',                False),
    ('masonry column',              'E034', 'Column',                False),
    # Beam (E035–E058) — selective entries
    ('rc beam',                     'E035', 'Beam',                  True),
    ('precast beam',                'E036', 'Beam',                  True),
    ('steel ub',                    'E037', 'Beam',                  True),
    ('universal beam',              'E037', 'Beam',                  True),
    ('transfer beam',               'E038', 'Beam',                  True),
    ('transfer truss',              'E039', 'Beam',                  True),
    ('vierendeel',                  'E040', 'Beam',                  False),
    ('castellated beam',            'E041', 'Beam',                  True),
    ('cellular beam',               'E041', 'Beam',                  True),
    ('glulam beam',                 'E042', 'Beam',                  False),
    ('lvl beam',                    'E043', 'Beam',                  False),
    ('tcc beam',                    'E044', 'Beam',                  False),
    ('timber-concrete composite beam', 'E044', 'Beam',               False),
    ('outrigger beam',              'E045', 'Lateral System',        False),
    ('belt truss',                  'E045', 'Lateral System',        False),
    ('spandrel beam',               'E046', 'Beam',                  True),
    ('ring beam',                   'E047', 'Beam',                  True),
    ('coupling beam',               'E048', 'Beam',                  True),
    ('hip rafter',                  'E049', 'Roof',                  False),
    ('valley rafter',               'E049', 'Roof',                  False),
    ('mono-pitch beam',             'E050', 'Roof',                  False),
    ('lean-to beam',                'E050', 'Roof',                  False),
    # Slab (E059–E085) — selective entries
    ('flat slab',                   'E059', 'Slab/Floor',            True),
    ('pt slab',                     'E060', 'Slab/Floor',            True),
    ('post-tensioned slab',         'E060', 'Slab/Floor',            True),
    ('ribbed slab',                 'E061', 'Slab/Floor',            True),
    ('waffle slab',                 'E062', 'Slab/Floor',            True),
    ('hollowcore',                  'E063', 'Slab/Floor',            True),
    ('hollow core',                 'E063', 'Slab/Floor',            True),
    ('composite slab',              'E064', 'Slab/Floor',            True),
    ('metal deck',                  'E064', 'Slab/Floor',            True),
    ('transfer slab',               'E065', 'Slab/Floor',            True),
    ('transfer plate',              'E065', 'Slab/Floor',            True),
    ('bubble deck',                 'E066', 'Slab/Floor',            True),
    ('cobiax',                      'E066', 'Slab/Floor',            True),
    ('clt floor',                   'E067', 'Slab/Floor',            False),
    ('tcc floor',                   'E068', 'Slab/Floor',            False),
    # Wall (E086–E105) — selective entries
    ('shear wall',                  'E086', 'Wall',                  True),
    ('core wall',                   'E087', 'Wall',                  True),
    ('precast wall',                'E088', 'Wall',                  True),
    ('tilt-up',                     'E089', 'Wall',                  True),
    ('tiltup',                      'E089', 'Wall',                  True),
    ('clt wall',                    'E090', 'Wall',                  False),
    ('timber frame wall',           'E091', 'Wall',                  False),
    ('loadbearing masonry wall',    'E092', 'Wall',                  False),
    ('confined masonry',            'E093', 'Wall',                  False),
    # Stair (E106–E110)
    ('stair slab',                  'E106', 'Stair',                 True),
    ('precast stair',               'E107', 'Stair',                 True),
    ('steel stair',                 'E108', 'Stair',                 True),
    ('timber stair',                'E109', 'Stair',                 False),
    # Roof (E111–E134)
    ('roof slab',                   'E111', 'Roof',                  True),
    ('roof beam',                   'E112', 'Roof',                  True),
    ('space frame',                 'E113', 'Roof',                  True),
    ('geodesic',                    'E114', 'Roof',                  False),
    ('etfe',                        'E115', 'Roof',                  False),
    ('membrane roof',               'E115', 'Roof',                  False),
    # Lateral System (E135–E143)
    ('steel bracing',               'E135', 'Lateral System',        True),
    ('x brace',                     'E135', 'Lateral System',        True),
    ('k brace',                     'E135', 'Lateral System',        True),
    ('chevron brace',               'E135', 'Lateral System',        True),
    ('buckling restrained brace',   'E136', 'Lateral System',        False),
    ('brb',                         'E136', 'Lateral System',        False),
    ('moment connection',           'E137', 'Lateral System',        False),
    ('gusset plate',                'E137', 'Lateral System',        False),
    ('base isolator',               'E138', 'Lateral System',        False),
    ('seismic isolator',            'E138', 'Lateral System',        False),
    ('viscous damper',              'E139', 'Lateral System',        False),
    ('tuned mass damper',           'E140', 'Lateral System',        False),
    ('outrigger',                   'E141', 'Lateral System',        False),
    ('knee brace',                  'E142', 'Lateral System',        False),
    # Connection (E144–E148) — no EC rate currently
    ('shear stud',                  'E144', 'Connection',            False),
    ('anchor bolt',                 'E145', 'Connection',            False),
    ('base plate',                  'E146', 'Connection',            False),
    ('haunch',                      'E147', 'Connection',            False),
    # Reinforcement (E149–E154)
    ('rebar',                       'E149', 'Reinforcement',         True),
    ('reinforcing bar',             'E149', 'Reinforcement',         True),
    ('welded wire mesh',            'E150', 'Reinforcement',         True),
    ('fabric reinforcement',        'E150', 'Reinforcement',         True),
    ('post-tensioning tendon',      'E151', 'Reinforcement',         True),
    ('pt strand',                   'E151', 'Reinforcement',         True),
    ('frp bar',                     'E152', 'Reinforcement',         False),
    ('gfrp',                        'E152', 'Reinforcement',         False),
    ('steel fibre',                 'E153', 'Reinforcement',         False),
    ('uhpc',                        'E154', 'Reinforcement',         False),
    # External Structure (E155–E160)
    # These are STRUCTURAL concrete/steel members that carry embodied carbon —
    # priced by their material (concrete → grade factor + rebar; steel → section
    # factor). Only genuinely material-less / uncovered items stay False. This is
    # structural-scope carbon, so parapets, façade-support / edge members, crash
    # barriers and canopy/mast frames DO count; architectural glazing/cladding/
    # doors are handled elsewhere (not in this taxonomy) and are excluded.
    ('retaining wall (gravity)',    'E155', 'External Structure',    True),
    ('gravity retaining wall',      'E155', 'External Structure',    True),
    ('crash barrier',               'E156', 'External Structure',    True),
    ('parapet',                     'E156', 'External Structure',    True),
    ('parapet beam',                'E156', 'External Structure',    True),
    ('parapet upstand',             'E156', 'External Structure',    True),
    ('upstand',                     'E156', 'External Structure',    True),
    ('facade beam',                 'E156', 'External Structure',    True),
    ('façade beam',                 'E156', 'External Structure',    True),
    ('cladding support',            'E156', 'External Structure',    True),
    ('external stair',              'E157', 'External Structure',    True),
    ('fire escape',                 'E157', 'External Structure',    True),
    ('canopy frame',                'E158', 'External Structure',    True),
    ('canopy',                      'E158', 'External Structure',    True),
    ('mast',                        'E159', 'External Structure',    True),
    ('flagpole',                    'E159', 'External Structure',    True),
    ('thrust block',                'E160', 'External Structure',    True),
]


def _taxonomy_lookup(name: str, category: str) -> tuple:
    """
    Match an element name/category against the taxonomy map.
    Returns (taxonomy_id, refined_category, ec_rate_available) or (None, category, True).
    Uses the existing category if no taxonomy match is found.
    """
    text = f"{name} {category}".lower()
    for keyword, tid, cat_label, has_rate in _TAXONOMY_ELEMENT_MAP:
        if keyword in text:
            return tid, cat_label, has_rate
    return None, category, True


_INC_REINF_CATALOGUE = None

def _factor_includes_reinforcement(e_id: str) -> bool:
    """True when the catalogue flags this factor as already covering the
    reinforcement (precast composite / prestressed entries). Cached catalogue
    instance — called per BOQ row."""
    global _INC_REINF_CATALOGUE
    if _INC_REINF_CATALOGUE is None:
        try:
            _INC_REINF_CATALOGUE = EmissionCatalogue()
        except Exception:
            return False
    return _INC_REINF_CATALOGUE.factor_includes_reinforcement(e_id)


_DENSITY_CATALOGUE = None

def _factor_density(e_id: str, default: float = 500.0) -> float:
    """Catalogue density (kg/m³) for an e_id — used so timber elements are
    massed by their real density instead of concrete's 2400. Cached instance."""
    global _DENSITY_CATALOGUE
    if _DENSITY_CATALOGUE is None:
        try:
            _DENSITY_CATALOGUE = EmissionCatalogue()
        except Exception:
            return default
    ef = _DENSITY_CATALOGUE.get_emission_factor(e_id)
    if ef and ef.get('density', 0) > 0:
        return float(ef['density'])
    return default


# Timber keyword → default IGBC factor. Matched on WHOLE WORDS (\b…\b) so an
# element/family name that merely CONTAINS these letters is not mis-priced as
# timber — e.g. "Redwood Court Column", "Woodside Beam" or "Timberyard Slab"
# (all concrete) must NOT match. Order matters: specific products before generic.
_TIMBER_PATTERNS = [
    (re.compile(r'\b(clt|cross[\s-]?laminated)\b'),   'T_001'),  # CLT
    (re.compile(r'\b(glulam|glue[\s-]?laminated)\b'), 'T_002'),  # Glulam
    (re.compile(r'\b(lvl|laminated\s+veneer)\b'),     'T_006'),  # LVL
    (re.compile(r'\b(mass\s+timber|timber|softwood|hardwood|wood)\b'), 'T_003'),  # generic
]


def _timber_default_eid(name: str, category: str):
    """Catalogue timber factor for timber-named elements, so a CLT/glulam member
    extracted from an IFC auto-prices as timber instead of showing zero. The user
    can always override via the full-database picker. Returns None for non-timber.
    (IGBC timber e_ids — see catalogue.TIMBER_EIDS / CATALOGUE_NOTES.md.)"""
    text = f"{name} {category}".lower()
    for pat, eid in _TIMBER_PATTERNS:
        if pat.search(text):
            return eid
    return None


def _precast_default_eid(name: str, category: str):
    """Catalogue factor for precast elements that have a dedicated entry.
    Only the two confident routings are automatic; other precast items (walls,
    stairs, solid planks) have no matching factor and keep the in-situ default
    (the user can still assign one via the full-database picker).
    Both auto factors include the reinforcement (includes_reinforcement=Y), so
    the rate-based rebar/PT guard suppresses the extra steel automatically."""
    text = f"{name} {category}".lower()
    if 'hollowcore' in text or 'hollow core' in text or 'hollow-core' in text:
        return 'C_056'   # precast hollowcore flooring, prestressed (world avg steel)
    if (('precast' in text or 'pre-cast' in text)
            and ('beam' in text or 'column' in text)):
        return 'C_054'   # precast beams & columns, steel reinforced (world avg steel)
    return None


class ProjectDataModel:
    """Container for all project data and calculation results."""

    def __init__(self):
        # ── Project info ────────────────────────────────────────────────
        self.project_info = {}
        self.distances = {}
        self.project_area = 0
        self.report_date = datetime.now().strftime('%Y-%m-%d')

        # ── Raw extracted data ──────────────────────────────────────────
        self.raw_elements_df = None    # From IFC/CSV before user edits
        self.geometry_data = []        # 3D geometry for viewer
        self.steel_type = ''           # User-selected steel type name
        self.pt_type = ''              # User-selected PT type name

        # ── User-reviewed BOQ (after edits) ─────────────────────────────
        self.boq_df = None             # Final BOQ with e_id, Mass, etc.

        # ── Calculation results ─────────────────────────────────────────
        self.detailed_df = None
        self.summary_df = None
        self.metrics = {}
        self.efficiency_rating = ""
        self.rating_row_index = 0
        self.stage_emissions = {}
        self.material_stage_emissions = {}
        self.has_pt_slab = False
        self.detailed_data = []

        # ── Derived display data ────────────────────────────────────────
        self.material_types = []
        self.material_emissions = []
        self.emission_per_sqm_data = []

        # ── Model file (for reports) ────────────────────────────────────
        self.model_file = None
        self.model_type = "none"
        self.model_base64 = ""

        # ── Assumptions used (for report assumptions sections) ──────────
        # A5a site-activity factor actually applied this run (kgCO2e/m² GIA).
        # Set by run_calculations_from_boq so every report/appendix quotes the
        # value the numbers were built from, not the SEAI default.
        self.a5a_factor = A5A_EMISSION_FACTOR_KGCO2E_PER_SQM
        self.rebar_rates_used = []       # [{element, rate_kg_m3}]
        self.concrete_specs_used = []    # [{element, grade, ggbs_pct, a1_a3}]
        self.emission_factors_used = []  # [{material, name, a1_a3, density, data_type, is_custom}]
        self.sensitivity_results = {}
        self.comparison_runs = {}


def process_ifc(ifc_path, progress_callback=None, defer_viewer_meshes=False,
                return_processor=False):
    """
    Process an IFC file and return extracted quantities + geometry.

    Args:
        defer_viewer_meshes: if True, skip cosmetic 3D-viewer tessellation for
            elements whose volume comes from QTO metadata (quantities return
            immediately); call processor.build_viewer_meshes() afterwards to
            fill those meshes. Default False = unchanged full-geometry output.
        return_processor: if True, also return the IFCProcessor instance so the
            caller can invoke build_viewer_meshes() later.

    Returns:
        (elements_df, geometry_list) — or (elements_df, geometry_list, processor)
        when return_processor=True.
    """
    from ifc_processor import IFCProcessor
    processor = IFCProcessor(ifc_path, progress_callback)
    df, geom = processor.process(defer_viewer_meshes=defer_viewer_meshes)
    if return_processor:
        return df, geom, processor
    return df, geom


def process_csv(csv_path):
    """
    Process a CSV/Excel file and return structured quantities.

    Returns:
        tuple: (elements_df, format_type)
    """
    from csv_processor import process_file
    return process_file(csv_path)


def prepare_boq_from_ifc(elements_df, concrete_grade, ggbs_pct, steel_rates_dict,
                          waste_factors, catalogue, pt_rates_dict=None,
                          steel_eid='R_022', pt_eid='PT_032',
                          member_eids=None, section_eid='R_004'):
    """
    Transform IFC-extracted elements into a calculator-ready BOQ DataFrame.

    For each concrete element, creates up to 3 rows:
    1. Concrete row (with assigned grade/GGBS)
    2. Rebar row for concrete members (from rebar rate kg/m³)
    3. PT steel row (for slabs/beams if PT rate provided)

    Args:
        elements_df: DataFrame from IFC processor
        concrete_grade: str like '32/40' (default for all elements)
        ggbs_pct: int (0, 25, 50, 70) (default for all elements)
        steel_rates_dict: dict of {element_name: rebar_rate_kg_m3}
        waste_factors: dict {'Concrete': 1.05, 'Rebar': 1.05, 'Steel Section': 1.01, 'Post Tensioning': 1.015}
        catalogue: EmissionCatalogue instance
        pt_rates_dict: dict of {element_name: pt_rate_kg_m3} for post-tensioning
        steel_eid: default rebar emission factor e_id
        section_eid: default structural steel section emission factor e_id
        pt_eid: default PT emission factor e_id
        member_eids: dict of {element_name: e_id} for per-member concrete grade override

    Returns:
        DataFrame in BOQ format ready for calculate_emissions()
    """
    if pt_rates_dict is None:
        pt_rates_dict = {}
    if member_eids is None:
        member_eids = {}

    default_concrete_eid = catalogue.get_concrete_eid(concrete_grade, ggbs_pct)
    if default_concrete_eid is None:
        default_concrete_eid = DEFAULT_CONCRETE_EID  # Fallback defined in catalogue.py
        print(f"[engine] WARNING: Grade '{concrete_grade}' with {ggbs_pct}% GGBS has no "
              f"catalogue entry. Falling back to default {DEFAULT_CONCRETE_EID} (C32/40 0% GGBS).")

    # If rebars are explicitly modeled, avoid adding duplicate rate-based rebar
    # rows for matching concrete elements/categories.
    modeled_rebar_keys = set()
    modeled_rebar_names = set()
    modeled_rebar_categories = set()

    for _, src in elements_df.iterrows():
        if not bool(src.get('is_steel', False)):
            continue
        if bool(src.get('is_structural_steel', False)):
            continue

        ifc_type = str(src.get('ifc_type', '') or '').strip()
        txt = (
            f"{src.get('name', '')} {src.get('family', '')} "
            f"{src.get('material', '')} {src.get('Description', '')}"
        ).lower()
        is_modeled_rebar = (
            ifc_type in {'IfcReinforcingBar', 'IfcReinforcingMesh'}
            or any(k in txt for k in ('rebar', 'reinforc', 'mesh'))
        )
        if not is_modeled_rebar:
            continue

        sname = str(src.get('name', '') or '')
        scat = str(src.get('category', '') or '')
        sw = round(float(src.get('width', 0) or 0))
        sd = round(float(src.get('depth', 0) or 0))
        skey = f"{sname}_{sw}x{sd}"

        modeled_rebar_keys.add(skey)
        if sname:
            modeled_rebar_names.add(sname)
        if scat:
            modeled_rebar_categories.add(scat)

    rows = []
    for _, elem in elements_df.iterrows():
        name = elem.get('name', '')
        category = elem.get('category', '')
        family = elem.get('family', '')
        ifc_type = elem.get('ifc_type', '')
        try:
            volume = float(elem.get('volume_m3', 0) or 0)
        except (ValueError, TypeError):
            volume = 0.0
        is_steel = elem.get('is_steel', False)
        try:
            count = int(elem.get('count', 1) or 1)
        except (ValueError, TypeError):
            count = 1

        # Build key matching app.py: name + dimensions (width×depth).
        # NaN-safe: `x or 0` does NOT catch NaN (NaN is truthy), so round() would
        # raise "cannot convert float NaN to integer" for elements/manual rows with
        # no width/depth. Treat NaN/None/invalid as 0.
        def _dim(v):
            try:
                v = float(v)
                return round(v) if v == v else 0   # v == v is False only for NaN
            except (ValueError, TypeError):
                return 0
        ew = _dim(elem.get('width', 0))
        ed = _dim(elem.get('depth', 0))
        elem_key = f"{name}_{ew}x{ed}"

        if volume <= 0:
            continue

        # ── Taxonomy enrichment ───────────────────────────────────────────
        taxonomy_id, category, ec_rate_available = _taxonomy_lookup(name, category)

        if is_steel:
            # IfcTendon elements are PT steel — route them to the PT bucket
            is_tendon = str(ifc_type) == 'IfcTendon'

            # Manually-added rows carry the user's material choice in
            # 'material_hint' (web app Step 2). Honour it: a manual
            # "Post Tensioning" row must not fall through to the rebar bucket.
            # Rows without the field (all model-extracted rows) are unaffected.
            mat_hint = str(elem.get('material_hint', '') or '').strip().lower()
            if mat_hint in ('post tensioning', 'post-tensioning', 'pt'):
                is_tendon = True

            is_structural_steel = bool(elem.get('is_structural_steel', False))
            if mat_hint in ('steel section', 'section'):
                is_structural_steel = True
            if not is_structural_steel and not is_tendon:
                section_hint = f"{name} {category} {elem.get('family', '')} {elem.get('ifc_type', '')}"
                if is_structural_steel_name(section_hint):
                    is_structural_steel = True

            # Modeled steel elements are massed from volume × 7850.
            steel_mass = volume * 7850

            if is_tendon:
                e_id = pt_eid
                material_label = 'Post Tensioning'
                steel_waste = waste_factors.get('Post Tensioning', 1.015)
            elif is_structural_steel:
                e_id = section_eid
                material_label = 'Steel'
                steel_waste = waste_factors.get('Steel Section', 1.01)
            else:
                e_id = steel_eid
                material_label = 'Steel'
                steel_waste = waste_factors.get('Rebar', 1.05)

            rows.append({
                'e_id': e_id,
                'Category': category,
                'Material': material_label,
                'Description': name,
                'Family': family,
                'Type': ifc_type,
                'Volume(m3)': volume,
                'Correction Formula': '',
                'Waste': steel_waste,
                'Total Volume(m3)': volume,
                'Density(kg/m3)': 7850,
                'Mass(kg)': steel_mass,
                'Count': count,
                'is_structural_steel': is_structural_steel,
                'taxonomy_id': taxonomy_id or '',
                'ec_rate_available': ec_rate_available,
            })
        else:
            # ── 1. Concrete row ───────────────────────────────────────
            # Net mass only — waste accounted for in A5w, not inflated here
            # Lookup priority: cat|||name (most specific) → elem_key (name+dims) → name → default
            cat_name_key = f'{category}|||{name}'
            user_assigned = (cat_name_key in member_eids or elem_key in member_eids
                             or name in member_eids)
            concrete_eid = member_eids.get(cat_name_key,
                           member_eids.get(elem_key,
                           member_eids.get(name, default_concrete_eid)))
            # Hollowcore / precast beams & columns default to their PRECAST
            # catalogue factor instead of the in-situ grade — unless the user
            # assigned a factor themselves, which always wins.
            if not user_assigned:
                _auto_pc = _precast_default_eid(name, category)
                if _auto_pc:
                    concrete_eid = _auto_pc
                else:
                    # Timber-named element (CLT/glulam/timber) → auto-route to its
                    # IGBC timber factor instead of the concrete grade default.
                    _auto_tim = _timber_default_eid(name, category)
                    if _auto_tim:
                        concrete_eid = _auto_tim
            # Timber is a distinct material: priced by volume × its own density
            # (~500, not concrete's 2400), labelled 'Timber', and carries biogenic
            # sequestration. Detected from the resolved factor (auto or user-picked).
            is_timber = str(concrete_eid or '').startswith('T_')
            # A no-factor material (masonry/timber — ec_rate_available=False)
            # normally calculates as 0, but when the user explicitly assigned a
            # catalogue factor to it (full-database picker) OR it resolved to a
            # timber factor, honour that instead of forcing zero.
            row_ec_ok = ec_rate_available or user_assigned or is_timber
            if is_timber:
                mat_density = _factor_density(concrete_eid, 500.0)
                mat_label = 'Timber'
                mat_waste = waste_factors.get('Timber', waste_factors.get('Concrete', 1.05))
                mat_desc = name
            else:
                mat_density = 2400
                mat_label = 'Concrete'
                mat_waste = waste_factors.get('Concrete', 1.05)
                mat_desc = f"{name} - Concrete"
            elem_mass = volume * mat_density  # net mass

            rows.append({
                # No-factor materials (masonry/… — taxonomy flags
                # ec_rate_available=False and no timber/user factor) must NOT
                # fall back to the concrete factor: blank e_id + the flag make
                # them calculate as 0 and be reported as "no emission factor".
                'e_id': concrete_eid if row_ec_ok else '',
                'Category': category,
                'Material': mat_label,
                'Description': mat_desc,
                'Family': family,
                'Type': ifc_type,
                'Volume(m3)': volume,
                'Correction Formula': '',
                'Waste': mat_waste,
                'Total Volume(m3)': volume,
                'Density(kg/m3)': mat_density,
                'Mass(kg)': elem_mass,
                'Count': count,
                'is_structural_steel': False,
                'taxonomy_id': taxonomy_id or '',
                'ec_rate_available': row_ec_ok,
                'concrete_grade_hint': concrete_grade,
            })

            # Timber carries no rate-based rebar/PT (a CLT panel is not a
            # reinforced-concrete member).
            if is_timber:
                continue
            # Rate-based rebar/PT presumes a reinforced-concrete member — skip
            # both for no-factor materials (a masonry wall has no rebar rate).
            # Deliberately keyed on the TAXONOMY flag, not row_ec_ok: a masonry
            # wall the user priced with a blockwork factor still gets no rebar.
            if not ec_rate_available:
                continue

            # Composite factors (precast beams/columns, prestressed hollowcore —
            # includes_reinforcement=Y in the catalogue) already price the steel:
            # adding rate-based rebar/PT on top would double count it. Applies to
            # both auto-routed precast defaults and user-assigned factors.
            if _factor_includes_reinforcement(concrete_eid):
                continue

            # ── 2. Steel reinforcement row ────────────────────────────
            has_modeled_rebar = (
                elem_key in modeled_rebar_keys
                or name in modeled_rebar_names
                or category in modeled_rebar_categories
            )
            steel_rate = 0 if has_modeled_rebar else steel_rates_dict.get(
                elem_key,
                steel_rates_dict.get(name, get_steel_rate(category).get('default', 150))
            )
            steel_waste = waste_factors.get('Rebar', 1.05)  # SEAI Table 4: rebar 5%
            # steel_rate is kg of steel per m³ of concrete — net mass only
            steel_mass = volume * steel_rate

            if steel_mass > 0:
                rows.append({
                    'e_id': steel_eid,
                    'Category': category,
                    'Material': 'Steel',
                    'Description': f"{name} - Rebar ({steel_rate} kg/m³)",
                    'Family': family,
                    'Type': ifc_type,
                    'Volume(m3)': steel_mass / 7850,
                    'Correction Formula': '',
                    'Waste': steel_waste,
                    'Total Volume(m3)': steel_mass / 7850,
                    'Density(kg/m3)': 7850,
                    'Mass(kg)': steel_mass,
                    'Count': count,
                    'is_structural_steel': False,
                    'taxonomy_id': taxonomy_id or '',
                    'ec_rate_available': ec_rate_available,
                })

            # ── 3. PT steel row (slabs and beams only) ────────────────
            pt_rate = pt_rates_dict.get(elem_key,
                      pt_rates_dict.get(name, 0))
            cat_norm = str(category or '').strip().lower()
            pt_supported = any(k in cat_norm for k in ('slab', 'floor', 'beam', 'roof'))
            if pt_rate > 0 and pt_supported:
                pt_waste = waste_factors.get('Post Tensioning', 1.015)
                pt_mass = volume * pt_rate  # net mass only

                if pt_mass > 0:
                    rows.append({
                        'e_id': pt_eid,
                        'Category': category,
                        'Material': 'Post Tensioning',
                        'Description': f"{name} - PT Steel ({pt_rate} kg/m³)",
                        'Family': family,
                        'Type': ifc_type,
                        'Volume(m3)': pt_mass / 7850,
                        'Correction Formula': '',
                        'Waste': pt_waste,
                        'Total Volume(m3)': pt_mass / 7850,
                        'Density(kg/m3)': 7850,
                        'Mass(kg)': pt_mass,
                        'Count': count,
                        'is_structural_steel': False,
                        'taxonomy_id': taxonomy_id or '',
                        'ec_rate_available': ec_rate_available,
                    })

    return pd.DataFrame(rows)


def run_calculations_from_boq(boq_df, project_info, distances, project_area,
                               catalogue=None, model_file=None, a5w_waste_pcts=None,
                               a5a_factor=None):
    """
    Run the full calculation pipeline from a prepared BOQ DataFrame.

    Args:
        boq_df: DataFrame with e_id, Mass(kg), etc.
        project_info: dict with project details
        distances: dict with transport distances
        project_area: float GIA in m²
        catalogue: EmissionCatalogue instance (optional, will load default)
        model_file: optional path to 3D model file
        a5a_factor: optional site-activity override (kgCO2e/m² GIA); default 28.

    Returns:
        ProjectDataModel with all results
    """
    if catalogue is None:
        catalogue = EmissionCatalogue()

    data = ProjectDataModel()
    data.project_info = project_info
    data.distances = distances
    data.project_area = project_area
    data.boq_df = boq_df
    data.model_file = model_file
    # Resolve once and record it: reports, the methodology appendix and the
    # comparison snapshots all quote data.a5a_factor rather than re-deriving it.
    data.a5a_factor = _resolve_a5a_factor(a5a_factor)

    # Get catalogue DataFrame for calculations
    catalogue_df = catalogue.get_raw_dataframe()

    # Run calculations
    data.detailed_df = calculate_emissions(boq_df, catalogue_df, distances, project_area,
                                           a5w_waste_pcts, a5a_factor=a5a_factor)

    # Ensure numeric columns
    _convert_emission_columns(data.detailed_df)

    data.summary_df = create_material_summary(data.detailed_df)
    _convert_summary_columns(data.summary_df)

    data.metrics = calculate_metrics(data.summary_df, project_area, a5a_factor=a5a_factor)
    # Biogenic carbon stored (timber) — a separate indicator, never netted into
    # the A1-A5 total. Surfaced on the dashboard + reports as its own line.
    _seq_ton = 0.0
    if data.detailed_df is not None and 'Sequestration(kgCO2e)' in data.detailed_df.columns:
        _seq_ton = pd.to_numeric(
            data.detailed_df['Sequestration(kgCO2e)'], errors='coerce').fillna(0).sum() / 1000.0
    data.metrics['sequestration_ton'] = round(_seq_ton, 3)
    data.efficiency_rating = get_efficiency_rating(data.metrics['total_emission_per_sqm'])
    data.rating_row_index = get_rating_row_index(data.efficiency_rating)
    data.stage_emissions = calculate_stage_emissions(data.detailed_df)
    data.material_stage_emissions = calculate_material_stage_emissions(data.detailed_df)
    data.has_pt_slab = check_has_pt_slab(data.detailed_df)
    data.detailed_data = prepare_detailed_data(data.detailed_df)

    # Display data
    data.material_types = data.summary_df['Material Type'].tolist()
    data.material_emissions = [round(v, 3) for v in data.summary_df['Total Emission(tCO2e)'].tolist()]
    if project_area > 0:
        data.emission_per_sqm_data = [
            round(v, 3) for v in
            (data.summary_df['Total Emission(tCO2e)'] * 1000 / project_area).tolist()
        ]
    else:
        data.emission_per_sqm_data = [0.0] * len(data.summary_df)

    # Process 3D model file
    _process_model_file(data, model_file)

    return data


def run_sensitivity_analysis(boq_df, project_info, distances, project_area,
                             catalogue, a5w_waste_pcts=None, base_metrics=None,
                             a5a_factor=None):
    """
    Parametric decarbonisation deltas — re-runs the engine on the ACTUAL BOQ with
    targeted modifications (no fixed multipliers, zero black-boxing). Ported from
    the proven desktop implementation so both front-ends share one engine.

    Returns dict keyed by intervention:
      {'ggbs_50pct': {'delta': tCO2e_saved, 'n_rows': int, 'note': str}, ...}
    Keys consumed by the dashboard: slab_thickness_30pct, ggbs_50pct, ggbs_70pct,
    rebar_slab_15pct, rebar_beam_15pct, rebar_col_10pct, rebar_found_10pct,
    rebar_wall_10pct.
    """
    results = {}
    base_total = (base_metrics or {}).get('total_emission_ton', 0)
    if base_total <= 0 or project_area <= 0 or boq_df is None or boq_df.empty:
        return results

    def _run(df):
        try:
            r = run_calculations_from_boq(df, project_info, distances, project_area,
                                          catalogue, a5w_waste_pcts=a5w_waste_pcts,
                                          a5a_factor=a5a_factor)
            return r.metrics.get('total_emission_ton', base_total)
        except Exception:
            return base_total

    # Identify key columns
    vol_col = next((c for c in ['Total Volume(m3)', 'Volume(m3)', 'volume_m3']
                    if c in boq_df.columns), None)
    mat_col = next((c for c in ['Material', 'material_hint'] if c in boq_df.columns), None)
    cat_col = next((c for c in ['Category', 'category'] if c in boq_df.columns), None)
    desc_col = next((c for c in ['Description', 'name'] if c in boq_df.columns), None)

    # ── Helper: is this slab row reducible in thickness? ─────────────────
    # Hollowcore and PT slabs are precast/prestressed — thickness is NOT a design
    # variable. Only in-situ RC flat slabs and band beams can be reduced.
    def _is_reducible_slab(row):
        name_str = ' '.join([
            str(row.get(desc_col, '') if desc_col else ''),
            str(row.get(cat_col, '') if cat_col else ''),
        ]).lower()
        excluded = ['hollowcore', 'hollow core', 'hollow-core', 'hc slab',
                    'precast slab', 'post-tension', 'post tension', ' pt ', 'pt slab',
                    'prestress', 'pre-stress', 'filigree', 'omnia', 'composite deck',
                    'metal deck', 'profiled deck']
        return not any(kw in name_str for kw in excluded)

    # ── 1. Slab thickness reduction: 30% for reducible RC flat slabs ─────
    if vol_col and cat_col and mat_col:
        df2 = boq_df.copy()
        slab_mask = df2[cat_col].astype(str).str.lower().str.contains(r'slab|floor', regex=True)
        conc_mask = df2[mat_col].astype(str).str.lower().str.contains('concrete')
        reducible_mask = slab_mask & conc_mask & df2.apply(_is_reducible_slab, axis=1)
        n_reducible = reducible_mask.sum()
        if n_reducible > 0:
            df2.loc[reducible_mask, vol_col] = (
                pd.to_numeric(df2.loc[reducible_mask, vol_col], errors='coerce').fillna(0) * 0.70)
            if 'Mass(kg)' in df2.columns:
                df2.loc[reducible_mask, 'Mass(kg)'] = (
                    pd.to_numeric(df2.loc[reducible_mask, 'Mass(kg)'], errors='coerce').fillna(0) * 0.70)
            total2 = _run(df2)
            results['slab_thickness_30pct'] = {
                'delta': round(base_total - total2, 2),
                'n_rows': int(n_reducible),
                'note': 'RC flat slabs only — hollowcore/PT excluded (structurally fixed)',
            }

    # ── 2. GGBS 50% substitution — swap e_ids per actual grade ───────────
    if mat_col and 'e_id' in boq_df.columns and 'concrete_grade_hint' in boq_df.columns:
        from catalogue import GRADE_LABEL_MAP, get_ggbs_max
        df3 = boq_df.copy()
        conc_rows = df3[mat_col].astype(str).str.lower() == 'concrete'
        n_swapped = 0
        n_grade_limited = 0
        for idx in df3.index[conc_rows]:
            # GGBS substitution only applies to in-situ mixes. Precast/blockwork
            # rows carry a family-specific factor (e.g. hollowcore C_056) plus a
            # grade hint from the UI default — swapping them to an in-situ GGBS
            # e_id would silently re-price precast as in-situ concrete.
            _cur_eid = str(df3.at[idx, 'e_id'] or '')
            _fam = catalogue.factor_family(_cur_eid) if hasattr(catalogue, 'factor_family') else ''
            if _fam and _fam != 'in_situ':
                continue
            grade_raw = str(df3.at[idx, 'concrete_grade_hint'])
            grade_label = None
            for k, v in GRADE_LABEL_MAP.items():
                if v in grade_raw or k in grade_raw:
                    grade_label = v
                    break
            if grade_label:
                if get_ggbs_max(grade_label) < 50:
                    n_grade_limited += 1
                    continue
                eid_50 = catalogue.get_concrete_eid(grade_label, 50)
                if eid_50 and eid_50 != df3.at[idx, 'e_id']:
                    df3.at[idx, 'e_id'] = eid_50
                    n_swapped += 1
        if n_swapped > 0:
            total3 = _run(df3)
            limit_note = (f'; {n_grade_limited} row(s) excluded — grade limit (IS EN 206)'
                          if n_grade_limited > 0 else '')
            results['ggbs_50pct'] = {
                'delta': round(base_total - total3, 2),
                'n_rows': n_swapped,
                'note': f'Swapped {n_swapped} concrete rows to 50% GGBS (actual catalogue rates){limit_note}',
            }

    # ── 3. GGBS 70% (CEM III equivalent) — grade limits enforced ─────────
    if mat_col and 'e_id' in boq_df.columns and 'concrete_grade_hint' in boq_df.columns:
        from catalogue import GRADE_LABEL_MAP, get_ggbs_max
        df4 = boq_df.copy()
        conc_rows = df4[mat_col].astype(str).str.lower() == 'concrete'
        n_swapped4 = 0
        n_capped = 0      # rows capped at 50% instead of 70%
        n_excluded = 0    # rows excluded (grade max < 50%)
        for idx in df4.index[conc_rows]:
            grade_raw = str(df4.at[idx, 'concrete_grade_hint'])
            grade_label = None
            for k, v in GRADE_LABEL_MAP.items():
                if v in grade_raw or k in grade_raw:
                    grade_label = v
                    break
            if grade_label:
                max_ggbs = get_ggbs_max(grade_label)
                if max_ggbs == 0:
                    n_excluded += 1
                    continue
                target_ggbs = min(70, max_ggbs)
                if target_ggbs < 70:
                    n_capped += 1
                eid_target = catalogue.get_concrete_eid(grade_label, target_ggbs)
                if eid_target and eid_target != df4.at[idx, 'e_id']:
                    df4.at[idx, 'e_id'] = eid_target
                    n_swapped4 += 1
        if n_swapped4 > 0:
            total4 = _run(df4)
            cap_note = (f'; {n_capped} row(s) capped at 50% (C40/50, IS EN 206)'
                        if n_capped > 0 else '')
            excl_note = (f'; {n_excluded} row(s) excluded — no IGBC catalogue entry'
                         if n_excluded > 0 else '')
            results['ggbs_70pct'] = {
                'delta': round(base_total - total4, 2),
                'n_rows': n_swapped4,
                'n_capped': n_capped,
                'note': f'Swapped {n_swapped4} rows to max-GGBS variant per grade (CEM III equiv){cap_note}{excl_note}',
            }

    # ── 4. Rebar reduction by member type ────────────────────────────────
    if mat_col and cat_col and 'Mass(kg)' in boq_df.columns:
        sec_col = 'is_structural_steel'
        has_sec = sec_col in boq_df.columns
        rebar_base = boq_df[mat_col].astype(str).str.lower().isin(['steel', 'rebar'])
        if has_sec:
            rebar_base = rebar_base & ~boq_df[sec_col].astype(bool)

        MEMBER_REDUCTIONS = {
            'slab':       ('rebar_slab_15pct',   0.85, 'Slabs',    15, 'Layout optimisation / reduced grid'),
            'floor':      ('rebar_slab_15pct',   0.85, 'Slabs',    15, 'Layout optimisation / reduced grid'),
            'beam':       ('rebar_beam_15pct',   0.85, 'Beams',    15, 'Right-sizing / composite action'),
            'column':     ('rebar_col_10pct',    0.90, 'Columns',  10, 'Higher-strength concrete, smaller sections'),
            'foundation': ('rebar_found_10pct',  0.90, 'Foundations', 10, 'Optimised pile/cap layout'),
            'wall':       ('rebar_wall_10pct',   0.90, 'Walls',    10, 'Optimised shear wall reinforcement'),
        }
        computed = {}
        for kw, (key, factor, label, pct, note) in MEMBER_REDUCTIONS.items():
            if key in computed:
                continue
            df5 = boq_df.copy()
            member_mask = df5[cat_col].astype(str).str.lower().str.contains(kw)
            mask = rebar_base & member_mask
            n = mask.sum()
            if n > 0:
                df5.loc[mask, 'Mass(kg)'] = (
                    pd.to_numeric(df5.loc[mask, 'Mass(kg)'], errors='coerce').fillna(0) * factor)
                total5 = _run(df5)
                computed[key] = True
                results[key] = {
                    'delta': round(base_total - total5, 2),
                    'n_rows': int(n),
                    'member': label,
                    'reduction_pct': pct,
                    'note': note,
                }

    return results


# ── Helpers ─────────────────────────────────────────────────────────────────

def _convert_emission_columns(df):
    cols = ['A1-A3 Emission(kgCO2e)', 'A4 Emission(kgCO2e)',
            'A5 Emission(kgCO2e)', 'A1-A5 Emission(tCO2e)',
            'Total Emission(tCO2e)']
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)


def _convert_summary_columns(df):
    if 'Total Emission(tCO2e)' in df.columns:
        df['Total Emission(tCO2e)'] = pd.to_numeric(
            df['Total Emission(tCO2e)'], errors='coerce').fillna(0)


def _process_model_file(data, model_file):
    if model_file and os.path.exists(model_file):
        ext = os.path.splitext(model_file)[1].lower()
        if ext in ('.glb', '.gltf'):
            data.model_type = "glb"
            data.model_base64 = encode_file_to_base64(model_file)
        elif ext == '.ifc':
            data.model_type = "ifc"
        else:
            data.model_type = "none"
    else:
        data.model_type = "none"


