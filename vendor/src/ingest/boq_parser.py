"""
Universal BOQ Parser
====================
Accepts any BOQ format (contractor Excel, QS schedule, scratch spreadsheet, etc.)
and normalises it to the standard internal schema expected by csv_processor.py
and the downstream pipeline.

Three-step process:
  1. detect_boq_format()   — rule-based analysis of column names and data patterns
  2. map_columns_gemini()  — Gemini Flash AI maps unknown columns to standard schema
  3. normalise_boq()       — converts the mapped DataFrame into pipeline-ready format

Rebar duplicate detection:
  If the BOQ already contains explicit rebar/reinforcement rows for a concrete member,
  normalise_boq() flags that member so engine.prepare_boq_from_ifc() will NOT add a
  second rate-based rebar row (same logic already used in the IFC path).

Non-structural filtering:
  Elements whose description/category matches non-structural keywords are flagged as
  non_structural=True and excluded by default, but returned in a separate DataFrame so
  the user can review and optionally re-include them via the GUI.
"""

import re
import os
import json
import pandas as pd

from csv_processor import (
    _parse_revit_category,
    _is_steel_material,
    _is_structural_steel_from_text,
    REBAR_EIDS,
    STEEL_SECTION_EIDS,
)
from steel_sections import is_structural_steel_name

# ── Standard schema columns the pipeline expects ───────────────────────────
STANDARD_COLS = [
    'category',        # Structural category: Slab/Floor, Beam, Column, Wall, etc.
    'Category',        # Same — some paths use title-case
    'name',            # Element name / description
    'Description',     # Same — title-case alias
    'Material',        # 'Concrete', 'Steel', 'Post Tensioning'
    'volume_m3',       # Net volume in m³
    'Volume(m3)',      # Alias
    'Total Volume(m3)',
    'Density(kg/m3)',
    'Mass(kg)',
    'Count',
    'Waste',
    'e_id',            # Emission catalogue ID — may be blank for scratch BOQs
    'is_steel',        # bool
    'is_structural_steel',  # bool: True = section, False = rebar
    'has_modeled_rebar',    # bool: True = rebar row present, skip rate-based rebar
]

# ── Keywords that identify non-structural / architectural elements ──────────
# Covers UK/Ireland QS terminology, common abbreviations, and misspellings
_NON_STRUCTURAL_KW = [
    # Architectural finishes & linings
    'partition', 'parttion', 'partiton', 'block partition', 'demountable',
    'cladding', 'claddin', 'cladd', 'rainscreen', 'rain screen',
    'facade', 'fascia', 'facia', 'soffit', 'spandrel panel',
    'glazing', 'glazed', 'curtain wall', 'curtainwall', 'unitised',
    'render', 'rendering', 'plaster', 'plastering', 'skim', 'dot and dab',
    'dry lining', 'drylining', 'dry-lining', 'plasterboard', 'gypsum',
    'screed', 'screeding', 'floor screed', 'liquid screed', 'anhydrite',
    'tile', 'tiling', 'ceramic', 'porcelain', 'mosaic', 'stone finish',
    'paint', 'painting', 'coating', 'decoration', 'emulsion', 'gloss',
    'insulation', 'insulaton', 'thermal', 'rockwool', 'kingspan', 'celotex',
    'pir board', 'eps board', 'xps board', 'mineral wool',
    'ceiling', 'suspended ceiling', 'dropped ceiling', 'ceiling tile',
    'raised floor', 'raised access', 'access floor', 'pedestal',
    'carpet', 'carpeting', 'vinyl', 'linoleum', 'lino', 'flooring finish',
    'door', 'doorset', 'doorframe', 'door frame', 'door leaf', 'fd30', 'fd60',
    'window', 'windowsill', 'window sill', 'window board',
    'shutter', 'blind', 'louvre', 'louver', 'solar shading',
    'ironmongery', 'ironmonger', 'hardware', 'ironwork',
    # MEP — mechanical electrical plumbing
    'duct', 'ductwork', 'air handling', 'ahu', 'fcu', 'fan coil',
    'pipe', 'pipework', 'piping', 'pipeing', 'pipe run',
    'plumbing', 'plumbng', 'sanitary ware', 'wc', 'basin', 'sink',
    'hvac', 'h.v.a.c', 'mechanical', 'mep', 'm&e',
    'electrical', 'electrcal', 'cabling', 'cable management',
    'conduit', 'trunking', 'cable tray', 'containment',
    'lighting', 'luminaire', 'light fitting', 'emergency lighting',
    'fixture', 'fitting', 'sprinkler', 'fire suppression',
    'drainage', 'drain', 'sewer', 'foul water', 'surface water',
    'sanitary', 'soil pipe', 'waste pipe', 'rainwater pipe', 'rwp',
    'lift', 'elevator', 'escalator', 'moving walkway', 'hoist',
    'boiler', 'chiller', 'pump', 'tank', 'cistern', 'water heater',
    'fire alarm', 'smoke detector', 'suppression',
    # Furniture / fit-out / FF&E
    'furniture', 'cabinet', 'joinery', 'worktop', 'shelving',
    'signage', 'sign', 'wayfinding', 'manifestation',
    'reception desk', 'kitchen unit', 'vanity unit',
    # Landscaping / external works / civils
    'landscape', 'landscaping', 'soft landscape', 'hard landscape',
    'paving', 'pavng', 'block paving', 'tarmac', 'tarmacadam',
    'kerb', 'kerbing', 'edging', 'channel',
    'road', 'roadway', 'carriageway', 'car park', 'carpark',
    'pavement', 'footpath', 'path', 'walkway',
    'tree', 'planting', 'topsoil', 'turf', 'seeding',
    'fill', 'hardcore', 'sub-base', 'granular fill', 'mot type 1',
    'fencing', 'fence', 'gate', 'hoarding',
    # Clearly non-load-bearing labels
    'non-structural', 'non structural', 'nonstructural',
    'architectural', 'arch element', 'decorative', 'feature',
    'feature wall', 'stud wall', 'metal stud', 'stud partition',
    'lightweight wall', 'block wall',  # block wall is non-structural unless noted
    'acoustic', 'acoustic panel', 'acoustic wall',
    # Substructure exclusions (earthworks only — not concrete)
    'excavation', 'excavate', 'excavtn', 'excav',
    'backfill', 'back fill', 'disposal', 'spoil', 'muck away',
    'earthwork', 'earthworks', 'cut and fill',
    'blinding', 'blnding',  # lean mix blinding concrete — minor, not structural
    'lean mix', 'lean concrete', 'mass concrete blinding',
    # Temporary works
    'formwork', 'shuttering', 'falsework', 'propping', 'prop',
    'temporary', 'temp works',
    # Preliminaries / general items
    'preliminaries', 'prelims', 'preamble', 'general items',
    'site setup', 'mobilisation', 'demobilisation', 'site clearance',
    'scaffold', 'scaffolding', 'access equipment',
    'testing', 'inspection', 'survey', 'setting out',
]

# ── Keywords that strongly confirm structural identity ─────────────────────
# Covers UK/Ireland terminology including precast, PT, and common abbreviations
_STRUCTURAL_KW = [
    # Slabs / floors
    'slab', 'slb', 'rc slab', 'r.c. slab', 'flat slab', 'two way slab',
    'one way slab', 'solid slab', 'ribbed slab', 'rib slab', 'waffle slab',
    'waffle', 'coffered', 'bubble deck', 'void slab', 'voided slab',
    'hollowcore', 'hollow core', 'hc slab', 'precast slab', 'composite slab',
    'metal deck slab', 'ground floor slab', 'gf slab', 'basement slab',
    'transfer slab', 'transfer plate', 'podium slab', 'post-tensioned slab',
    'pt slab', 'p.t. slab',
    # Floors (generic)
    'floor', 'flr', 'fl ',
    # Beams
    'beam', 'bm', 'rc beam', 'r.c. beam', 'transfer beam', 'grade beam',
    'ground beam', 'ring beam', 'tie beam', 'spandrel beam', 'lintel',
    'coupling beam', 'secondary beam', 'primary beam', 'upstand beam',
    'downstand beam', 'universal beam', 'ub ', ' ub', 'ub-', 'steel beam',
    # Columns
    'column', 'col ', 'col.', 'rc col', 'r.c. col', 'circular column',
    'square column', 'rectangular column', 'composite column', 'steel column',
    'universal column', 'uc ', ' uc', 'uc-',
    # Walls
    'shear wall', 'core wall', 'rc wall', 'r.c. wall', 'retaining wall',
    'basement wall', 'structural wall', 'str wall', 'diaphragm wall',
    'secant wall', 'contiguous wall', 'reinforced wall', 'precast wall',
    # Foundations
    'footing', 'foundation', 'fdn', 'ftg', 'raft', 'raft slab',
    'pad footing', 'strip footing', 'pile cap', 'pilecap', 'ground beam',
    'combined footing', 'isolated footing', 'mat foundation',
    # Piles
    'pile', 'bored pile', 'cfa pile', 'driven pile', 'precast pile',
    'spun pile', 'micropile', 'mini pile', 'helical pile', 'screw pile',
    'caisson', 'drilled shaft', 'secant pile', 'contiguous pile',
    # Stairs / ramps
    'stair', 'staircase', 'stair slab', 'stair waist', 'flight',
    'ramp', 'car ramp', 'sloped slab',
    # Roof structure
    'roof', 'roof slab', 'roof beam', 'terrace slab',
    'geodesic', 'space frame roof', 'etfe', 'membrane roof',
    'hip rafter', 'valley rafter', 'mono-pitch', 'lean-to roof',
    # Lateral system (taxonomy E135–E143)
    'bracing', 'brace', 'x brace', 'k brace', 'chevron brace',
    'buckling restrained brace', 'brb',
    'moment connection', 'gusset plate',
    'base isolator', 'seismic isolator',
    'viscous damper', 'tuned mass damper', 'tmd',
    'outrigger', 'belt truss', 'knee brace',
    # External structure (taxonomy E155–E160)
    'crash barrier', 'parapet wall', 'canopy frame', 'canopy beam',
    'external stair', 'fire escape', 'mast structure', 'flagpole',
    'thrust block', 'gravity retaining wall',
    # Timber & mass timber (taxonomy F11–F14)
    'clt', 'cross laminated timber', 'glulam', 'lvl beam',
    'timber-concrete composite', 'tcc slab', 'tcc beam',
    'mass timber', 'timber frame', 'light timber',
    # Masonry (taxonomy F15–F16)
    'loadbearing masonry', 'confined masonry', 'reinforced masonry',
    # Cold-formed / modular (taxonomy F17–F18)
    'cold-formed steel', 'cfs frame', 'light gauge frame',
    'modular unit', 'volumetric module',
    # Steel & rebar
    'rebar', 're-bar', 'reinforc', 'reinforcement', 'r.c.', 'r/c',
    'structural steel', 'steel section', 'steel frame',
    'hollow section', 'chs', 'shs', 'rhs', 'pfc', 'ubp',
    'post tension', 'post-tension', 'post tensioning', 'tendon', 'strand',
    'pt ', 'p.t.', 'pt-',
]

# ── Unit detection & conversion ─────────────────────────────────────────────
# BOQs express quantities in many units and frequently put the unit in a
# SEPARATE column ("Unit"/"UOM"/"Measure") or INSIDE the value cell ("84 m3",
# "9.25 tonnes"), and mix units down one "Qty" column (concrete m³, rebar
# tonne, steel kg). These helpers canonicalise the unit, split a value+unit
# cell, and route each quantity to volume (m³) vs mass (kg) — converting
# tonne/gram → kg, the pipeline's required steel/rebar unit. Concrete stays m³.
_UNIT_ALIASES = {
    'm3':    {'m3', 'cum', 'cu.m', 'cubicm', 'cubicmetre', 'cubicmeter', 'cbm', 'm^3', 'cu.m.'},
    'm2':    {'m2', 'sqm', 'sq.m', 'squaremetre', 'squaremeter', 'm^2', 'sm'},
    'kg':    {'kg', 'kgs', 'kilogram', 'kilograms', 'kilo'},
    't':     {'t', 'te', 'ton', 'tons', 'tonne', 'tonnes', 'mt', 'metricton', 'metrictonne'},
    'g':     {'g', 'gram', 'grams', 'gm'},
    'm':     {'m', 'lm', 'rm', 'linm', 'linearmetre', 'linearmeter', 'metre', 'meter', 'metres', 'meters', 'ml'},
    'count': {'no', 'nr', 'nos', 'pcs', 'pc', 'ea', 'each', 'number', 'item', 'items', 'unit', 'units', 'off'},
}


def _canonical_unit(token):
    """Map a raw unit token to one of m3/m2/kg/t/g/m/count, or None."""
    if token is None:
        return None
    t = str(token).strip().lower()
    if not t:
        return None
    t = t.replace('³', '3').replace('²', '2').replace(' ', '').rstrip('.')
    for canon, aliases in _UNIT_ALIASES.items():
        if t in {a.replace(' ', '').rstrip('.') for a in aliases}:
            return canon
    return None


# number (no spaces) then an optional unit that starts with a letter but may
# contain digits — so "m3"/"m2" are captured whole (not split at the digit).
_QTY_UNIT_RE = re.compile(r'^\s*(-?\d[\d,\.]*)\s*([A-Za-z][A-Za-z0-9²³\.\^/]*)?\s*$')


def _split_qty_unit(value):
    """Split a quantity cell into (number, canonical_unit).
    '84 m3'->(84.0,'m3'); '9.25 tonnes'->(9.25,'t'); '46.5'->(46.5,None)."""
    if value is None:
        return None, None
    s = str(value).strip()
    if not s or s.lower() in ('nan', 'none'):
        return None, None
    m = _QTY_UNIT_RE.match(s)
    if not m:
        try:
            return float(s.replace(',', '')), None
        except ValueError:
            return None, None
    try:
        val = float(m.group(1).replace(',', '').replace(' ', ''))
    except ValueError:
        return None, None
    unit = _canonical_unit(m.group(2)) if m.group(2) else None
    return val, unit


def _qty_to_vol_mass(value, unit, is_steel_like):
    """Route a (value, unit) quantity to (volume_m3, mass_kg); the unused one is
    0.0 and is back-filled later from density. tonne→kg (×1000), gram→kg (÷1000).
    When no unit is given, fall back to the material's natural unit: kg for
    steel/rebar, m³ for concrete. Area/length/count are not direct quantities."""
    if value is None or value <= 0:
        return 0.0, 0.0
    if unit == 'm3':
        return float(value), 0.0
    if unit == 't':
        return 0.0, float(value) * 1000.0
    if unit == 'kg':
        return 0.0, float(value)
    if unit == 'g':
        return 0.0, float(value) / 1000.0
    if unit is None:
        return (0.0, float(value)) if is_steel_like else (float(value), 0.0)
    return 0.0, 0.0  # m2 / m / count → not a direct structural volume or mass


# Description tokens that mark a row as a subtotal / carried-total / repeated
# header / collection line — never a real material quantity. Matched against the
# row's description so summary rows don't double-count or pollute the takeoff.
_JUNK_ROW_KW = (
    'subtotal', 'sub-total', 'sub total', 'carried forward', 'carried to',
    'carry forward', 'brought forward', 'grand total', 'page total',
    'to collection', 'collection', 'total concrete', 'total reinforcement',
    'total steel', 'total structural steel', 'total rebar',
)
# Worksheet names that are not BOQ data (skipped during sheet selection).
_SHEET_SKIP_KW = (
    'readme', 'read me', 'cover', 'note', 'preamble', 'rate', 'summary',
    'blank', 'content', 'index', 'legend', 'reference', 'revision',
    'glossary', 'mapping', 'expected', 'do_not', 'do not', 'instruction',
    'guide', 'title', 'preliminar',
)


# ── Gemini API ─────────────────────────────────────────────────────────────
# Backend-only call. v1 (stable) is used; JSON mode is forced via
# generationConfig.responseMimeType so the model can't return prose/markdown.
# Switch to v1beta only if a needed feature is unavailable on v1.
GEMINI_MODEL = "gemini-1.5-flash"
GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# Every role the LLM is allowed to return. Any value outside this set is
# rejected by code (the column stays unresolved for manual review) — the LLM
# never gets to invent a role, and never supplies a quantity.
_VALID_ROLES = {
    'name', 'category', 'material_hint', 'volume_m3', 'Total Volume(m3)',
    'Mass(kg)', 'Density(kg/m3)', 'Count', 'Waste', 'rebar_rate_kg_m3',
    'concrete_grade_hint', 'ggbs_hint', 'e_id', 'length_m', 'width_mm',
    'depth_mm', 'level', 'quantity', 'unit', 'IGNORE',
}

# ── Fuzzy header → role (runs AFTER rules, BEFORE the LLM) ───────────────────
# Catches typos/variants the keyword rules miss (e.g. "discription", "volme")
# without an API call, cutting both latency and LLM cost. Uses stdlib difflib
# only — no extra dependency. High cutoff keeps it conservative.
_FUZZY_ANCHORS = {
    'description': 'name', 'item description': 'name', 'element': 'name',
    'member': 'name', 'work description': 'name', 'particulars': 'name',
    'category': 'category', 'element type': 'category', 'group': 'category',
    'material': 'material_hint', 'material type': 'material_hint',
    'volume': 'volume_m3', 'net volume': 'volume_m3', 'concrete volume': 'volume_m3',
    'total volume': 'Total Volume(m3)', 'gross volume': 'Total Volume(m3)',
    'mass': 'Mass(kg)', 'weight': 'Mass(kg)', 'steel weight': 'Mass(kg)',
    'density': 'Density(kg/m3)',
    'count': 'Count', 'number': 'Count', 'quantity nr': 'Count',
    'quantity': 'quantity', 'qty': 'quantity', 'take-off amount': 'quantity',
    'measured quantity': 'quantity', 'takeoff': 'quantity',
    'unit': 'unit', 'uom': 'unit', 'measure': 'unit',
    'waste': 'Waste',
    'rebar rate': 'rebar_rate_kg_m3', 'steel ratio': 'rebar_rate_kg_m3',
    'grade': 'concrete_grade_hint', 'strength': 'concrete_grade_hint',
    'ggbs': 'ggbs_hint',
    'level': 'level', 'storey': 'level', 'floor level': 'level',
    'length': 'length_m', 'width': 'width_mm', 'depth': 'depth_mm',
    'thickness': 'depth_mm', 'height': 'depth_mm',
    'e_id': 'e_id', 'emission id': 'e_id',
}


def _fuzzy_map_column(cl: str, cutoff: float = 0.86):
    """Best-effort fuzzy match of a header to a role; None if not confident."""
    import difflib
    if not cl:
        return None
    match = difflib.get_close_matches(cl.strip(), _FUZZY_ANCHORS.keys(),
                                      n=1, cutoff=cutoff)
    return _FUZZY_ANCHORS[match[0]] if match else None


# =============================================================================
#  STEP 1 — FORMAT DETECTION
# =============================================================================

class BOQFormat:
    """Result of format detection."""
    READY       = 'ready'        # Already in pipeline standard format
    REVIT_RAW   = 'revit_raw'    # Revit material takeoff
    SCRATCH     = 'scratch'      # Contractor / QS scratch BOQ — needs AI mapping
    UNKNOWN     = 'unknown'

    def __init__(self, fmt, confidence, notes, column_roles):
        self.fmt = fmt
        self.confidence = confidence   # 0.0–1.0
        self.notes = notes             # human-readable description
        self.column_roles = column_roles  # {original_col: standard_role or None}


def detect_boq_format(df: pd.DataFrame) -> BOQFormat:
    """
    Analyse the DataFrame's columns and first rows to determine its format.

    Returns a BOQFormat object describing the detected format, confidence,
    and a preliminary column-role mapping (may be incomplete for scratch BOQs).
    """
    cols_lower = {c: c.lower().strip() for c in df.columns}
    roles = {}

    # Score how many standard columns can be matched by rule
    matched = 0

    for orig, cl in cols_lower.items():
        role = _rule_map_column(cl) or _fuzzy_map_column(cl)
        roles[orig] = role
        if role is not None:
            matched += 1

    coverage = matched / max(len(df.columns), 1)

    # Check for pipeline-ready markers
    has_eid   = any('e_id' in cl for cl in cols_lower.values())
    has_mass  = any('mass' in cl or 'weight' in cl for cl in cols_lower.values())
    has_vol   = any('vol' in cl for cl in cols_lower.values())
    has_revit = any(
        ('family' in cl and 'type' in cl) or 'material: name' in cl or 'material: volume' in cl
        for cl in cols_lower.values()
    )

    if has_eid and (has_mass or has_vol) and coverage >= 0.6:
        return BOQFormat(BOQFormat.READY, 0.95,
                         "Standard ready-format BOQ — e_id and volume/mass present.",
                         roles)

    if has_revit:
        return BOQFormat(BOQFormat.REVIT_RAW, 0.90,
                         "Revit Material Takeoff detected — Family+Type and material columns found.",
                         roles)

    if has_vol and coverage >= 0.4:
        return BOQFormat(BOQFormat.SCRATCH, 0.70,
                         f"Contractor/QS BOQ detected — "
                         f"{matched} of {len(df.columns)} column(s) auto-mapped by rules, "
                         f"remaining column(s) will be mapped by AI.",
                         roles)

    return BOQFormat(BOQFormat.UNKNOWN, 0.40,
                     "Column layout not recognised — AI will attempt to map all columns. "
                     "Please review the mapping table carefully before confirming.",
                     roles)


def _rule_map_column(cl: str):
    """
    Rule-based: map a lower-case column name to a standard role string.
    Covers UK/Ireland QS terminology, Revit exports, NBS, CostX, Causeway,
    and common abbreviations / misspellings used by drafters and contractors.
    Returns None if no confident match found.
    """
    # ── Measured-quantity column (value carries / needs a unit) ───────────
    # Checked FIRST so "Take-off amount" / "Measured Quantity" are not mistaken
    # for a cost "amount" by the IGNORE filter below. Bare "Qty"/"Quantity"
    # also land here; normalise_boq decides per row whether it is a measured
    # volume/mass (a unit is present) or a plain element count (no unit).
    if (('measured' in cl and ('quantity' in cl or 'qty' in cl))
            or any(k in cl for k in (
                'take-off amount', 'take off amount', 'takeoff amount',
                'take-off', 'takeoff', 'taking off', 'quantum',
                'qty value', 'qtyvalue', 'net qty', 'nett qty', 'total qty'))
            or cl in ('qty', 'quantity', 'quantities', 'qty.', 'measured')):
        return 'quantity'

    # ── Unit-of-measure column (Unit / UOM / Measure / U/M) ───────────────
    # Distinct from a cost "unit rate" and from "units" (an element count).
    if ((('unit' in cl) or ('uom' in cl) or ('measure' in cl) or cl in ('u/m', 'um'))
            and not any(k in cl for k in ('rate', 'cost', 'price', '£', '€', '$',
                                          'weight', 'mass', 'amount', 'density'))
            and cl.strip() not in ('units', 'no of units', 'number of units',
                                   'no. of units', 'no of unit')):
        return 'unit'

    # ── Always IGNORE: cost, price, rate £/unit, notes, refs ──────────────
    _cost_kw = ('cost', 'price', 'rate', '£', '€', '$', 'usd', 'eur', 'gbp',
                'total cost', 'amount', 'labour', 'labor', 'prelim',
                'note', 'notes', 'remark', 'comment', 'ref', 'reference',
                'drawing', 'drg', 'dwg', 'spec', 'clause', 'nbs',
                'provisional', 'p.c.', 'p.s.', 'prime cost', 'daywork',
                'contingency', 'allowance', 'adjustment')
    if any(k in cl for k in _cost_kw):
        return None  # Caller keeps as IGNORE

    # ── Area columns: m² is NOT volume — ignore unless it's a slab thickness calc ─
    _area_only_kw = ('area (m2)', 'area(m2)', 'floor area', 'plan area',
                     'surface area', 'area m2', 'gfa', 'gia', 'nia')
    if any(k == cl or k in cl for k in _area_only_kw):
        return None

    # ── Emission catalogue ID ──────────────────────────────────────────────
    if cl in ('e_id', 'eid', 'emission id', 'emission_id', 'emission factor id',
              'factor id', 'factor_id', 'ec id', 'carbon id'):
        return 'e_id'

    # ── Element name / description ─────────────────────────────────────────
    # UK QS: "Description", "Item Description", "Element", "Work Item",
    # "Member", "Mark", "Type Mark", "Element Mark", "Ref", "Member Ref"
    _name_kw = ('description', 'desc', 'item description', 'item desc',
                'element description', 'work item', 'work section',
                'element', 'member', 'member description', 'member type',
                'structural element', 'structural member',
                'item', 'name', 'type', 'type description',
                'mark', 'member mark', 'element mark', 'type mark',
                'component', 'part', 'detail', 'title',
                # Revit family/type naming
                'family', 'family and type', 'family type',
                # Common misspellings
                'desciption', 'descirption', 'descption', 'descrption',
                'elment', 'elemnt', 'elemen')
    if any(k in cl for k in _name_kw):
        return 'name'

    # ── Structural category ────────────────────────────────────────────────
    _cat_kw = ('categor', 'category', 'cat', 'group', 'element type',
               'element group', 'structural type', 'member type',
               'trade', 'work type', 'section', 'division')
    if any(k in cl for k in _cat_kw):
        return 'category'

    # ── Material ──────────────────────────────────────────────────────────
    # Exclude if combined with 'volume' (e.g. "Material Volume" in Revit)
    if 'material' in cl and 'vol' not in cl and 'area' not in cl:
        return 'material_hint'
    _mat_kw = ('concrete grade', 'concrete type', 'mix design', 'mix type',
               'steel type', 'steel grade', 'bar type', 'section type')
    if any(k in cl for k in _mat_kw):
        return 'material_hint'

    # ── Volume (m³) ───────────────────────────────────────────────────────
    # Total/gross volume takes priority over net volume
    _tvol_kw = ('total vol', 'total volume', 'gross vol', 'gross volume',
                'net vol', 'net volume', 'nett vol', 'nett volume',
                'adjusted vol', 'concrete vol', 'concrete volume',
                'vol (m3)', 'vol(m3)', 'volume (m3)', 'volume(m3)',
                'volume m3', 'vol m3', 'm3 (total)', 'total m3',
                'conc. vol', 'conc vol')
    if any(k in cl for k in _tvol_kw):
        return 'volume_m3'

    # Generic volume — catch all remaining vol columns
    _vol_kw = ('vol', 'volume', 'cubic', 'm3', 'cu.m', 'cu m', 'cum',
               'concrete quantity', 'conc qty', 'conc. qty')
    if any(k in cl for k in _vol_kw):
        return 'volume_m3'

    # ── Mass / weight ──────────────────────────────────────────────────────
    _mass_kw = ('mass', 'weight', 'wt', 'wt.', 'steel weight', 'rebar weight',
                'steel mass', 'rebar mass', 'kg', 'tonnes', 'tonne',
                'mass (kg)', 'mass(kg)', 'weight (kg)', 'weight(kg)',
                'steel kg', 'rebar kg', 'kg (total)', 'total kg',
                'steel (t)', 'steel (kg)', 'rebar (kg)')
    if any(k in cl for k in _mass_kw):
        return 'Mass(kg)'

    # ── Density ───────────────────────────────────────────────────────────
    if any(k in cl for k in ('densit', 'density', 'unit weight', 'bulk density',
                              'kg/m3', 'kg/m³')):
        return 'Density(kg/m3)'

    # ── Count / quantity of elements ──────────────────────────────────────
    _count_kw = ('count', 'no.', 'no ', 'nr', 'nr.', 'nos', 'nos.',
                 'number', 'num', 'qty', 'quantity', 'pcs', 'pieces',
                 'units', 'elements', 'items', 'instances', 'each',
                 'no of', 'number of', 'element count', 'item count',
                 'nº', '#')
    if cl in _count_kw or any(k in cl for k in _count_kw):
        return 'Count'

    # ── Waste factor ──────────────────────────────────────────────────────
    _waste_kw = ('waste', 'waste factor', 'waste %', 'wastage', 'wastage %',
                 'allowance', 'overbuild', 'over-measure', 'cutting waste',
                 'bulking factor', 'compaction factor')
    if any(k in cl for k in _waste_kw):
        return 'Waste'

    # ── Building level / storey ───────────────────────────────────────────
    _level_kw = ('level', 'floor level', 'storey', 'story', 'storeys',
                 'fl.', 'flr', 'floor no', 'floor number', 'zone',
                 'basement', 'b1', 'b2', 'gf', 'ground floor',
                 'podium level', 'plant level', 'roof level',
                 'lvl', 'lv', 'lev')
    if any(k in cl for k in _level_kw):
        return 'level'

    # ── Rebar / reinforcement rate ─────────────────────────────────────────
    _rebar_kw = ('rebar', 're-bar', 'reinforc', 'reinforcement', 'reinf',
                 'steel rate', 'bar rate', 'steel ratio', 'bar ratio',
                 'kg/m3', 'kg/m³', 'kg per m3', 'kg per m³',
                 'rebar rate', 'rebar ratio', 'rebar density',
                 'rebar content', 'steel content', 'r/f rate',
                 'r.c. rate', 'rc rate')
    if any(k in cl for k in _rebar_kw):
        return 'rebar_rate_kg_m3'

    # ── Concrete grade / strength ──────────────────────────────────────────
    _grade_kw = ('grade', 'concrete grade', 'strength', 'compressive strength',
                 'mpa', 'n/mm2', 'n/mm²', 'fck', 'fcu', "fc'", 'fc ',
                 'mix', 'mix design', 'mix designation',
                 'c20', 'c25', 'c28', 'c30', 'c32', 'c35', 'c40',
                 'gen0', 'gen1', 'gen2', 'gen3', 'gen 0', 'gen 1',
                 'rc32', 'rc40', 'concrete class', 'exposure class')
    if any(k in cl for k in _grade_kw):
        return 'concrete_grade_hint'

    # ── GGBS / supplementary cementitious materials ───────────────────────
    _ggbs_kw = ('ggbs', 'g.g.b.s', 'slag', 'fly ash', 'flyash', 'pfa',
                'p.f.a', 'pulverised fuel ash', 'cement replacement',
                'scm', 'supplementary', 'cem ii', 'cem iii', 'cem iv',
                'blended cement', 'low carbon cement', 'sustainab')
    if any(k in cl for k in _ggbs_kw):
        return 'ggbs_hint'

    # ── Dimensions ────────────────────────────────────────────────────────
    _len_kw = ('length', 'len', 'lg', 'lng', 'span', 'run',
               'length (m)', 'length(m)', 'len (m)', 'len(m)',
               'length mm', 'length (mm)')
    if any(k in cl for k in _len_kw):
        return 'length_m'

    _wid_kw = ('width', 'breadth', 'wd', 'wid', 'b ',
               'width (mm)', 'width(mm)', 'width (m)', 'width(m)',
               'flange width', 'section width')
    if any(k in cl for k in _wid_kw):
        return 'width_mm'

    _dep_kw = ('depth', 'height', 'thick', 'thk', 'thk.', 'thickness',
               'slab thick', 'slab depth', 'wall thick', 'section depth',
               'depth (mm)', 'depth(mm)', 'height (mm)', 'height(mm)',
               'depth (m)', 'height (m)', 'd ', 'h ',
               'overall depth', 'overall height')
    if any(k in cl for k in _dep_kw):
        return 'depth_mm'

    # ── Plan area (m²) — used only for area × thickness volume fallback ────
    _area_kw = ('area', 'plan area', 'floor area', 'surface area',
                'area (m2)', 'area(m2)', 'area m2', 'm2', 'sq m', 'sqm')
    if any(k in cl for k in _area_kw):
        return 'area_m2'

    return None


# =============================================================================
#  STEP 2 — AI COLUMN MAPPING  (Gemini Flash)
# =============================================================================

def map_columns_gemini(df: pd.DataFrame, api_key: str,
                        preliminary_roles: dict = None) -> dict:
    """
    Send column names + sample values to Gemini Flash to get a complete
    column-role mapping for columns that rule-based detection could not resolve.

    Args:
        df:                 The raw uploaded DataFrame
        api_key:            Google AI Studio Gemini API key
        preliminary_roles:  Partial mapping from detect_boq_format()

    Returns:
        dict {original_column_name: standard_role}
        Roles are one of: name, category, material_hint, volume_m3,
        Total Volume(m3), Mass(kg), Density(kg/m3), Count, Waste,
        rebar_rate_kg_m3, concrete_grade_hint, ggbs_hint, e_id,
        length_m, width_mm, depth_mm, level, IGNORE
    """
    try:
        import urllib.request
    except ImportError:
        return preliminary_roles or {}

    if preliminary_roles is None:
        preliminary_roles = {}

    # Only ask Gemini about columns that rule-based did not resolve
    unresolved = {c: None for c, r in preliminary_roles.items() if r is None}
    if not unresolved:
        return preliminary_roles  # Everything resolved by rules — skip API

    # Build sample: column name + first 3 non-null values
    samples = {}
    for col in unresolved:
        vals = df[col].dropna().astype(str).head(3).tolist()
        samples[col] = vals

    prompt = f"""You are a structural engineering quantity surveyor assistant.

CONTEXT: This data is from a structural BOQ (Bill of Quantities) used to calculate
embodied carbon (CO2e) for a building's structural frame. The pipeline needs:
  - what structural element it is — category must be one of:
      Slab, Beam, Column, Wall, Foundation, Stair, Ramp, Roof,
      Lateral System, External Structure
  - its material (concrete, steel rebar, structural steel section, post-tensioning,
    mass timber/CLT/glulam, cold-formed steel, masonry)
  - its volume in cubic metres (m³) — this is the MOST CRITICAL field
  - optionally: mass (kg), count, concrete grade, rebar rate, building level

CATEGORY GUIDANCE:
  Lateral System     — bracing, BRB, moment connection, gusset plate, base isolator,
                       viscous/fluid damper, TMD, outrigger, belt truss, knee brace,
                       wind bracing, seismic bracing, stability system
  External Structure — crash barrier, parapet wall, canopy frame/beam/column,
                       external stair, fire escape, mast, flagpole, thrust block,
                       gravity retaining wall, external frame

MAP each column below to exactly one role from this list:

  name               — element/member name or description (e.g. "300THK RC FLAT SLAB", "UC 203x203",
                       "BRB Brace", "Parapet Wall", "CLT Floor Panel")
  category           — structural category label — must be one of the 10 values listed above
  material_hint      — material text (e.g. "Concrete C32/40", "Rebar", "Structural Steel",
                       "CLT", "Glulam", "Masonry", "Cold-Formed Steel")
  volume_m3          — net volume in cubic metres (m³) — MOST IMPORTANT
  Total Volume(m3)   — total/gross volume after waste allowance
  quantity           — a SINGLE measured-quantity column whose unit varies row to
                       row (concrete in m³, rebar/steel in t or kg) and whose unit
                       is given in a separate "unit" column or written inside the
                       value cell (e.g. "84 m3", "9.25 tonnes"). Headers like
                       "Qty", "Take-off amount", "Measured Quantity", "QtyValue".
  unit               — the unit-of-measure column for `quantity` (e.g. "Unit",
                       "UOM", "Measure", "U/M"); values like m3, m², kg, t, tonne, No.
  Mass(kg)           — mass or weight in kilograms
  Density(kg/m3)     — density of material (typically 2400 concrete, 7850 steel)
  Count              — number of elements or items
  Waste              — waste factor as multiplier (1.05) or percentage (5%)
  rebar_rate_kg_m3   — rebar/reinforcement rate in kg per m³ of concrete volume
  concrete_grade_hint — concrete strength grade (e.g. "C32/40", "40 MPa", "Grade 40")
  ggbs_hint          — GGBS, fly ash, PFA or slag content percentage or label
  e_id               — emission factor catalogue code (e.g. "C_020", "R_022")
  length_m           — element length in metres
  width_mm           — element width in mm or m
  depth_mm           — element depth, height or thickness in mm or m
  level              — building floor level or storey (e.g. "Level 1", "B1", "GF")
  IGNORE             — cost, price, rate £/m³, notes, references, non-structural items,
                       architectural/MEP/finishes elements, or any column not useful for
                       embodied carbon calculation

RULES:
1. If a column contains cost (£, €, $, price, rate per unit cost) → always IGNORE.
   NOTE: "Take-off amount" / "Measured amount" are quantities, NOT cost → quantity.
2. If a column is area (m²) ONLY, with no thickness/volume → IGNORE (we need m³).
3. If unsure between volume_m3 and Total Volume(m3), pick volume_m3.
4. Headers are often messy, abbreviated, apostrophed or misspelled
   (e.g. "Mat'l family", "Scope / Spec text", "U/M", "QtyValue") — map by meaning,
   not exact text. The real description is the long free-text column; short
   item/ref/mark codes are NOT the name.
5. A measured-quantity column → `quantity` when a separate unit column exists OR
   the unit is inside the cell OR units differ row to row (m³ for concrete, t/kg
   for steel). Only use Count for a pure element COUNT of small integers with no unit.
6. Mark the unit-of-measure column as `unit` (e.g. "Unit", "UOM", "Measure", "U/M").
   Rebar/steel given in tonnes will be converted to kg downstream — still map it.
7. Prefer specificity: if a column clearly matches one role, use it; don't guess IGNORE.
8. category value contains bracing, damper, isolator, outrigger, belt truss → Lateral System.
9. category value contains parapet, canopy, barrier, mast, fire escape → External Structure.

COLUMNS AND SAMPLE VALUES TO MAP:
{json.dumps(samples, indent=2)}

Reply with ONLY a valid JSON object. No explanation. No markdown. Just the JSON.
Example: {{"Element Description": "name", "Concrete Vol (m3)": "volume_m3", "Unit Rate": "IGNORE"}}"""

    # Force JSON output so the model can't return prose/markdown; temperature 0
    # for deterministic mapping.
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }).encode('utf-8')

    # SECURITY: the API key travels in a request HEADER, never in the URL —
    # query strings end up in proxy/server logs and browser histories.
    url = GEMINI_API_URL

    # Call with one retry on transient failure. ai_roles stays {} if both fail,
    # so the pipeline degrades gracefully to the rule/fuzzy mapping.
    ai_roles = {}
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={'Content-Type': 'application/json',
                         'x-goog-api-key': api_key}, method='POST')
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode('utf-8'))
            text = result['candidates'][0]['content']['parts'][0]['text'].strip()
            text = re.sub(r'^```[a-z]*\n?', '', text)   # strip stray code fences
            text = re.sub(r'\n?```$', '', text)
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                ai_roles = parsed
            break
        except Exception as exc:
            if attempt == 1:
                print(f"[boq_parser] Gemini API error: {exc}. "
                      f"Falling back to rule/fuzzy mapping only.")

    # Merge: rule/fuzzy mapping takes priority; the LLM only fills gaps, and only
    # with a VALID role for a column we actually asked about. Anything else
    # (unknown role, hallucinated column, non-string) is dropped — the column
    # stays unresolved for the user-review / manual step. The LLM never supplies
    # a quantity, only a role label that code then trusts to read the cell.
    merged = dict(preliminary_roles)
    for col, role in ai_roles.items():
        if col in merged and merged[col] is None and isinstance(role, str):
            if role in _VALID_ROLES and role != 'IGNORE':
                merged[col] = role
    return merged


# =============================================================================
#  STEP 3 — NORMALISE
# =============================================================================

# ── Stacked / long-format BOQ support (additive) ───────────────────────────
# Some BOQs are exported in a "long" layout: one row per material, with the
# material AND its unit embedded in a label column (e.g. "Concrete (m³)",
# "Rebar (kg)", "Structural Steel (kg)", "Steel Deck (m²)") and a single
# quantity column carrying mixed units. The standard normaliser assumes one row
# per element with a dedicated volume column, so it cannot read this shape.
# The helpers below detect that specific layout and pivot it into the standard
# row schema. _detect_stacked_layout() returns None for every conventional BOQ,
# so all existing formats are completely unaffected.

# Nominal weight of profiled composite metal decking (kg per m² of plan area).
# Decking is supplied by area, not volume; this converts m² -> mass. Editable.
STACKED_DECK_KG_PER_M2 = 10.0

# material keyword -> (Material label, default unit, e_id, is_steel,
#                      is_structural_steel, density, ifc_type, name_suffix)
# Order matters: more specific keywords are listed first.
_STACKED_MATERIALS = [
    ('post-tension',     ('Post Tensioning', 'kg', 'PT_032', True,  False, 7850.0, 'IfcTendon', 'PT Steel')),
    ('post tension',     ('Post Tensioning', 'kg', 'PT_032', True,  False, 7850.0, 'IfcTendon', 'PT Steel')),
    ('tendon',           ('Post Tensioning', 'kg', 'PT_032', True,  False, 7850.0, 'IfcTendon', 'PT Steel')),
    ('steel deck',       ('Steel',           'm2', 'R_004',  True,  True,  7850.0, '',          'Steel Deck')),
    ('metal deck',       ('Steel',           'm2', 'R_004',  True,  True,  7850.0, '',          'Steel Deck')),
    ('composite deck',   ('Steel',           'm2', 'R_004',  True,  True,  7850.0, '',          'Steel Deck')),
    ('structural steel', ('Steel',           'kg', 'R_004',  True,  True,  7850.0, '',          'Structural Steel')),
    ('steel section',    ('Steel',           'kg', 'R_004',  True,  True,  7850.0, '',          'Steel Section')),
    ('rebar',            ('Steel',           'kg', 'R_005',  True,  False, 7850.0, '',          'Rebar')),
    ('re-bar',           ('Steel',           'kg', 'R_005',  True,  False, 7850.0, '',          'Rebar')),
    ('reinforc',         ('Steel',           'kg', 'R_005',  True,  False, 7850.0, '',          'Rebar')),
    ('mesh',             ('Steel',           'kg', 'R_005',  True,  False, 7850.0, '',          'Mesh')),
    ('concrete',         ('Concrete',        'm3', 'C_020',  False, False, 2400.0, '',          'Concrete')),
]


def _parse_stacked_material(value):
    """Return the material spec dict for a stacked label value, or None.

    Recognises labels like "Concrete (m³)", "Rebar (kg)", "Structural Steel (kg)",
    "Steel Deck (m²)". The unit is read from the label when legible, otherwise
    inferred from the material (concrete→m³, rebar/steel→kg, deck→m²) — this keeps
    working even when the ³/² characters are mangled by a bad file encoding.
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None

    spec = None
    for kw, data in _STACKED_MATERIALS:
        if kw in s:
            spec = data
            break
    if spec is None:
        return None

    mat_label, default_unit, e_id, is_steel, is_struct, density, ifc_type, suffix = spec

    # Explicit unit in the label overrides the material default where legible.
    unit = default_unit
    if 'kg' in s:
        unit = 'kg'
    elif 'm3' in s or 'm³' in s or 'cu.m' in s or 'cum' in s:
        unit = 'm3'
    elif 'm2' in s or 'm²' in s:
        unit = 'm2'
    elif '(t)' in s or ' t)' in s or 'tonne' in s:
        unit = 't'

    return {
        'Material': mat_label, 'unit': unit, 'e_id': e_id,
        'is_steel': is_steel, 'is_structural_steel': is_struct,
        'density': density, 'ifc_type': ifc_type, 'suffix': suffix,
    }


def _detect_stacked_layout(df: pd.DataFrame, column_roles: dict):
    """Detect a stacked/long BOQ layout.

    Returns a dict {element_col, material_col, qty_col, count_col} when the sheet
    is a stacked layout, or None for the common case (so existing formats are
    untouched). The signature it keys on: a column whose values are unit-bearing
    material labels spanning at least two distinct units, with no dedicated
    volume column present.
    """
    if df is None or df.shape[0] == 0 or df.shape[1] < 3:
        return None

    # Duplicate column labels make df[col] return a DataFrame — not a stacked
    # sheet; let the normal path handle it.
    if df.columns.duplicated().any():
        return None

    # Veto: if a real volume column was identified, this is a normal BOQ.
    roles = column_roles or {}
    if any(r in ('volume_m3', 'Total Volume(m3)') for r in roles.values()):
        return None

    cols = list(df.columns)

    def _is_num(v):
        try:
            float(str(v).replace(',', '').strip())
            return True
        except (ValueError, TypeError):
            return False

    # 1. Material-label column: most values parse to a material spec, the values
    #    carry an explicit "(unit)" token, and at least two distinct implied
    #    units appear (the signature of a mixed-unit stacked sheet).
    material_col = None
    for c in cols:
        vals = [v for v in df[c].tolist() if pd.notna(v) and str(v).strip()]
        if len(vals) < 2:
            continue
        matched = [s for s in (_parse_stacked_material(v) for v in vals) if s is not None]
        if len(matched) < 0.6 * len(vals):
            continue
        has_paren = sum(1 for v in vals if '(' in str(v) and ')' in str(v))
        if has_paren < 0.5 * len(vals):
            continue
        if len({s['unit'] for s in matched}) < 2:
            continue
        material_col = c
        break

    if material_col is None:
        return None

    # 2. Numeric columns → quantity (varied magnitudes) vs count (small ints).
    numeric_cols = []
    for c in cols:
        if c == material_col:
            continue
        vals = [v for v in df[c].tolist() if pd.notna(v) and str(v).strip()]
        if vals and sum(1 for v in vals if _is_num(v)) >= 0.6 * len(vals):
            numeric_cols.append(c)
    if not numeric_cols:
        return None

    def _num_series(c):
        return pd.to_numeric(df[c].astype(str).str.replace(',', '', regex=False),
                             errors='coerce')

    count_col = None
    qty_col = None
    if len(numeric_cols) == 1:
        qty_col = numeric_cols[0]
    else:
        scored = []
        for c in numeric_cols:
            s = _num_series(c).dropna()
            if s.empty:
                continue
            is_counter = (s.max() <= 50 and (s == s.round()).all()) or 'count' in str(c).lower()
            scored.append((c, is_counter, float(s.abs().sum())))
        counters = [c for c, ic, _ in scored if ic]
        if counters:
            count_col = counters[0]
        rest = [(c, tot) for c, ic, tot in scored if c != count_col]
        if rest:
            qty_col = max(rest, key=lambda x: x[1])[0]
        elif scored:
            qty_col = scored[0][0]
    if qty_col is None:
        return None

    # 3. Element column: a remaining text column (prefer an element-like header).
    used = {material_col, qty_col, count_col}
    text_cols = [c for c in cols if c not in used]
    element_col = None
    for c in text_cols:
        if any(k in str(c).lower() for k in ('element', 'member', 'name', 'description', 'item')):
            element_col = c
            break
    if element_col is None and text_cols:
        element_col = text_cols[0]
    if element_col is None:
        return None

    return {'element_col': element_col, 'material_col': material_col,
            'qty_col': qty_col, 'count_col': count_col}


def _normalise_stacked_boq(df: pd.DataFrame, layout: dict) -> tuple:
    """Pivot a detected stacked/long BOQ into the standard structural schema.

    Mirrors the row schema produced by normalise_boq() so the downstream pipeline
    (filter page, materials step, prepare_boq_from_ifc, calculations) is
    unaffected. Steel/PT/deck quantities given by mass or area are converted to a
    net volume (mass / 7850), because the IFC-style engine re-derives steel mass
    as volume × 7850. Returns (structural_df, excluded_df).
    """
    element_col = layout['element_col']
    material_col = layout['material_col']
    qty_col = layout['qty_col']
    count_col = layout['count_col']

    rows_structural = []

    for _, row in df.iterrows():
        spec = _parse_stacked_material(row.get(material_col, ''))
        if spec is None:
            continue
        qty = pd.to_numeric(str(row.get(qty_col, '')).replace(',', '').strip(),
                            errors='coerce')
        if qty is None or pd.isna(qty) or qty <= 0:
            continue

        element = str(row.get(element_col, '') or '').strip()
        level = element.split(' - ')[0].strip() if ' - ' in element else ''
        category = _parse_revit_category(element) if element else 'Other'

        density = spec['density']
        unit = spec['unit']
        if unit == 'm3':
            volume = float(qty)
        elif unit == 'm2':
            volume = (float(qty) * STACKED_DECK_KG_PER_M2) / density
        elif unit == 't':
            volume = (float(qty) * 1000.0) / density
        else:  # kg
            volume = float(qty) / density
        mass = volume * density

        count = 1
        if count_col is not None:
            c = pd.to_numeric(row.get(count_col, 1), errors='coerce')
            if c is not None and not pd.isna(c) and c >= 1:
                count = int(c)

        desc = f"{element} - {spec['suffix']}" if element else spec['suffix']
        is_steel = spec['is_steel']
        is_struct = spec['is_structural_steel']
        if spec['Material'] == 'Post Tensioning':
            waste = 1.015
        elif is_struct:
            waste = 1.01
        else:
            waste = 1.05

        rows_structural.append({
            'name': desc, 'Description': desc,
            'category': category, 'Category': category,
            'Material': spec['Material'],
            'volume_m3': volume, 'Volume(m3)': volume,
            'Total Volume(m3)': volume * waste,
            'Density(kg/m3)': density,
            'Mass(kg)': mass,
            'Count': count,
            'Waste': waste,
            'e_id': spec['e_id'],
            'is_steel': is_steel,
            'is_structural_steel': is_struct,
            'has_modeled_rebar': False,
            'level': level,
            'rebar_rate_hint': 0.0,
            'concrete_grade_hint': '',
            'ggbs_hint': '',
            'family': '',
            'ifc_type': spec['ifc_type'],
            'count': count,
        })

    return pd.DataFrame(rows_structural), pd.DataFrame([])


def normalise_boq(df: pd.DataFrame, column_roles: dict) -> tuple:
    """
    Apply the column mapping and produce two DataFrames:

    Returns:
        (structural_df, excluded_df)

        structural_df  — pipeline-ready DataFrame (same schema as csv_processor output)
                         with extra flags:
                           has_modeled_rebar (bool) — True if an explicit rebar row
                           exists for this concrete member — signals engine to skip
                           rate-based rebar addition (no duplicates)

        excluded_df    — rows identified as non-structural/architectural, kept so
                         the GUI can show them and let the user re-include any
    """
    # ── Stacked / long-format fast-path (additive) ────────────────────────────
    # If the sheet is a stacked layout (one row per material, with a unit-bearing
    # material label column and a single mixed-unit quantity column), pivot it
    # directly. Returns None for every conventional BOQ, so all existing formats
    # take the unchanged code path below.
    _stacked = _detect_stacked_layout(df, column_roles)
    if _stacked is not None:
        return _normalise_stacked_boq(df, _stacked)

    # ── Pre-filter: drop rows that are clearly headers, subtotals, or blanks ──
    # These commonly appear in contractor BOQs and inflate the row count with
    # useless entries. Drop any row where ALL cells are NaN, or where the first
    # non-null cell looks like a section header (all-caps text, no numeric data).
    def _is_junk_row(row) -> bool:
        vals = [v for v in row.values if pd.notna(v) and str(v).strip()]
        if not vals:
            return True  # completely blank row
        # Row where every value is non-numeric text → likely a section heading
        numeric_count = sum(1 for v in vals if _is_numeric(v))
        if numeric_count == 0 and len(vals) <= 3:
            return True  # e.g. "SUBSTRUCTURE", "Section 2 — Superstructure"
        return False

    def _is_numeric(v) -> bool:
        try:
            float(str(v).replace(',', '').strip())
            return True
        except (ValueError, TypeError):
            return False

    df = df[~df.apply(_is_junk_row, axis=1)].reset_index(drop=True)

    # ── Invert: standard_role → [original_columns] ────────────────────────────
    role_to_cols = {}
    for orig, role in column_roles.items():
        if role and role != 'IGNORE':
            role_to_cols.setdefault(role, []).append(orig)

    def _pick(role, *fallback_roles):
        """Return first column that maps to role (or fallbacks), else None."""
        for r in (role,) + fallback_roles:
            if r in role_to_cols and role_to_cols[r]:
                return role_to_cols[r][0]
        return None

    # When several columns map to 'name' (e.g. an "Item" number column AND a
    # "Description" column), prefer the free-text description — material and
    # category inference depend on it, and an item number tells us nothing.
    name_col    = _pick_name_col(df, role_to_cols.get('name', []))
    cat_col     = _pick('category')
    mat_col     = _pick('material_hint')
    vol_col     = _pick('volume_m3', 'Total Volume(m3)')
    tvol_col    = _pick('Total Volume(m3)', 'volume_m3')
    mass_col    = _pick('Mass(kg)')
    dens_col    = _pick('Density(kg/m3)')
    count_col   = _pick('Count')
    waste_col   = _pick('Waste')
    rebar_col   = _pick('rebar_rate_kg_m3')
    grade_col   = _pick('concrete_grade_hint')
    ggbs_col    = _pick('ggbs_hint')
    eid_col     = _pick('e_id')
    level_col   = _pick('level')
    area_col    = _pick('area_m2')
    depth_col   = _pick('depth_mm')
    qty_col     = _pick('quantity')   # unit-bearing measured quantity (Qty/Take-off/Measured)
    unit_col    = _pick('unit')       # unit-of-measure column (Unit/UOM/Measure)

    rows_structural = []
    rows_excluded   = []

    # First pass: identify rows that are explicit rebar/reinforcement lines
    # Key = normalised description of the parent concrete member they belong to
    modeled_rebar_descs = set()
    if name_col:
        for _, row in df.iterrows():
            desc = str(row.get(name_col, '') or '').strip().lower()
            mat  = str(row.get(mat_col, '') or '').lower() if mat_col else ''
            if _is_rebar_row(desc, mat):
                # Record the parent member name pattern (strip rebar keywords)
                parent = _infer_parent_member(desc)
                if parent:
                    modeled_rebar_descs.add(parent)

    for _, row in df.iterrows():
        desc  = str(row.get(name_col, '') or '').strip() if name_col else ''
        mat   = str(row.get(mat_col,  '') or '').strip() if mat_col  else ''
        cat   = str(row.get(cat_col,  '') or '').strip() if cat_col  else ''
        level = str(row.get(level_col,'') or '').strip() if level_col else ''
        eid   = str(row.get(eid_col,  '') or '').strip() if eid_col  else ''

        # ── Skip subtotal / carried-total / repeated-header rows ──────────
        # These commonly appear in printed/contractor BOQs and would otherwise
        # double-count (a "Total concrete" row) or inject a header as a fake
        # element. Matched on the description and the whole row text.
        _dl = desc.lower().strip()
        _rowtext = ' '.join(str(v) for v in row.values if pd.notna(v)).lower()
        if (any(k in _rowtext for k in _JUNK_ROW_KW)
                or _dl in ('item', 'description', 'work description', 'element',
                           'member', 'scope / spec text', 'repeated header row')
                or _dl.startswith(('subtotal', 'sub-total', 'sub total', 'carried',
                                   'brought forward', 'grand total', 'collection',
                                   'total '))):
            continue

        # Volume
        # Blank/non-numeric cells become NaN via to_numeric; NaN is truthy so the
        # `or 0.0` idiom would leave vol as NaN, and `NaN <= 0` is False — that
        # silently skips both the Total-Volume fallback and the mass→volume
        # derivation below (steel rows carry mass only). Coerce NaN to 0.0.
        vol = 0.0
        if vol_col:
            _v = pd.to_numeric(row.get(vol_col, 0), errors='coerce')
            vol = 0.0 if pd.isna(_v) else float(_v)
        if vol <= 0 and tvol_col and tvol_col != vol_col:
            _v = pd.to_numeric(row.get(tvol_col, 0), errors='coerce')
            vol = 0.0 if pd.isna(_v) else float(_v)

        # Fallback: compute volume from area × thickness if no volume column
        # Handles BOQs that give m² + mm thickness (common in UK slab schedules)
        if vol <= 0 and area_col and depth_col:
            area = pd.to_numeric(row.get(area_col, 0), errors='coerce') or 0.0
            thk  = pd.to_numeric(row.get(depth_col, 0), errors='coerce') or 0.0
            if area > 0 and thk > 0:
                # thickness may be in mm — convert if value looks like mm (>1.0)
                thk_m = thk / 1000.0 if thk > 1.0 else thk
                vol = area * thk_m

        # ── Unit-aware quantity ───────────────────────────────────────────
        # A single "Qty"/"Take-off"/"Measured" column whose unit lives in a
        # separate Unit/UOM column or inside the value cell ("84 m3",
        # "9.25 tonnes"), with units mixed down the column. Route to volume
        # (m³) or mass (kg) and convert rebar/steel tonne→kg. Only fills what
        # the dedicated volume/mass columns did not, so explicit-column BOQs
        # are untouched.
        qty_mass = 0.0
        qty_consumed = False
        if vol <= 0 and (qty_col or unit_col):
            raw_q = row.get(qty_col) if qty_col else row.get(vol_col)
            qv_num, qv_unit = _split_qty_unit(raw_q)
            if qv_unit is None and unit_col:
                qv_unit = _canonical_unit(row.get(unit_col))
            steel_like = (_is_rebar_row(desc.lower(), mat.lower())
                          or _is_steel_material(mat) or _is_steel_material(desc)
                          or 'steel' in f"{desc} {mat}".lower()
                          or ('conc' not in mat.lower()
                              and (is_structural_steel_name(desc)
                                   or is_structural_steel_name(cat))))
            _qv, _qm = _qty_to_vol_mass(qv_num, qv_unit, steel_like)
            if _qv > 0:
                vol = _qv
            if _qm > 0:
                qty_mass = _qm
            qty_consumed = (_qv > 0 or _qm > 0)

        # Skip completely empty rows (no volume, no mass, no description)
        if vol <= 0 and qty_mass <= 0 and not desc:
            continue

        # Mass
        mass = 0.0
        if mass_col:
            mass = pd.to_numeric(row.get(mass_col, 0), errors='coerce') or 0.0
        if mass <= 0 and qty_mass > 0:
            mass = qty_mass

        # Count
        count = 1
        if count_col:
            # pd.to_numeric returns NaN on blank/non-numeric cells, and NaN is
            # truthy so `or 1` does not catch it — int(NaN) would crash. Guard
            # explicitly so rows with an empty Count default to 1.
            count_raw = pd.to_numeric(row.get(count_col, 1), errors='coerce')
            count = 1 if pd.isna(count_raw) else max(int(count_raw), 1)
        elif qty_col and not qty_consumed:
            # A "Qty" column that was NOT used as a measured volume/mass (a real
            # volume column supplied the quantity) is an element count — restore
            # the pre-existing Qty→Count behaviour for plain count columns.
            _qn, _qu = _split_qty_unit(row.get(qty_col))
            if (_qu in (None, 'count') and _qn is not None
                    and _qn >= 1 and float(_qn).is_integer()):
                count = max(int(_qn), 1)

        # Waste
        waste = 1.05
        if waste_col:
            w = pd.to_numeric(row.get(waste_col, None), errors='coerce')
            if w is not None and not pd.isna(w):
                # Accept both 1.05 and 5 (percent) forms
                waste = w if w >= 1.0 else (1.0 + w / 100.0)

        # Rebar rate hint (kg/m³) — may be in a dedicated column
        rebar_rate_hint = 0.0
        if rebar_col:
            rebar_rate_hint = pd.to_numeric(row.get(rebar_col, 0), errors='coerce') or 0.0

        # Concrete grade / GGBS hints — stored for the Materials step to use
        grade_hint = str(row.get(grade_col, '') or '').strip() if grade_col else ''
        ggbs_hint  = str(row.get(ggbs_col,  '') or '').strip() if ggbs_col  else ''

        # Infer is_steel / is_structural_steel
        combined_text = f"{desc} {mat} {cat}"
        is_rebar = _is_rebar_row(desc.lower(), mat.lower())
        is_steel = is_rebar or _is_steel_material(mat) or _is_steel_material(desc)
        # Section designations written without a space ("UC203x203x46") and
        # steel-only categories ("UC-Universal Columns") never hit the material
        # keywords — recognise them by section name/category too. An explicit
        # concrete material always wins ("Concrete - Rectangular Beam" stays
        # concrete even if the name contains a section-like token).
        if (not is_steel and 'conc' not in mat.lower()
                and (is_structural_steel_name(desc)
                     or is_structural_steel_name(cat))):
            is_steel = True
        # A post-tensioned / reinforced CONCRETE member ("200mm PT slab",
        # "RC beam") reads as steel only because "PT"/"reinforced" hit the steel
        # keywords. It is concrete — its PT/rebar is applied as a kg/m³ rate, not
        # massed as solid steel. Flip it back UNLESS it is an explicit rebar row,
        # the Material column says steel, or the text names the steel product
        # itself (strand/bar/section/plate).
        if (is_steel and not is_rebar
                and 'conc' not in mat.lower()
                and not _is_steel_material(mat)
                and _looks_concrete(f"{desc} {cat}")
                and not _names_steel_product(f"{desc} {mat}")):
            is_steel = False
        is_structural_steel = (
            is_steel and not is_rebar
            and _is_structural_steel_from_text(f"{desc} {cat}", mat)
        )

        # Infer category if not given
        if not cat:
            cat = _parse_revit_category(combined_text)

        # Infer material label
        if is_rebar or is_structural_steel or is_steel:
            mat_label = 'Steel'
        elif _looks_concrete(f"{desc} {mat} {cat}"):
            mat_label = 'Concrete'
        else:
            # No concrete/steel signal → genuinely unknown material. Kept as
            # 'Unknown' so the UI offers a full-database factor picker instead of
            # silently pricing it as concrete C32/40.
            mat_label = 'Unknown'

        # Density
        density = 7850.0 if is_steel else 2400.0
        if dens_col:
            d = pd.to_numeric(row.get(dens_col, None), errors='coerce')
            if d and d > 0:
                density = float(d)

        # Compute mass if not given
        if mass <= 0 and vol > 0:
            mass = vol * density

        # Compute volume from mass if volume missing
        if vol <= 0 and mass > 0 and density > 0:
            vol = mass / density

        # Nothing measurable on this row (e.g. an m²/length/units-only line, or a
        # quantity in a unit we cannot convert to volume or mass) — skip it so it
        # doesn't appear as a zero-quantity element.
        if vol <= 0 and mass <= 0:
            continue

        # Total volume
        if tvol_col and tvol_col != vol_col:
            tvol = pd.to_numeric(row.get(tvol_col, 0), errors='coerce') or 0.0
        else:
            tvol = vol * waste

        # ── Non-structural classification ───────────────────────────────
        if _is_non_structural(desc, cat, mat):
            rows_excluded.append({
                'name': desc, 'Description': desc,
                'category': cat, 'Category': cat,
                'Material': mat_label,
                'volume_m3': vol, 'Volume(m3)': vol,
                'Total Volume(m3)': tvol,
                'Density(kg/m3)': density,
                'Mass(kg)': mass,
                'Count': count,
                'Waste': waste,
                'e_id': eid,
                'is_steel': is_steel,
                'is_structural_steel': is_structural_steel,
                'has_modeled_rebar': False,
                'level': level,
                'rebar_rate_hint': rebar_rate_hint,
                'concrete_grade_hint': grade_hint,
                'ggbs_hint': ggbs_hint,
                'excluded_reason': 'non_structural',
            })
            continue

        # ── has_modeled_rebar flag ──────────────────────────────────────
        # True for a concrete row when an explicit rebar row already exists
        # in this BOQ for the same parent member — avoids rate-based duplication
        has_modeled_rebar = False
        if not is_steel and not is_rebar:
            parent_key = _infer_parent_member(desc.lower())
            has_modeled_rebar = bool(parent_key and parent_key in modeled_rebar_descs)

        rows_structural.append({
            'name': desc, 'Description': desc,
            'category': cat, 'Category': cat,
            'Material': mat_label,
            'volume_m3': vol, 'Volume(m3)': vol,
            'Total Volume(m3)': tvol,
            'Density(kg/m3)': density,
            'Mass(kg)': mass,
            'Count': count,
            'Waste': waste,
            'e_id': eid if eid else '',
            'is_steel': is_steel,
            'is_structural_steel': is_structural_steel,
            'has_modeled_rebar': has_modeled_rebar,
            'level': level,
            'rebar_rate_hint': rebar_rate_hint,
            'concrete_grade_hint': grade_hint,
            'ggbs_hint': ggbs_hint,
            'family': '',
            'ifc_type': '',
            'count': count,
        })

    structural_df = pd.DataFrame(rows_structural)
    excluded_df   = pd.DataFrame(rows_excluded)

    return structural_df, excluded_df


# =============================================================================
#  PUBLIC ENTRY POINT
# =============================================================================

def _clean_excel_layout(file_path: str) -> pd.DataFrame:
    """
    Load an Excel/CSV file robustly — handles messy real-world BOQs where:
      - The header row is not the first row (title rows above)
      - Some columns are entirely empty (unnamed/NaN headers)
      - Leading blank columns exist

    Strategy:
      1. Read with no header assumption (header=None)
      2. Find the first row that contains at least 2 non-null text cells
         and at least 1 cell that looks like a column label — this is the header
      3. Use that row as the header, everything above it is dropped
      4. Drop columns that are entirely NaN or have a NaN/integer header
         (these are unnamed spacer columns Revit/Excel sometimes exports)
      5. Reset index
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == '.csv':
        # CSVs: try default first, then no-header scan
        try:
            df = pd.read_csv(file_path)
            if _looks_like_valid_header(df.columns.tolist()):
                df = _drop_empty_columns(df)
                return df
        except Exception:
            pass
        # Ragged-tolerant fallback: a short title/preamble row must not make
        # pandas infer too few columns and drop/err on the wider data rows.
        from csv_processor import _read_csv_ragged
        raw = _read_csv_ragged(file_path)
    else:
        # Workbooks often carry README / Cover / Notes / Rates / Summary / Blank
        # sheets alongside the actual bill. Read every sheet and pick the one
        # that most looks like a BOQ table, so we don't parse the README by
        # accident (the previous behaviour read only the first sheet).
        try:
            sheets = pd.read_excel(file_path, sheet_name=None, header=None)
        except Exception:
            sheets = None
        if sheets:
            raw = (next(iter(sheets.values())) if len(sheets) == 1
                   else _select_boq_sheet(sheets))
            if raw is None or raw.shape[0] == 0:
                raw = next(iter(sheets.values()))
        else:
            raw = pd.read_excel(file_path, header=None)

    # Find the header row
    header_row_idx = _find_header_row(raw)

    if header_row_idx is None:
        # Fallback: treat row 0 of the selected sheet as the header
        data = raw.copy()
        if len(data):
            data.columns = data.iloc[0].tolist()
            data = data.iloc[1:].reset_index(drop=True)
        return _drop_empty_columns(data)

    # Use that row as column names, data starts after it
    headers = raw.iloc[header_row_idx].tolist()
    data = raw.iloc[header_row_idx + 1:].reset_index(drop=True)
    data.columns = headers

    # Drop columns with NaN or purely numeric header (unnamed spacers)
    data = _drop_empty_columns(data)
    return data


def _select_boq_sheet(sheets: dict) -> pd.DataFrame:
    """From {sheet_name: raw_df(header=None)} pick the raw sheet most likely to
    be the BOQ table. Non-BOQ sheets (README/Cover/Notes/Rates/Summary/Blank,
    and the Expected_Normalised helper sheets) are strongly deprioritised; the
    rest are scored by header-row presence and how many rows carry a number."""
    def _num(v):
        try:
            float(str(v).split()[0].replace(',', '')) if str(v).strip() else None
            return bool(str(v).strip()) and str(v).strip().lower() not in ('nan', 'none') \
                and str(v).split()[0].replace(',', '').replace('.', '', 1).lstrip('-').isdigit()
        except (ValueError, IndexError):
            return False

    best, best_score = None, -1e9
    for name, raw in sheets.items():
        if raw is None or raw.shape[0] == 0:
            continue
        hdr = _find_header_row(raw)
        start = (hdr + 1) if hdr is not None else 0
        numeric_rows = sum(1 for _, r in raw.iloc[start:].iterrows()
                           if any(_num(v) for v in r))
        score = numeric_rows + (5 if hdr is not None else 0)
        if any(k in str(name).lower() for k in _SHEET_SKIP_KW):
            score -= 1000  # README / Cover / Notes / Rates / Summary / Expected …
        if score > best_score:
            best, best_score = raw, score
    return best


def _find_header_row(raw: pd.DataFrame) -> int:
    """
    Scan rows top-to-bottom and return the index of the row most likely
    to be the header. A header row has:
      - At least 2 non-null cells
      - At least 1 cell containing text that matches a known column keyword
    """
    header_keywords = {
        'description', 'desc', 'element', 'item', 'name', 'type',
        'vol', 'volume', 'qty', 'quantity', 'count', 'no.', 'nr',
        'mass', 'weight', 'area', 'length', 'level', 'storey',
        'material', 'category', 'cat', 'grade', 'rebar', 'steel',
        'floor', 'section', 'ref', 'mark', 'id',
    }
    for i, row in raw.iterrows():
        vals = [str(v).strip().lower() for v in row if pd.notna(v) and str(v).strip()]
        if len(vals) < 2:
            continue
        if any(any(kw in v for kw in header_keywords) for v in vals):
            return i
    return None


def _looks_like_valid_header(cols) -> bool:
    """Return True if the column list looks like real headers (not numeric indices)."""
    text_cols = [c for c in cols if isinstance(c, str) and not c.startswith('Unnamed')]
    return len(text_cols) >= 2


def _drop_empty_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Drop columns that are:
      - Entirely NaN
      - Have a NaN header
      - Have a purely numeric header (0, 1, 2 ... from headerless reads)
    """
    keep = []
    for col in df.columns:
        # Drop if header is NaN or a plain integer index
        if pd.isna(col) or (isinstance(col, (int, float)) and str(col).replace('.0','').isdigit()):
            continue
        # Drop if column name is 'Unnamed: N'
        if isinstance(col, str) and col.startswith('Unnamed'):
            continue
        # Drop if entire column is NaN
        if df[col].isna().all():
            continue
        keep.append(col)
    return df[keep].reset_index(drop=True)


def parse_universal_boq(file_path: str, api_key: str = None,
                         user_overrides: dict = None) -> dict:
    """
    Full pipeline: load file → clean layout → detect format → map columns → normalise.

    Args:
        file_path:      Path to any CSV or Excel BOQ file
        api_key:        Gemini Flash API key (optional — skips AI if not provided)
        user_overrides: {original_col: standard_role} edits made by the user
                        in the "Column Mapping Review" GUI step — these take
                        priority over both rule-based and AI mapping

    Returns a dict with:
        'format'          BOQFormat object
        'column_roles'    Final column mapping used
        'structural_df'   Pipeline-ready DataFrame (structural rows only)
        'excluded_df'     Non-structural rows (user can re-include via GUI)
        'warnings'        List of warning strings for the GUI to display
    """
    # Always use the robust loader — handles misaligned headers, blank columns, etc.
    df = _clean_excel_layout(file_path)

    warnings = []

    # ── Step 1: Detect format ──────────────────────────────────────────
    boq_fmt = detect_boq_format(df)

    # If it's already a known format, hand off to existing processors
    if boq_fmt.fmt == BOQFormat.READY:
        from csv_processor import process_ready_format
        structural_df = process_ready_format(df)
        structural_df['has_modeled_rebar'] = False
        return {
            'format': boq_fmt,
            'column_roles': boq_fmt.column_roles,
            'structural_df': structural_df,
            'excluded_df': pd.DataFrame(),
            'warnings': ['File recognised as standard ready-format BOQ — no AI mapping needed.'],
        }

    if boq_fmt.fmt == BOQFormat.REVIT_RAW:
        from csv_processor import process_revit_raw
        structural_df = process_revit_raw(df)
        structural_df['has_modeled_rebar'] = False
        return {
            'format': boq_fmt,
            'column_roles': boq_fmt.column_roles,
            'structural_df': structural_df,
            'excluded_df': pd.DataFrame(),
            'warnings': ['Revit Material Takeoff detected — processed via standard Revit path.'],
        }

    # ── Step 2: AI mapping for scratch / unknown BOQs ─────────────────
    if api_key:
        column_roles = map_columns_gemini(df, api_key, boq_fmt.column_roles)
    else:
        column_roles = boq_fmt.column_roles
        warnings.append(
            "No Gemini API key provided — using rule-based column mapping only. "
            "Some columns may not be mapped correctly. "
            "Please review the column mapping table before proceeding."
        )

    # ── Apply user overrides from the GUI review step ─────────────────
    if user_overrides:
        for col, role in user_overrides.items():
            column_roles[col] = role if role != 'IGNORE' else None

    # Warn about unmapped columns
    unmapped = [c for c, r in column_roles.items() if r is None]
    if unmapped:
        warnings.append(
            f"{len(unmapped)} column(s) could not be mapped and will be ignored: "
            + ', '.join(unmapped[:5]) + ('...' if len(unmapped) > 5 else '')
        )

    # Check that a volume column was found — critical
    has_vol = any(r in ('volume_m3', 'Total Volume(m3)') for r in column_roles.values() if r)
    if not has_vol:
        warnings.append(
            "WARNING: No volume column could be identified. "
            "Rows with zero volume will be skipped. "
            "Please check the column mapping and assign the volume column manually."
        )

    # ── Step 3: Normalise ─────────────────────────────────────────────
    structural_df, excluded_df = normalise_boq(df, column_roles)

    if len(structural_df) == 0:
        warnings.append(
            "No structural rows were extracted. "
            "Check the column mapping — the volume column may not be correctly assigned."
        )

    n_excl = len(excluded_df)
    if n_excl > 0:
        warnings.append(
            f"{n_excl} row(s) identified as non-structural/architectural and excluded. "
            "You can review and re-include them in the BOQ Review step."
        )

    # Check for rebar duplicate prevention
    if structural_df is not None and len(structural_df) > 0:
        n_rebar_flagged = structural_df['has_modeled_rebar'].sum() \
            if 'has_modeled_rebar' in structural_df.columns else 0
        if n_rebar_flagged > 0:
            warnings.append(
                f"{n_rebar_flagged} concrete element(s) have explicit rebar rows in this BOQ. "
                "Rate-based rebar will NOT be added for those elements to avoid double-counting."
            )

    return {
        'format': boq_fmt,
        'column_roles': column_roles,
        'structural_df': structural_df,
        'excluded_df': excluded_df,
        'warnings': warnings,
    }


# =============================================================================
#  PRIVATE HELPERS
# =============================================================================

def _pick_name_col(df: pd.DataFrame, candidates: list):
    """Choose the best element-description column among several 'name' matches.
    Prefers a header that reads like free text (Description/Work/Spec/Member),
    else the column with the longest average text (item numbers and marks are
    short); element marks/IDs lose to real descriptions."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    _pref = ('description', 'desc', 'work item', 'work description', 'spec',
             'scope', 'particular', 'narrative', 'member description',
             'element description')
    for c in candidates:
        if any(p in str(c).lower() for p in _pref):
            return c
    best, best_len = candidates[0], -1.0
    for c in candidates:
        try:
            avg = df[c].dropna().astype(str).str.len().mean()
        except Exception:
            avg = 0.0
        if avg and avg > best_len:
            best, best_len = c, avg
    return best


def _looks_concrete(text: str) -> bool:
    """True if the text carries a concrete signal (material, grade, or a concrete
    structural element). Used to decide 'Concrete' vs 'Unknown' — a row with no
    concrete or steel signal is left Unknown so the user can pick any factor."""
    t = (text or '').lower()
    return any(k in t for k in (
        'concrete', 'conc', 'r.c.', ' rc ', 'rcc', 'reinforced', 'in-situ', 'in situ',
        'precast', 'blinding', 'screed', 'grout', 'gen0', 'gen1', 'gen2', 'gen3',
        'c20', 'c25', 'c28', 'c30', 'c32', 'c35', 'c40', 'c45', 'c50',
        'slab', 'beam', 'column', 'wall', 'footing', 'foundation', 'pile',
        'raft', 'pier', 'lintel', 'stair', 'ramp', 'deck', 'kerb',
    ))


def _is_rebar_row(desc_lower: str, mat_lower: str) -> bool:
    """Return True if this row represents a rebar/reinforcement element."""
    combined = f"{desc_lower} {mat_lower}"
    return any(k in combined for k in [
        'rebar', 'reinforc', 'reinforcing bar', 'mesh', 're-bar',
        'high yield', 'mild steel bar', 'bar bending', 'bbs',
        'tendon', 'strand', 'post-tension', 'post tension', 'pt bar',
    ])


# Tokens that name the steel PRODUCT itself (a strand/bar/section/plate), as
# opposed to a reinforced- or post-tensioned CONCRETE member. The bare word
# "steel" also counts (an explicit "Steel Beam" is steel), but a qualifier like
# "PT" or "reinforced" on a concrete member does NOT.
_STEEL_PRODUCT_TOKENS = (
    'steel', 'strand', 'tendon', 'cable', 'rebar', 're-bar', 'mesh', 'fabric',
    'plate', 'coupler', 'ferrule', ' bar', 'bars', ' wire', 'rsj',
)

def _names_steel_product(text: str) -> bool:
    """True when the text names the steel product itself (strand/bar/section/
    plate/…), not merely a reinforced-concrete member. Used to keep a
    post-tensioned or reinforced concrete member ("200mm PT slab", "RC beam")
    classified as concrete rather than solid steel."""
    t = (text or '').lower()
    if any(k in t for k in _STEEL_PRODUCT_TOKENS):
        return True
    return is_structural_steel_name(text)


def _infer_parent_member(rebar_desc_lower: str) -> str:
    """
    From a rebar row description, extract the parent concrete member keyword
    so we can match it against concrete rows and prevent duplicate rebar.
    e.g. 'rebar in slab S1' → 'slab s1'
         'column C3 reinforcement' → 'column c3'
    """
    # Strip common rebar keywords
    cleaned = re.sub(
        r'\b(rebar|reinforcement|reinforcing|bar|mesh|steel|re-bar|bbs)\b',
        '', rebar_desc_lower
    ).strip()
    # Return first meaningful token group
    tokens = [t for t in cleaned.split() if len(t) > 1]
    return ' '.join(tokens[:3]) if tokens else ''


def _is_non_structural(desc: str, cat: str, mat: str) -> bool:
    """
    Return True if the element is clearly non-structural based on its
    description, category, or material text.
    Never filter out a row if it matches any structural keyword.
    """
    combined = f"{desc} {cat} {mat}".lower()

    # If it matches a structural keyword → definitely keep
    if any(k in combined for k in _STRUCTURAL_KW):
        return False

    # If it matches a non-structural keyword → exclude
    return any(k in combined for k in _NON_STRUCTURAL_KW)
