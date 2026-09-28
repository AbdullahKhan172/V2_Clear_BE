"""
Web App Helpers — Factor Catalogues, Steel Classification, Transport Distances
================================================================================
Pure business-logic helpers used by web_app.py's routes, extracted verbatim
(no Flask coupling — none of these touch `request`/`session`/`app`, they all
take explicit arguments) so web_app.py itself stays focused on routing.
"""

import re

from catalogue import (
    EmissionCatalogue, REBAR_EIDS, STEEL_SECTION_EIDS,
    DEFAULT_STEEL_EID, DEFAULT_STEEL_SECTION_EID, DEFAULT_PT_EID,
)
from calculations import SEAI_TRANSPORT_DEFAULTS
from steel_rates import STEEL_RATES


def _default_steel_rates(categories: list[str]) -> dict:
    """Return default rebar kg/m³ rate for each element category."""
    rates = {}
    for cat in categories:
        info = STEEL_RATES.get(cat)
        if info:
            rates[cat] = info['default']
        else:
            # Try partial match
            for key, val in STEEL_RATES.items():
                if key.lower() in cat.lower() or cat.lower() in key.lower():
                    rates[cat] = val['default']
                    break
            else:
                rates[cat] = 150  # fallback
    return rates


def _pt_applicable(category: str, is_steel: bool) -> bool:
    """Whether post-tensioning can apply to this element.

    PT is a flexural/prestressing technique applied to concrete slabs, floors
    and beams, and also — less commonly — to walls and raft (mat) foundations.
    Columns, piles, pad/strip footings, stairs and any steel member cannot be
    post-tensioned, so the PT-rate input is hidden (shown as n/a) for them.
    """
    if is_steel:
        return False
    c = (category or '').lower()
    return any(k in c for k in ('slab', 'floor', 'beam', 'wall', 'raft'))


_STEEL_FACTOR_CACHE = None

def _steel_factor_catalog():
    """Grouped steel emission factors (Rebar / Steel Section / Post Tensioning)
    with human-readable names from the emission database — so the UI can offer a
    named dropdown instead of raw e_id codes. Also returns an e_id→name map."""
    global _STEEL_FACTOR_CACHE
    if _STEEL_FACTOR_CACHE is not None:
        return _STEEL_FACTOR_CACHE
    groups = {'Rebar': [], 'Steel Section': [], 'Post Tensioning': []}
    names = {}
    buckets = {}   # {e_id: bucket} so the whole app classifies every steel factor
    try:
        df = EmissionCatalogue().get_raw_dataframe()
        def _nm(eid):
            if eid in df.index:
                n = str(df.loc[eid, 'mat_name']).strip()
                if n and n.lower() != 'nan':
                    return n
            return eid
        _rebar_set = set(REBAR_EIDS)
        _sect_set  = set(STEEL_SECTION_EIDS)
        _cat = EmissionCatalogue()
        _products = []   # non-structural sheet/coil/pipe products — listed last
        for i in df.index:
            e = str(i)
            item = str(df.loc[i, 'item_name']).lower()
            mat  = str(df.loc[i, 'mat_name']).lower()
            if e.startswith('PT_') or 'tension' in item:
                bucket = 'Post Tensioning'
            elif e.startswith('R_') or 'steel' in item:
                if e in _rebar_set or any(k in mat for k in ('reinforc', 'rebar', 'world average')):
                    bucket = 'Rebar'
                else:
                    bucket = 'Steel Section'   # sections, plate, coil, engineering, "Section", etc.
            else:
                continue
            # Structural members first; sheet/coil/pipe products (tinplate,
            # electrogalvanized coil, …) are still selectable but sink to the
            # bottom of the dropdown with an explicit label so nobody prices a
            # UC column as tinplate by accident.
            nm = _nm(e)
            if bucket == 'Steel Section' and not _cat.factor_structural_use(e):
                nm = f"{nm} (sheet/coil product)"
                _products.append({'e_id': e, 'name': nm})
            else:
                groups[bucket].append({'e_id': e, 'name': nm})
            names[e] = nm
            buckets[e] = bucket
        groups['Steel Section'].extend(_products)
    except Exception as exc:
        print(f"[web_app] steel factor catalog build failed: {exc}")
    _STEEL_FACTOR_CACHE = {'groups': groups, 'names': names, 'buckets': buckets}
    return _STEEL_FACTOR_CACHE


_FULL_FACTOR_CACHE = None

def _full_factor_catalog():
    """Every emission factor in the database as [{e_id, name, group}], grouped by
    Concrete / Steel / Post Tensioning / Other — used for the full-database
    dropdown on Unknown/Other elements so the user can assign any factor rather
    than being forced onto the default concrete grade."""
    global _FULL_FACTOR_CACHE
    if _FULL_FACTOR_CACHE is not None:
        return _FULL_FACTOR_CACHE
    items = []
    try:
        _cat = EmissionCatalogue()
        df = _cat.get_raw_dataframe()
        for i in df.index:
            e = str(i)
            nm = str(df.loc[i, 'mat_name']).strip()
            if not nm or nm.lower() == 'nan':
                nm = e
            # Group by the catalogue's own family so precast / blockwork /
            # sheet-product entries are visible as their own sections instead
            # of hiding inside one flat "Concrete"/"Steel" list.
            fam = _cat.factor_family(e)
            if e.startswith('C_'):
                grp = {'precast': 'Concrete — Precast',
                       'block':   'Concrete — Blocks & Walls'}.get(fam, 'Concrete — In-situ')
            elif e.startswith('PT_'):
                grp = 'Post Tensioning'
            elif e.startswith('R_'):
                grp = ('Steel — Structural' if _cat.factor_structural_use(e)
                       else 'Steel — Other Products')
            elif e.startswith('T_'):
                grp = 'Timber / Mass Timber'   # IGBC factors — carry sequestration
            else:
                grp = 'Other'
            try:
                _v = float(df.loc[i, 'carbon_a1_a3_kgco2e_kg'])
                if _v != _v:   # NaN
                    _v = 0.0
            except (KeyError, ValueError, TypeError):
                _v = 0.0
            items.append({'e_id': e, 'name': nm, 'group': grp,
                          'inc_reinf': _cat.factor_includes_reinforcement(e),
                          'a1a3': _v})
    except Exception as exc:
        print(f"[web_app] full factor catalog build failed: {exc}")
    _FULL_FACTOR_CACHE = items
    return _FULL_FACTOR_CACHE


# UK open sections encode their linear weight as the last number of the
# designation (UC 203x203x46 → 46 kg/m; "203 UC 46" → 46). Display-only hint so
# engineers can sanity-check modelled steel mass against the section weight.
_SECTION_WT_RE1 = re.compile(
    r'(?:ub|uc|ubp|ukb|ukc|pfc|ukpfc)\s*-?\s*\d{2,4}\s*[x×]\s*\d{2,4}\s*[x×]\s*'
    r'(\d{1,3}(?:\.\d+)?)\b', re.I)
_SECTION_WT_RE2 = re.compile(
    r'\b\d{2,4}\s*(?:ub|uc|pfc)\s*[- ]?\s*(\d{1,3}(?:\.\d+)?)\s*(?:kg)?\b', re.I)

def _kg_per_m_from_name(name) -> float:
    t = str(name or '')
    m = _SECTION_WT_RE1.search(t) or _SECTION_WT_RE2.search(t)
    try:
        return float(m.group(1)) if m else 0.0
    except (TypeError, ValueError):
        return 0.0


def _factor_type(eid: str, is_steel: bool, is_struct: bool, text: str = '') -> str:
    """Classify a BOQ row's emission-factor bucket (Rebar / Steel Section /
    Post Tensioning) so the UI can offer the right A1-A3 override and the dashboard
    labels it correctly. Falls back to text hints when the e_id is blank/unknown
    (e.g. scratch BOQs with no e_id column)."""
    if not is_steel:
        return 'Concrete'
    e = str(eid or '').strip()
    if e.startswith('PT_'):
        return 'Post Tensioning'
    # Catalogue-derived bucket covers every steel factor (e.g. R_019 = "Section")
    _bk = _steel_factor_catalog()['buckets'].get(e)
    if _bk:
        return _bk
    if e in STEEL_SECTION_EIDS:
        return 'Steel Section'
    if e in REBAR_EIDS:
        return 'Rebar'
    # Blank / unknown e_id → infer from material/description/category text
    t = f' {str(text).lower()} '
    if any(k in t for k in ('post-tension', 'post tension', 'tendon', 'strand', ' pt ', ' pt')):
        return 'Post Tensioning'
    if any(k in t for k in ('rebar', 'reinforc', 'mesh')):
        return 'Rebar'
    if is_struct or any(k in t for k in ('section', 'beam', 'column', ' ub', ' uc', 'rhs', 'shs',
                                         'chs', 'plate', 'angle', 'channel', 'stanchion', 'girder')):
        return 'Steel Section'
    return 'Rebar'


# Default catalogue e_id for each steel bucket — used to seed a blank steel row so
# its dropdown pre-selects sensibly and it calculates as the right material.
_BUCKET_DEFAULT_EID = {
    'Rebar': DEFAULT_STEEL_EID,
    'Steel Section': DEFAULT_STEEL_SECTION_EID,
    'Post Tensioning': DEFAULT_PT_EID,
}


def _build_distances(form: dict) -> dict:
    """Per-material transport distances the engine routes by (SEAI Table 2).

    Reads the Step-4 per-material inputs (form['transport_distances'] holding
    t_<family>_sea / t_<family>_road) and falls back to the SEAI default for any
    family the UI omits. Legacy concrete/steel/pt keys are still accepted (older
    payloads) and are also emitted for any report code that reads them.
    """
    td = form.get('transport_distances') or {}

    def _f(val, default):
        try:
            return float(val)
        except (TypeError, ValueError):
            return float(default)

    # Legacy flat keys (older UI / desktop) → their primary family.
    _legacy = {'in_situ': ('conc_sea', 'conc_road'),
               'section': ('steel_sea', 'steel_road'),
               'strand':  ('pt_sea', 'pt_road')}

    out = {}
    for fam, (droad, dsea) in SEAI_TRANSPORT_DEFAULTS.items():
        sea_v = td.get(f't_{fam}_sea')
        road_v = td.get(f't_{fam}_road')
        if sea_v is None and fam in _legacy:
            sea_v = form.get(_legacy[fam][0])
        if road_v is None and fam in _legacy:
            road_v = form.get(_legacy[fam][1])
        out[f'{fam}_sea_distance'] = _f(sea_v, dsea)
        out[f'{fam}_road_distance'] = _f(road_v, droad)

    # Legacy group aliases (concrete=in_situ, steel=section, pt=strand).
    out['concrete_sea_distance'] = out['in_situ_sea_distance']
    out['concrete_road_distance'] = out['in_situ_road_distance']
    out['steel_sea_distance'] = out['section_sea_distance']
    out['steel_road_distance'] = out['section_road_distance']
    out['pt_sea_distance'] = out['strand_sea_distance']
    out['pt_road_distance'] = out['strand_road_distance']
    return out
