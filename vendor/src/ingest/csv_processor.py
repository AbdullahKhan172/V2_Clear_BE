"""
CSV/Excel Processor - Parse Revit exports and Ready-format BOQs
================================================================
Supports:
  1. "Ready" format CSVs (with e_id, Volume, Mass columns)
  2. Raw Revit Material Takeoff exports (auto-detected and transformed)
"""

import os
import pandas as pd

from steel_sections import is_structural_steel_name
from catalogue import REBAR_EIDS as _REBAR_EIDS_LIST, STEEL_SECTION_EIDS as _SECTION_EIDS_LIST

REBAR_EIDS = set(_REBAR_EIDS_LIST)
STEEL_SECTION_EIDS = set(_SECTION_EIDS_LIST)


def _read_csv_ragged(file_path):
    """Read a CSV that may have rows of differing widths (short title/preamble
    rows above a wider data table) into a header-less DataFrame, padding every
    row to the widest. Uses the csv module so quoted commas are handled."""
    import csv as _csv
    for enc in ('utf-8-sig', 'latin-1'):
        try:
            with open(file_path, newline='', encoding=enc, errors='replace') as fh:
                rows = list(_csv.reader(fh))
            break
        except Exception:
            rows = []
    if not rows:
        return pd.DataFrame()
    width = max(len(r) for r in rows)
    rows = [r + [''] * (width - len(r)) for r in rows]
    df = pd.DataFrame(rows)
    return df.replace('', pd.NA)


def load_file(file_path):
    """Load CSV or Excel file into a DataFrame, skipping any preamble title rows."""
    import re as _re
    ext = os.path.splitext(file_path)[1].lower()

    _header_kw = {
        'description', 'desc', 'element', 'item', 'name', 'type',
        'vol', 'volume', 'qty', 'quantity', 'count', 'no.', 'nr',
        'mass', 'weight', 'area', 'length', 'level', 'storey',
        'material', 'category', 'cat', 'grade', 'rebar', 'steel',
        'floor', 'section', 'ref', 'mark', 'id', 'family',
    }

    def _find_header(raw):
        for i, row in raw.iterrows():
            vals = [str(v).strip().lower() for v in row if pd.notna(v) and str(v).strip()]
            if len(vals) >= 2 and any(any(kw in v for kw in _header_kw) for v in vals):
                return i
        return None

    if ext == '.csv':
        # Ragged-tolerant read: Revit/QS CSVs often have a short title row (fewer
        # commas than the data) which makes pandas infer too few columns and then
        # error/drop every wider data row. Read via the csv module and pad short
        # rows to the widest, so a narrow preamble never truncates the takeoff.
        raw = _read_csv_ragged(file_path)
        hi = _find_header(raw)
        if hi is None:
            hi = 0  # assume the first row is the header
        data = raw.iloc[hi + 1:].reset_index(drop=True)
        data.columns = raw.iloc[hi].tolist()
        data = data.loc[:, [c for c in data.columns if pd.notna(c) and str(c).strip()]]
        return data
    elif ext in ('.xlsx', '.xls'):
        raw = pd.read_excel(file_path, header=None)
        hi = _find_header(raw)
        if hi is not None and hi > 0:
            data = raw.iloc[hi + 1:].reset_index(drop=True)
            data.columns = raw.iloc[hi].tolist()
            data = data.loc[:, [c for c in data.columns if pd.notna(c) and str(c).strip()]]
            return data
        return pd.read_excel(file_path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")


def detect_format(df):
    """
    Detect whether the DataFrame is in 'ready' or 'revit_raw' format.

    Returns:
        str: 'ready' if it has e_id + Mass columns, 'revit_raw' otherwise
    """
    cols_lower = [c.lower().strip() for c in df.columns]

    has_eid = any('e_id' in c for c in cols_lower)
    has_mass = any('mass' in c for c in cols_lower)
    has_volume = any('volume' in c or 'vol' in c for c in cols_lower)

    if has_eid and (has_mass or has_volume):
        return 'ready'
    return 'revit_raw'


def process_ready_format(df):
    """
    Process a "Ready" format CSV/Excel that already has e_id, volumes, mass.

    Expected columns: e_id, Category, Material, Description, Volume, Waste,
                      Total Volume, Density, Mass, Count
    """
    # Normalize column names
    col_map = {}
    _used_targets = set()
    for c in df.columns:
        cl = c.lower().strip()
        if 'e_id' in cl:
            target = 'e_id'
        elif 'category' in cl or 'cat' in cl:
            target = 'Category'
        elif cl == 'material' or cl == 'mat':
            target = 'Material'
        elif 'description' in cl or 'desc' in cl:
            target = 'Description'
        elif 'total vol' in cl or 'total_vol' in cl:
            target = 'Total Volume(m3)'
        elif 'volume' in cl or 'vol' in cl:
            target = 'Volume(m3)'
        elif 'correction' in cl or 'formula' in cl:
            target = 'Correction Formula'
        elif 'waste' in cl:
            target = 'Waste'
        elif 'density' in cl or 'dens' in cl:
            target = 'Density(kg/m3)'
        elif 'mass' in cl or 'weight' in cl:
            target = 'Mass(kg)'
        elif 'count' in cl or 'qty' in cl or 'quantity' in cl:
            target = 'Count'
        else:
            continue
        # Map only the first column matching each target — avoids duplicate
        # column names (e.g. several "Volume" columns) that make row[col] return
        # a Series and crash downstream with "truth value ambiguous".
        if target in _used_targets:
            continue
        col_map[c] = target
        _used_targets.add(target)

    df = df.rename(columns=col_map)

    # Ensure required columns exist
    if 'e_id' not in df.columns:
        df['e_id'] = pd.NA  # Missing — will trigger "no e_id" path in app
    if 'Category' not in df.columns:
        df['Category'] = df.apply(lambda r: _infer_category(r.get('Description', ''), r.get('e_id', '')), axis=1)
    if 'Material' not in df.columns:
        df['Material'] = df['e_id'].apply(_infer_material_from_eid)

    if 'is_structural_steel' not in df.columns:
        eids = df['e_id'].astype(str).str.strip() if 'e_id' in df.columns else pd.Series('', index=df.index)
        desc = df.get('Description', pd.Series('', index=df.index)).astype(str)
        df['is_structural_steel'] = [
            _is_structural_steel_from_ready_row(eid, d)
            for eid, d in zip(eids, desc)
        ]

    # Calculate missing values
    # SEAI waste factors: concrete/rebar 5%, steel sections 1%, PT 1.5%
    if 'Waste' not in df.columns:
        def _default_waste(row):
            eid = str(row.get('e_id', '') or '').strip()
            if eid in STEEL_SECTION_EIDS:
                return 1.01
            if eid.startswith('PT_'):
                return 1.015
            return 1.05  # concrete + rebar
        df['Waste'] = df.apply(_default_waste, axis=1)
    if 'Density(kg/m3)' not in df.columns:
        df['Density(kg/m3)'] = df['Material'].apply(
            lambda m: 7850 if 'steel' in str(m).lower() else 2400
        )

    # Net volume convention (matches IFC path): Total Volume(m3) = Volume(m3),
    # waste is accounted for once in A5w only, never inflated into mass.
    if 'Volume(m3)' in df.columns:
        df['Volume(m3)'] = pd.to_numeric(df['Volume(m3)'], errors='coerce').fillna(0)
    if 'Total Volume(m3)' not in df.columns and 'Volume(m3)' in df.columns:
        df['Total Volume(m3)'] = df['Volume(m3)']
    # Reverse case: some ready BOQs supply only a "Total Volume" column (no plain
    # "Volume"). Derive Volume(m3) from it so the display and the engine — which
    # both read Volume(m3)/volume_m3 — see the quantities instead of zero.
    if 'Volume(m3)' not in df.columns and 'Total Volume(m3)' in df.columns:
        df['Volume(m3)'] = pd.to_numeric(df['Total Volume(m3)'], errors='coerce').fillna(0)

    if 'Mass(kg)' not in df.columns:
        df['Density(kg/m3)'] = pd.to_numeric(df['Density(kg/m3)'], errors='coerce').fillna(2400)
        df['Total Volume(m3)'] = pd.to_numeric(df['Total Volume(m3)'], errors='coerce').fillna(0)
        df['Mass(kg)'] = df['Total Volume(m3)'] * df['Density(kg/m3)']

    if 'Count' not in df.columns:
        df['Count'] = 1

    # Ensure numeric columns
    for col in ['Volume(m3)', 'Total Volume(m3)', 'Density(kg/m3)', 'Mass(kg)', 'Count', 'Waste']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

    # Per-row mass backfill: a row that has volume but a blank/zero Mass (e.g. a
    # concrete member where only the volume was filled in) must still get a mass,
    # or it contributes ZERO emissions. Steel rows quantified by mass have no
    # volume, so this never overwrites their supplied mass.
    if 'Mass(kg)' in df.columns and 'Volume(m3)' in df.columns:
        dens = (df['Density(kg/m3)'] if 'Density(kg/m3)' in df.columns
                else pd.Series(2400, index=df.index))
        need = (df['Mass(kg)'] <= 0) & (df['Volume(m3)'] > 0)
        df.loc[need, 'Mass(kg)'] = df.loc[need, 'Volume(m3)'] * dens.loc[need]
        if 'Total Volume(m3)' in df.columns:
            tneed = (df['Mass(kg)'] <= 0) & (df['Total Volume(m3)'] > 0)
            df.loc[tneed, 'Mass(kg)'] = df.loc[tneed, 'Total Volume(m3)'] * dens.loc[tneed]

    # Blank Count cells coerce to 0 above; a quantity row represents at least one
    # element, so treat 0/blank as 1.
    if 'Count' in df.columns:
        df.loc[df['Count'] <= 0, 'Count'] = 1

    # Mirror Volume(m3) → volume_m3 (lowercase) so the IFC-style engine reads it.
    # Without this, prepare_boq_from_ifc looks up 'volume_m3', gets 0, skips every row.
    if 'Volume(m3)' in df.columns:
        df['volume_m3'] = df['Volume(m3)']
    if 'Description' in df.columns and 'name' not in df.columns:
        df['name'] = df['Description']
    if 'Category' in df.columns and 'category' not in df.columns:
        df['category'] = df['Category']
    if 'Count' in df.columns and 'count' not in df.columns:
        df['count'] = df['Count']
    if 'is_steel' not in df.columns:
        # Recognise steel broadly so every rebar / steel-section / PT row is flagged
        # (and therefore gets a steel emission-factor dropdown and classifies right),
        # regardless of how the Material column is worded — "Steel", "Steel Section",
        # "Structural Steel", "Reinforcement", "PT", "Tendon", etc. — or by its e_id.
        _matl = df['Material'].astype(str).str.lower()
        _eidu = (df['e_id'].astype(str).str.upper().str.strip()
                 if 'e_id' in df.columns else pd.Series('', index=df.index))
        _steel_kw  = _matl.str.contains(
            r'steel|rebar|reinforc|tendon|strand|post.?tension|section|\bpt\b',
            regex=True, na=False)
        _steel_eid = _eidu.str.startswith('PT_') | _eidu.isin(
            {e.upper() for e in (REBAR_EIDS | STEEL_SECTION_EIDS)})
        _is_conc   = _matl.str.contains('concrete|cement', na=False) | _eidu.str.startswith('C_')
        df['is_steel'] = (_steel_kw | _steel_eid) & ~_is_conc

    return df


def _parse_revit_volume(raw_val) -> float:
    """Extract a numeric m³ value from a Revit cell that may contain units like '461.45 m³'."""
    import re as _re
    if pd.isna(raw_val):
        return 0.0
    s = str(raw_val).strip()
    # Strip common unit suffixes: m³, m3, m^3, m ³, cubic metres, etc.
    s = _re.sub(r'(?i)\s*(m[³3^]|cubic\s*m(etre|eter)?s?)\s*$', '', s).strip()
    # Strip thousands separators and normalise decimal
    s = s.replace(',', '')
    try:
        return float(s)
    except ValueError:
        return 0.0


def process_revit_raw(df):
    """
    Process a raw Revit Material Takeoff export.

    Typical Revit columns:
      Family and Type, Material: Description/Name, Material: Volume, Count
    """
    # Normalize column names — check volume BEFORE material so that
    # "Material: Volume" maps to volume_raw, not material_desc
    col_map = {}
    _used_targets = set()
    for c in df.columns:
        cl = c.lower().strip()
        if 'family' in cl and 'type' in cl:
            target = 'family_type'
        elif 'volume' in cl:
            target = 'volume_raw'
        elif 'material' in cl:
            target = 'material_desc'
        elif 'count' in cl:
            target = 'count'
        elif 'area' in cl:
            target = 'area'
        elif 'length' in cl:
            target = 'length'
        else:
            continue
        # Map only the first column matching each target. Without this, files
        # with several volume columns (e.g. Total/Used/Excluded Volume) all map
        # to 'volume_raw', producing duplicate column names — then row[col]
        # returns a Series and pd.isna()/float() raise "truth value ambiguous".
        if target in _used_targets:
            continue
        col_map[c] = target
        _used_targets.add(target)

    df = df.rename(columns=col_map)

    rows = []
    skipped_zero = 0
    for _, row in df.iterrows():
        ft = str(row.get('family_type', ''))
        mat = str(row.get('material_desc', ''))
        vol = _parse_revit_volume(row.get('volume_raw', 0))
        count_raw = pd.to_numeric(row.get('count', 1), errors='coerce')
        # NaN check — pd.to_numeric returns NaN on bad input, and NaN is truthy
        # so `or 1` does not catch it. int(NaN) would crash.
        count = 1 if pd.isna(count_raw) else max(int(count_raw), 1)

        if vol <= 0:
            skipped_zero += 1
            continue

        category = _parse_revit_category(ft)
        description = ft
        # Material column is authoritative when it names a material; but Revit
        # takeoffs often give a generic/blank material for a steel section, so
        # also trust a recognised section name in Family & Type (UB/UC/…, and the
        # ASB/SFB/IFB Slimflor family). Don't override an explicit "concrete".
        is_steel = _is_steel_material(mat)
        if (not is_steel and 'concrete' not in mat.lower() and 'conc' not in mat.lower()
                and (is_structural_steel_name(ft) or _is_steel_material(ft))):
            is_steel = True
        is_structural_steel = is_steel and _is_structural_steel_from_text(ft, mat)

        # Material label: Steel / Concrete / Unknown. A non-steel row is only
        # called Concrete when the material or type actually reads as concrete;
        # otherwise (generic/blank Revit material, e.g. an "Other" element) it is
        # left Unknown so the UI offers the full-database factor picker rather
        # than defaulting it to a concrete grade.
        if is_steel:
            mat_label = 'Steel'
        elif category == 'Other':
            mat_label = 'Unknown'   # unclassified element → general full-DB picker
        else:
            mat_label = 'Concrete'  # a real structural category → concrete + rebar

        # All steel: volume × 7850 (no kg/m CSV lookup needed)
        # SEAI waste factors: structural steel sections 1%, rebar 5%, concrete 5%
        density = 7850 if is_steel else 2400
        if is_steel:
            waste = 1.01 if is_structural_steel else 1.05
        else:
            waste = 1.05
        # Net mass — waste accounted for in A5w only, NOT inflated into mass.
        # (Matches IFC path convention.)
        mass = vol * density

        rows.append({
            'category': category,
            'Category': category,
            'Material': mat_label,
            'Description': description,
            'volume_m3': vol,
            'Volume(m3)': vol,
            'Correction Formula': '',
            'Waste': waste,
            'Total Volume(m3)': vol,
            'Density(kg/m3)': density,
            'Mass(kg)': mass,
            'Count': count,
            'count': count,
            'name': ft,
            'family': ft,
            'level': '',
            'is_steel': is_steel,
            'is_structural_steel': is_structural_steel,
            'section_name': '',
            'section_kg_per_m': 0.0,
            'section_status': '',
            'length_m': 0.0,
            'length_source': '',
        })

    result = pd.DataFrame(rows)
    if skipped_zero > 0:
        print(f"[csv_processor] WARNING: {skipped_zero} row(s) skipped — zero or missing volume. "
              f"Check for unit mismatch in the Revit export (volumes should be in m³).")
    return result


def process_file(file_path):
    """
    Main entry point: load and process any supported file format.

    Returns:
        tuple: (DataFrame, format_type_str)
    """
    df = load_file(file_path)

    # ── Stacked / long-format BOQ (one row per material, mixed-unit column) ──
    # Additive pre-check: if the sheet is a stacked layout (e.g. a "Category"
    # column whose values are material+unit labels like "Concrete (m³)" /
    # "Rebar (kg)" with a single mixed-unit quantity column), pivot it via the
    # universal parser. Returns None for every conventional file, so the existing
    # ready/revit_raw detection below is unaffected.
    try:
        from boq_parser import (_detect_stacked_layout, _normalise_stacked_boq,
                                 _rule_map_column)
        roles = {c: _rule_map_column(str(c).lower().strip()) for c in df.columns}
        stacked = _detect_stacked_layout(df, roles)
        if stacked is not None:
            structural_df, _ = _normalise_stacked_boq(df, stacked)
            if structural_df is not None and len(structural_df) > 0:
                return structural_df, 'scratch'
    except Exception as exc:
        print(f"[csv_processor] Stacked-layout check skipped: {exc}")

    fmt = detect_format(df)

    if fmt == 'ready':
        return process_ready_format(df), 'ready'

    # Structured BOQ with an explicit Category column but no e_id (so it failed
    # the 'ready' test) must NOT be forced down the Revit path: process_revit_raw
    # only reads a "Family and Type" column for the category, so without one every
    # row collapses to 'Other' and mass-only steel rows (no volume) get dropped.
    # Route such files through the universal parser, which honours the Category /
    # Description / Material columns and derives steel volume from mass.
    cols_lower = [str(c).lower().strip() for c in df.columns]
    has_family_type = any('family' in c and 'type' in c for c in cols_lower)
    has_category    = any(c == 'cat' or c.startswith('categor') for c in cols_lower)
    if has_category and not has_family_type:
        try:
            from boq_parser import parse_universal_boq
            result = parse_universal_boq(file_path)
            structural_df = result.get('structural_df')
            if structural_df is not None and len(structural_df) > 0:
                return structural_df, 'scratch'
        except Exception as exc:
            print(f"[csv_processor] Universal-parser route failed, "
                  f"falling back to Revit path: {exc}")

    return process_revit_raw(df), 'revit_raw'


# ── Private helpers ─────────────────────────────────────────────────────────

_REVIT_COLUMN_KW = [
    # Standard
    'column', 'col', 'cols', 'colmn', 'coloumn', 'colum', 'collumn',
    'columnn', 'coulmn', 'colm', 'clmn',
    # UK/Ireland QS abbreviations
    'c/col', 'rc col', 'rc column', 'r.c. col', 'r.c. column',
    'str column', 'structural column', 'concrete column', 'steel column',
    'circular column', 'round column', 'square column', 'rect column',
    'rectangular column', 'composite column', 'precast column',
    # Taxonomy: composite/specialist column types (E025–E030, E032–E034)
    'composite steel-concrete column', 'encased column', 'cft column',
    'concrete-filled tube', 'cfs column', 'light gauge column',
    'modular column', 'glulam column', 'clt column', 'timber column',
    'masonry column', 'brick column', 'blockwork column',
    # Revit family name prefixes used by UK engineers
    'concrete-rectangular-column', 'concrete-round-column',
    'concrete-square-column', 'concrete-circular-column',
    'm_concrete-rectangular-column', 'm_concrete-round-column',
    # NBS / spec codes
    'e20', 'e30',
    # Size-based naming (e.g. "300x300 col", "c1", "c2")
    'col c', 'rc c', '×column', 'xcolumn',
    # Common drafter shorthand
    'stanchion', 'post', 'upright', 'pier column', 'column pier',
]
_REVIT_BEAM_KW = [
    # Standard
    'beam', 'girder', 'bm', 'bmr', 'beams', 'beamr', 'beaam', 'beem',
    'beaem', 'grider', 'gder',
    # UK/Ireland QS
    'grd beam', 'grade beam', 'transfer beam', 'transfer bm',
    'lintel', 'lintol', 'lintle', 'lnl', 'lintels',
    'ground beam', 'grd bm', 'g.beam', 'g beam',
    'rc beam', 'r.c. beam', 'steel beam', 'concrete beam',
    'precast beam', 'str beam', 'structural beam',
    'secondary beam', 'primary beam', 'main beam', 'sub beam', 'sub-beam',
    'cantilever', 'cantlever', 'cantiliver', 'canti', 'cant beam',
    'spandrel beam', 'spandrel bm',
    'ring beam', 'ring bm', 'tie beam', 'tie bm',
    'coupling beam', 'coupling bm',
    'upstand beam', 'upstand', 'downstand beam', 'downstand',
    'edge beam', 'edge bm', 'perimeter beam',
    # Revit family names
    'concrete-rectangular beam', 'm_concrete-rectangular beam',
    'concrete beam-rectangular', 'm_concrete beam-rectangular',
    'slab support beam',
    # Steel section beams
    'ub ', ' ub', 'universal beam', 'rafter', 'purlin', 'truss',
    # Taxonomy: specialist beam types (E043–E058)
    'transfer truss', 'vierendeel', 'castellated beam', 'cellular beam',
    'outrigger beam', 'belt truss', 'outrigger truss',
    'glulam beam', 'lvl beam', 'clt beam', 'timber beam',
    'tcc beam', 'timber-concrete composite beam',
    'hip rafter', 'valley rafter', 'mono-pitch beam', 'lean-to beam',
    # NBS codes
    'e05', 'g10', 'g12',
]
_REVIT_SLAB_KW = [
    # Standard
    'floor', 'slab', 'flr', 'slb', 'flooor', 'slabb', 'flore', 'slaab',
    'deck', 'plate',
    # UK/Ireland QS — slab types
    'mat slab', 'raft slab', 'ground slab', 'transfer slab', 'transfer plate',
    'hollowcore', 'hollow core', 'hc slab', 'h.c. slab',
    'waffle', 'waffle slab', 'waffle floor', 'coffered', 'coffered slab',
    'ribbed slab', 'rib slab', 'one-way rib', 'two-way rib',
    'composite deck', 'composite slab', 'metal deck', 'metal decking',
    'precast slab', 'precast floor', 'precast plank', 'precast plank floor',
    'podium slab', 'podium deck',
    'ground floor slab', 'gf slab', 'g.f. slab',
    'basement slab', 'b1 slab', 'b2 slab',
    'rc slab', 'r.c. slab', 'concrete slab',
    'flat slab', 'two-way slab', 'two way slab', 'one-way slab', 'one way slab',
    'solid slab', 'bubble deck', 'void slab', 'voided slab', 'cobiax',
    'post-tensioned floor', 'pt floor', 'pt slab', 'p.t. slab',
    'topping slab', 'structural topping', 'topping', 'screed topping',
    'suspended slab', 'elevated slab',
    # Revit floor family names used in UK
    'basic floor', 'generic floor', 'concrete floor',
    'm_floor', 'floor:', 'floor - ',
    # NBS codes
    'e10', 'e20', 'e40',
    # Taxonomy: timber/composite floor types (E078–E085)
    'clt floor', 'clt slab', 'cross laminated timber floor',
    'tcc floor', 'tcc slab', 'timber-concrete composite floor',
    'glulam floor', 'timber floor', 'mass timber floor',
    'cobiax', 'bubble deck', 'void former slab',
    # Size-coded slab names (e.g. "200thk slab", "250 rc flat slab")
    'thk slab', 'thk floor', 'thk rc', 'rc flat slab', 'flat plate',
]
_REVIT_WALL_KW = [
    # Standard
    'wall', 'wl', 'wll', 'wal', 'walll', 'wwall',
    # UK/Ireland QS structural wall types
    'shear wall', 'shear wl', 'core wall', 'core wl',
    'retaining wall', 'ret wall', 'ret. wall', 'retaining wl',
    'basement wall', 'bsmt wall',
    'rc wall', 'r.c. wall', 'concrete wall', 'reinforced wall',
    'structural wall', 'str wall', 'str wl', 'load bearing wall',
    'loadbearing wall', 'load-bearing wall', 'lb wall', 'l/b wall',
    'sw', 's/wall', 'lateral wall',
    'diaphragm wall', 'diafragm wall', 'd-wall', 'dwall',
    'piled wall', 'sheet pile wall', 'sheet piling', 'sheet pile',
    'secant wall', 'secant pile wall', 'contiguous wall', 'contiguous bore pile',
    'precast wall', 'tilt-up', 'tiltup',
    'party wall', 'parapet', 'parapett', 'upstand wall',
    'wing wall', 'abutment',
    # Revit wall family names
    'basic wall', 'generic wall', 'concrete wall:',
    # NBS codes
    'e20', 'e30', 'e60',
    # Thickness-coded names (e.g. "150mm rc wall", "300 shear wall")
    'thk wall', 'thk rc wall', 'mm rc wall', 'mm wall',
    'basic wall: ', 'rc wall:', 'in-situ concrete wall',
    # Taxonomy: masonry/timber/specialist wall types (E095–E105)
    'loadbearing masonry wall', 'load bearing masonry', 'masonry wall',
    'confined masonry', 'reinforced masonry wall',
    'clt wall', 'cross laminated timber wall', 'timber frame wall',
    'tilt-up', 'tiltup', 'tilt up panel',
    'modular wall', 'volumetric wall',
]
_REVIT_FOUNDATION_KW = [
    # Standard
    'footing', 'foundation', 'raft', 'ftg', 'fdn', 'fdtn', 'fndn', 'ftng',
    # Common misspellings
    'foundtion', 'foundatin', 'founation', 'foting', 'footng', 'footig',
    'foundat', 'foundatn',
    # UK/Ireland QS types
    'pad', 'pad footing', 'pad fdn',
    'strip footing', 'strip fdn', 'strip foundation', 'continuous strip',
    'pile cap', 'pilecap', 'pile cap slab',
    'mat foundation', 'mat fdn', 'raft foundation', 'raft fdn',
    'spread footing', 'isolated footing', 'combined footing',
    'pile foundation', 'deep foundation', 'shallow foundation',
    'rc footing', 'concrete footing', 'rc fdn', 'r.c. fdn',
    'ground beam', 'grd beam', 'tie beam', 'grade beam',
    # Taxonomy: additional foundation types (E018–E024)
    'underpinning', 'underpinn',
    'blinding concrete', 'blinding slab', 'blinding layer',
    'basement slab', 'ground-bearing slab', 'ground bearing slab',
    'gabion wall', 'gabion retaining', 'mass retaining wall',
    'stone retaining wall', 'masonry retaining wall',
    'thrust block',
    # NBS codes
    'd20', 'd30', 'd40',
]
_REVIT_PILE_KW = [
    # Standard
    'pile', 'piles', 'plle', 'pille',
    # UK/Ireland pile types
    'bored pile', 'cfa pile', 'cfa', 'continuous flight auger',
    'driven pile', 'driven precast', 'driven cast-in-situ',
    'micropile', 'micro pile', 'mini pile', 'minipile',
    'caisson', 'large diameter pile',
    'helical pile', 'screw pile', 'ground screw',
    'auger pile', 'augered pile',
    'friction pile', 'end bearing pile',
    'precast pile', 'precast concrete pile',
    'steel pile', 'steel h-pile', 'h pile',
    'spun pile', 'spun concrete pile',
    'secant pile', 'contiguous pile',
    'drilled shaft', 'drilled pier',
    # NBS codes
    'd30', 'd31',
]
_REVIT_STAIR_KW = [
    # Standard
    'stair', 'stairs', 'staircase', 'stairway',
    # Common misspellings
    'staris', 'starir', 'steair', 'stairflight', 'stairflght', 'stacase',
    # UK/Ireland QS
    'flight', 'stair flight', 'stair slab', 'stair waist', 'waist slab',
    'landing', 'stair landing',
    'step', 'steps', 'going', 'riser',
    'escape stair', 'fire stair', 'fire escape', 'protected stair',
    'external stair', 'stairwell', 'stair core',
    'precast stair', 'precast staircase', 'precast flight',
    # Revit
    'assembled stair', 'cast-in-place stair',
]
_REVIT_ROOF_KW = [
    # Standard
    'roof', 'rooof', 'rooff', 'roff', 'roofing',
    # UK/Ireland QS roof structure
    'roof slab', 'roof beam', 'roof deck', 'roof plate',
    'terrace slab', 'terrace', 'roof terrace',
    'overhead slab', 'overhead beam',
    'flat roof', 'flat roof slab',
    'pitched roof', 'roof structure', 'roof framing',
    'rc roof', 'r.c. roof', 'concrete roof',
    # Plant rooms / penthouses
    'plant room slab', 'plant slab', 'penthouse slab',
    # Taxonomy: specialist roof structures (E128–E134)
    'space frame roof', 'geodesic', 'dome frame',
    'etfe roof', 'membrane roof', 'tensile roof',
    'hip rafter', 'valley rafter', 'hip roof structure',
    'mono-pitch roof', 'lean-to roof', 'mono pitch',
]
_REVIT_RAMP_KW = [
    # Standard
    'ramp', 'rampp', 'raamp',
    # UK/Ireland QS
    'car ramp', 'vehicle ramp', 'parking ramp', 'spiral ramp',
    'sloped slab', 'inclined slab', 'access ramp',
    'loading ramp', 'service ramp',
]

# Taxonomy: Lateral system elements (E135–E143)
_TAXONOMY_LATERAL_KW = [
    'bracing', 'brace', 'x brace', 'k brace', 'chevron brace',
    'cross brace', 'diagonal brace', 'lateral brace',
    'buckling restrained brace', 'brb',
    'moment connection', 'gusset plate', 'gusset',
    'base isolator', 'seismic isolator', 'lead rubber bearing',
    'viscous damper', 'fluid damper', 'hydraulic damper',
    'tuned mass damper', 'tmd',
    'outrigger', 'belt truss', 'outrigger truss', 'outrigger wall',
    'knee brace', 'timber haunch', 'steel knee brace',
    'lateral system', 'lateral frame', 'stability system',
    'wind bracing', 'seismic bracing',
]

# Taxonomy: External structural elements (E155–E160)
_TAXONOMY_EXTERNAL_KW = [
    'crash barrier', 'vehicle barrier', 'safety barrier',
    'parapet', 'parapett', 'parapet wall', 'parapet beam',
    'external stair', 'fire escape stair', 'fire escape',
    'canopy', 'porte-cochere', 'porte cochere', 'canopy frame',
    'canopy beam', 'canopy column',
    'mast', 'flagpole', 'flag pole', 'mast structure',
    'thrust block',
    'retaining wall (gravity)', 'gravity retaining wall',
    'external structure', 'external frame',
]


def _parse_revit_category(family_type_str):
    """Infer structural category from Revit Family and Type string."""
    ft = family_type_str.lower()
    if any(kw in ft for kw in _REVIT_PILE_KW):
        return 'Pile'
    if any(kw in ft for kw in _REVIT_FOUNDATION_KW):
        return 'Foundation/Footing'
    if any(kw in ft for kw in _REVIT_COLUMN_KW):
        return 'Column'
    if any(kw in ft for kw in _REVIT_BEAM_KW):
        return 'Beam'
    if any(kw in ft for kw in _REVIT_SLAB_KW):
        return 'Slab/Floor'
    if any(kw in ft for kw in _REVIT_WALL_KW):
        return 'Wall'
    if any(kw in ft for kw in _REVIT_STAIR_KW):
        return 'Stair'
    if any(kw in ft for kw in _REVIT_ROOF_KW):
        return 'Roof'
    if any(kw in ft for kw in _REVIT_RAMP_KW):
        return 'Ramp'
    if any(kw in ft for kw in _TAXONOMY_LATERAL_KW):
        return 'Lateral System'
    if any(kw in ft for kw in _TAXONOMY_EXTERNAL_KW):
        return 'External Structure'
    return 'Other'


def _is_steel_material(material_str):
    """Check if the material description indicates steel — UK/Ireland QS terminology."""
    ml = material_str.lower()
    return any(k in ml for k in [
        # Generic
        'steel', 'rebar', 'reinforcement', 'metal', 'iron',
        # Common abbreviations & misspellings
        'rebr', 'reinf', 'rienf', 'reinft', 'reinfo',
        # UK designations — only as standalone material labels, not as part of "RC slab" etc.
        # These are matched against the material column, not the description column
        'r.c. steel', 'r/c steel',
        'mild steel', 'ms bar', 'ms rod',
        'high yield', 'hy ', 'h.y.', 'hy bar',
        'deformed bar', 'def bar', 't bar', 't10', 't12', 't16', 't20', 't25', 't32',
        'y bar', 'y10', 'y12', 'y16', 'y20', 'y25', 'y32',
        'b500', 'b500b', 'grade 500', '500b', 'grade b',
        # Structural sections
        'ub ', ' ub', 'uc ', ' uc', 'chs', 'rhs', 'shs', 'pfc',
        'universal beam', 'universal column', 'hollow section',
        'structural steel', 'str steel', 'fabricated steel',
        # Slimflor / shallow-floor steel beams (ASB/SFB/IFB/Slimdek)
        'asb', 'sfb', 'ifb', 'slimflor', 'slimdek',
        # Post-tensioning
        'tendon', 'strand', 'pt ', 'p.t.', 'post-tension', 'post tension',
        # Surface treatment (still steel)
        'galvanised', 'galvanized', 'stainless', 'hdg', 'hot dip',
        # Mesh
        'mesh', 'fabric', 'a142', 'a193', 'a252', 'a393',
        'b196', 'b283', 'b385', 'b503', 'b785', 'b1131',
        # Misc
        'ferrule', 'coupler', 'mechanical splice',
    ])


def _is_structural_steel_from_text(name_text, material_text=''):
    """Classify steel as structural section only if it matches section keywords."""
    txt = f"{name_text} {material_text}".lower()
    rebar_like = any(k in txt for k in [
        'rebar', 'reinforcement', 'reinforcing', 'mesh',
        'tendon', 'strand', 'pt ', 'post-tension', 'post tension',
    ])
    if rebar_like:
        return False
    return is_structural_steel_name(name_text)


def _is_structural_steel_from_ready_row(eid, description):
    eid = str(eid).strip()
    if eid in STEEL_SECTION_EIDS:
        return True
    if eid in REBAR_EIDS or eid.startswith('PT_'):
        return False
    if eid.startswith('R_'):
        return _is_structural_steel_from_text(description, '')
    return False


def _infer_category(desc, e_id):
    """Infer category from description or e_id."""
    if e_id:
        eid = str(e_id).strip()
        if eid.startswith('PT_'):
            return 'Post-Tensioning'
    text = str(desc).lower()
    return _parse_revit_category(text)


def _infer_material_from_eid(e_id):
    """Infer material type from e_id prefix."""
    eid = str(e_id).strip()
    if eid.startswith('C_'):
        return 'Concrete'
    elif eid.startswith('R_'):
        return 'Steel'
    elif eid.startswith('PT_'):
        return 'Post Tensioning'
    return 'Concrete'
