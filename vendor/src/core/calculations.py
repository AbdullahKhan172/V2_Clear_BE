import pandas as pd
from utils import safe_float_convert
from steel_sections import is_structural_steel_name
from catalogue import (DEFAULT_STEEL_EID, DEFAULT_STEEL_SECTION_EID,
                       DEFAULT_PT_EID, DEFAULT_CONCRETE_EID)
from catalogue import REBAR_EIDS as _REBAR_EIDS_LIST, STEEL_SECTION_EIDS as _SECTION_EIDS_LIST

REBAR_EIDS = set(_REBAR_EIDS_LIST)
STEEL_SECTION_EIDS = set(_SECTION_EIDS_LIST)

# A5a Construction Activity Emission Factor (SEAI methodology)
# Full building factor: 40 kgCO2e/m² GIA
# Structural engineering scope (70%): 28 kgCO2e/m² GIA
A5A_EMISSION_FACTOR_KGCO2E_PER_SQM = 28.0

# Accepted band for a user override of the A5a factor (kgCO2e/m² GIA).
# Single source of truth: the web form's min/max, the server-side validation and
# the desktop app all read these, so the UI can never accept a value the engine
# would reject. 0 is allowed (site activity deliberately excluded / reported
# elsewhere); the upper bound catches unit errors (e.g. tonnes entered as kg).
A5A_MIN_KGCO2E_PER_SQM = 0.0
A5A_MAX_KGCO2E_PER_SQM = 500.0

# ── A4 transport: SEAI methodology ──────────────────────────────────────────
# Carbon conversion factors, SEAI Table 3 (kgCO2e per kg per km). Per SEAI's own
# worked example, road transport = OUTWARD (average laden) + RETURN (0% laden,
# derived) — two separate factors ADDED together, not one factor scaled by a
# multiplier. Verified against SEAI's worked example (Concrete C40/50, 20 km):
#   (0.00017853 + 0.00030342) x 20 km = 0.009639 kgCO2e/kg  ==  SEAI's A4_CF (0.00964)
# Sea is a single leg (no return factor in SEAI's table).
#   cf_outward (road, average laden) ....... 0.00017853
#   cf_return  (road, 0% laden, derived) .... 0.00030342
#   cf_sea     (single leg) ................. 0.00001977
# The per-row catalogue column a4_kgco2e_kg_road carries cf_outward (same value
# for every row); cf_return is added on top in the A4 formula below — uniformly,
# since the empty-running penalty doesn't depend on what the truck was carrying.
SEAI_CF_OUTWARD_ROAD = 0.00017853
SEAI_CF_RETURN_ROAD = 0.00030342
SEAI_CF_SEA = 0.00001977

# SEAI Table 2 default transport scenarios by material family: (road_km, sea_km).
# The catalogue's `family` column drives which scenario applies; the UI may
# override per material. Unidentified elements use the conservative import fallback.
SEAI_TRANSPORT_DEFAULTS = {
    'in_situ': (20, 0),      # ready-mix / in-situ concrete — locally manufactured
    'precast': (120, 0),     # precast concrete — nationally manufactured (short range)
    'block':   (80, 0),      # blockwork / masonry — regionally manufactured
    'rebar':   (100, 1000),  # reinforcement — imported (100 km road, 1,000 km sea)
    'section': (100, 1000),  # structural steel — imported (100 km road, 1,000 km sea)
    'sheet':   (100, 1000),  # sheet / coil steel — imported, treated as steel
    'strand':  (100, 1000),  # post-tensioning strand — imported (100 km road, 1,000 km sea)
    'timber':  (1500, 100),  # CLT / glulam — European manufactured
}
SEAI_TRANSPORT_FALLBACK = (100, 1000)   # unidentified elements — conservative import

# Old flat distance-group keys map onto these primary families for backward
# compatibility (a caller that only sends concrete/steel/pt still works).
_LEGACY_GROUP_FAMILY = {'in_situ': 'concrete', 'section': 'steel',
                        'strand': 'pt', 'timber': 'timber'}


def _transport_family(e_id, catalogue_row=None):
    """Resolve a row's transport family from the catalogue `family` column, then
    fall back to the e_id prefix (so ready BOQs / legacy catalogues still route)."""
    fam = ''
    if catalogue_row is not None:
        try:
            fam = str(catalogue_row.get('family', '') or '').strip().lower()
        except (AttributeError, TypeError):
            fam = ''
    if fam in SEAI_TRANSPORT_DEFAULTS:
        return fam
    eid = str(e_id or '')
    if eid.startswith('T_'):
        return 'timber'
    if eid.startswith('PT_'):
        return 'strand'
    if eid.startswith('C_'):
        return 'in_situ'
    if eid.startswith('R_'):
        return 'section' if e_id in STEEL_SECTION_EIDS else 'rebar'
    return ''


def _transport_distances(fam, distances):
    """(sea_km, road_km) for a material family: an explicit per-material override
    from `distances` if present, else the legacy group key, else the SEAI default."""
    road_km, sea_km = SEAI_TRANSPORT_DEFAULTS.get(fam, SEAI_TRANSPORT_FALLBACK)
    road = distances.get(f'{fam}_road_distance') if distances else None
    sea = distances.get(f'{fam}_sea_distance') if distances else None
    legacy = _LEGACY_GROUP_FAMILY.get(fam)
    if road is None and legacy and distances:
        road = distances.get(f'{legacy}_road_distance')
    if sea is None and legacy and distances:
        sea = distances.get(f'{legacy}_sea_distance')
    road = road_km if road is None else safe_float_convert(road)
    sea = sea_km if sea is None else safe_float_convert(sea)
    return sea, road


def _is_rebar_like_text(text):
    txt = str(text or '').lower()
    return any(k in txt for k in [
        'rebar', 'reinforc', 'reinforcing', 'mesh',
        'tendon', 'strand', 'post-tension', 'post tension',
    ])


def _material_label_from_row(row):
    e_id = str(row.get('e_id', '') or '').strip()
    material = str(row.get('Material', '') or '').strip().lower()
    section_flag = bool(row.get('is_structural_steel', False))
    desc_text = f"{row.get('Description', '')} {row.get('Category') or row.get('category', '')}"

    # Timber first — its own material bucket, priced by volume × timber density
    # (never concrete's 2400). Must be labelled before the concrete fallthrough
    # or its carbon would be summed as Concrete.
    if e_id.startswith('T_') or material == 'timber':
        return 'Timber'
    if e_id.startswith('C_'):
        return 'Concrete'
    if e_id.startswith('PT_'):
        return 'Post Tensioning'

    if e_id in STEEL_SECTION_EIDS:
        return 'Steel Section'
    if e_id in REBAR_EIDS:
        return 'Rebar'

    if section_flag:
        return 'Steel Section'

    if e_id.startswith('R_'):
        if _is_rebar_like_text(desc_text):
            return 'Rebar'
        if is_structural_steel_name(desc_text):
            return 'Steel Section'
        return 'Steel Section'

    if material == 'steel section':
        return 'Steel Section'
    if material == 'rebar':
        return 'Rebar'
    if material == 'post tensioning':
        return 'Post Tensioning'
    if material == 'steel':
        return 'Rebar' if _is_rebar_like_text(desc_text) else 'Steel Section'

    return 'Concrete'


def _resolve_catalogue_eid(raw_eid, row, catalogue_df):
    """Resolve unknown/missing e_id to a valid catalogue ID using material context."""
    eid = str(raw_eid or '').strip()
    if eid and eid in catalogue_df.index:
        return eid

    material = str(row.get('Material', '') or '').strip().lower()
    desc_text = f"{row.get('Description', '')} {row.get('Category', '')}"
    is_section = bool(row.get('is_structural_steel', False))

    def _first_existing(candidates):
        for c in candidates:
            if c in catalogue_df.index:
                return c
        return None

    # Post-tensioning fallback
    if eid.startswith('PT_') or material == 'post tensioning' or 'pt steel' in desc_text.lower():
        pt_candidates = [DEFAULT_PT_EID] + [c for c in catalogue_df.index if str(c).startswith('PT_')]
        return _first_existing(pt_candidates)

    # Concrete fallback
    if eid.startswith('C_') or material == 'concrete':
        conc_candidates = [DEFAULT_CONCRETE_EID] + [c for c in catalogue_df.index if str(c).startswith('C_')]
        return _first_existing(conc_candidates)

    # Steel fallback (rebar vs section)
    if is_section or material == 'steel section':
        sec_candidates = [DEFAULT_STEEL_SECTION_EID] + [c for c in catalogue_df.index if str(c) in STEEL_SECTION_EIDS]
        return _first_existing(sec_candidates)

    if material == 'rebar':
        reb_candidates = [DEFAULT_STEEL_EID] + [c for c in catalogue_df.index if str(c) in REBAR_EIDS]
        return _first_existing(reb_candidates)

    if eid.startswith('R_'):
        if _is_rebar_like_text(desc_text):
            reb_candidates = [DEFAULT_STEEL_EID] + [c for c in catalogue_df.index if str(c) in REBAR_EIDS]
            return _first_existing(reb_candidates)
        sec_candidates = [DEFAULT_STEEL_SECTION_EID] + [c for c in catalogue_df.index if str(c) in STEEL_SECTION_EIDS]
        return _first_existing(sec_candidates)

    return None


def process_catalogue(catalogue_df):
    if 'Column headings' in catalogue_df.columns:
        catalogue_df = catalogue_df.set_index('Column headings').T
    else:
        if 'e_id' in catalogue_df.columns:
            catalogue_df = catalogue_df.set_index('e_id')
        else:
            first_col = catalogue_df.columns[0]
            catalogue_df = catalogue_df.set_index(first_col)

    catalogue_df.index.name = 'e_id'

    for col in catalogue_df.columns:
        catalogue_df[col] = catalogue_df[col].apply(safe_float_convert)

    return catalogue_df


def _resolve_a5a_factor(a5a_factor):
    """A5a construction/site-activity factor (kgCO2e/m² GIA). Falls back to the
    SEAI default (28) when no override is supplied or the value is invalid."""
    try:
        v = float(a5a_factor)
        if v >= 0:
            return v
    except (TypeError, ValueError):
        pass
    return A5A_EMISSION_FACTOR_KGCO2E_PER_SQM


def calculate_emissions(boq_df, catalogue_df, distances, project_area=0, a5w_waste_pcts=None,
                        a5a_factor=None):
    # Upfront carbon only (A1-A5)
    output_columns = [
        'e_id', 'Category', 'Material', 'Description', 'Family', 'Type',
        'Correction Formula', 'Waste', 'Total Volume(m3)',
        'Density(kg/m3)', 'Mass(kg)', 'A1-A3 Emission(kgCO2e)',
        'A4 Emission(kgCO2e)', 'A5 Emission(kgCO2e)',
        'A1-A5 Emission(tCO2e)', 'Total Emission(tCO2e)',
        # Biogenic carbon stored in timber/bio-based materials (negative).
        # Reported SEPARATELY per EN 16485 — never added into A1-A5 above.
        'Sequestration(kgCO2e)',
    ]

    output_df = pd.DataFrame(columns=output_columns)

    for col in ['e_id', 'Category', 'Material', 'Description', 'Family', 'Type',
               'Correction Formula', 'Waste', 'Total Volume(m3)',
               'Density(kg/m3)', 'Mass(kg)']:
        if col in boq_df.columns:
            output_df[col] = boq_df[col]
        else:
            output_df[col] = ""

    for col in ['A1-A3 Emission(kgCO2e)', 'A4 Emission(kgCO2e)',
               'A5 Emission(kgCO2e)', 'A1-A5 Emission(tCO2e)',
               'Total Emission(tCO2e)', 'Sequestration(kgCO2e)']:
        output_df[col] = 0.0

    output_df['Mass(kg)'] = output_df['Mass(kg)'].apply(safe_float_convert)

    # Derive mass from volume × density if mass is zero
    for idx in output_df.index:
        mass_val = safe_float_convert(output_df.at[idx, 'Mass(kg)'])
        if mass_val <= 0:
            vol = safe_float_convert(output_df.at[idx, 'Total Volume(m3)'])
            dens = safe_float_convert(output_df.at[idx, 'Density(kg/m3)'])
            if vol > 0 and dens > 0:
                output_df.at[idx, 'Mass(kg)'] = vol * dens

    # Default A5w waste percentages (fraction, not %) if not provided
    # SEAI Table 4: in-situ concrete 5%, rebar 5%, structural steel sections 1%, PT 1.5%
    if a5w_waste_pcts is None:
        a5w_waste_pcts = {'Concrete': 0.05, 'Rebar': 0.05, 'Steel Section': 0.01, 'Post Tensioning': 0.015}

    missing_eids = []

    # Rows flagged by the element taxonomy as having NO emission factor
    # (ec_rate_available == False: timber/CLT/masonry/composite/connections).
    # They must contribute 0 — never silently priced with a concrete fallback —
    # and are excluded from the A5a mass allocation below. The dashboard warns
    # about them explicitly (zero-blackboxing).
    def _row_has_no_factor(row):
        v = row.get('ec_rate_available', True)
        try:
            if pd.isna(v):
                return False
        except (TypeError, ValueError):
            pass
        return not bool(v)

    has_rate_flag = 'ec_rate_available' in boq_df.columns
    no_factor_indices = set()

    for index, row in boq_df.iterrows():
        if has_rate_flag and _row_has_no_factor(row):
            no_factor_indices.add(index)
            continue  # emissions stay 0.0 for this row

        raw_e_id = str(row.get('e_id', '') or '').strip()
        e_id = _resolve_catalogue_eid(raw_e_id, row, catalogue_df)

        if not e_id:
            if raw_e_id:
                missing_eids.append(raw_e_id)
            continue

        output_df.at[index, 'e_id'] = e_id

        if e_id in catalogue_df.index:
            catalogue_row = catalogue_df.loc[e_id]

            a1_a3_factor = 0
            if 'carbon_a1_a3_kgco2e_kg' in catalogue_row:
                a1_a3_factor = catalogue_row['carbon_a1_a3_kgco2e_kg']
            elif 'carbon_a1_a3' in catalogue_row:
                a1_a3_factor = catalogue_row['carbon_a1_a3']
            else:
                print(f"WARNING: No A1-A3 carbon factor found for e_id '{e_id}' "
                      f"(checked 'carbon_a1_a3_kgco2e_kg' and 'carbon_a1_a3'). A1-A3 will be 0.")

            # User EPD override takes precedence over catalogue factor
            if 'a1_a3_override' in boq_df.columns:
                ov = boq_df.at[index, 'a1_a3_override']
                try:
                    ov_f = float(ov)
                    if ov_f > 0:
                        a1_a3_factor = ov_f
                except (TypeError, ValueError):
                    pass

            a4_sea_factor = catalogue_row.get('a4_kgco2e_kg_sea', 0)
            a4_road_factor = catalogue_row.get('a4_kgco2e_kg_road', 0)

            mass = safe_float_convert(output_df.at[index, 'Mass(kg)'])

            # Timber safety net (covers ready-BOQ / manual rows too): a timber
            # factor must be massed by its own density, never concrete's 2400.
            # Idempotent with the engine's IFC path (same volume × same density).
            if e_id.startswith('T_'):
                _tden = safe_float_convert(catalogue_row.get('density_kg_per_m3', 0))
                _tvol = safe_float_convert(output_df.at[index, 'Total Volume(m3)'])
                if _tden > 0 and _tvol > 0:
                    mass = _tvol * _tden
                    output_df.at[index, 'Density(kg/m3)'] = _tden
                    output_df.at[index, 'Mass(kg)'] = mass

            # A1-A3: Manufacturing emissions = factor (kgCO2e/kg) × mass (kg)
            a1_a3_emission = a1_a3_factor * mass

            # SEAI Table 2 transport scenario for this element, chosen by its
            # material family (catalogue `family` column, e_id-prefix fallback).
            # Distances are the SEAI defaults unless the UI overrode them.
            _fam = _transport_family(e_id, catalogue_row)
            sea_distance, road_distance = _transport_distances(_fam, distances)

            # A4: Transport = mass × distance × factor (SEAI methodology).
            # Road  = mass × dist × (outward average-laden + return 0%-laden), added.
            # Sea   = mass × dist × single-leg factor (no return).
            a4_emission = (
                (mass * sea_distance * a4_sea_factor) +
                (mass * road_distance * (a4_road_factor + SEAI_CF_RETURN_ROAD))
            )

            # A5w (site waste) — SEAI A5.3. The wasted material still had to be
            # produced (A1-A3) and hauled to site (A4) before being discarded, so
            # its waste carbon carries both stages. End-of-life C2/C4 are excluded
            # (this tool is A1-A5 only). Per SEAI: A5w = waste% × (A1-A3 + A4).
            # SEAI Table 4 waste rates: rebar 5%, steel sections 1%, concrete 5%, PT 1.5%.
            material_type = row.get('Material', '')
            is_section = e_id in STEEL_SECTION_EIDS
            is_rebar = e_id in REBAR_EIDS
            if e_id.startswith('PT_') or material_type == 'Post Tensioning':
                waste_pct = a5w_waste_pcts.get('Post Tensioning', 0.015)
            elif is_section:
                waste_pct = a5w_waste_pcts.get('Steel Section', 0.01)
            elif is_rebar or material_type == 'Steel':
                waste_pct = a5w_waste_pcts.get('Rebar', 0.05)
            else:
                waste_pct = a5w_waste_pcts.get('Concrete', 0.05)
            a5_emission = waste_pct * (a1_a3_emission + a4_emission)

            a1_a5_emission_ton = (a1_a3_emission + a4_emission + a5_emission) / 1000

            # Total emission equals A1-A5 (upfront carbon only)
            total_emission = a1_a5_emission_ton

            # Biogenic carbon stored (timber): factor (negative) × mass. Kept in
            # its own column, never folded into A1-A5.
            seq_factor = 0.0
            if 'sequestration_kgco2e_kg' in catalogue_row:
                try:
                    _s = str(catalogue_row['sequestration_kgco2e_kg']).strip()
                    seq_factor = 0.0 if _s in ('', '-', 'nan', 'None') else float(_s)
                except (TypeError, ValueError):
                    seq_factor = 0.0

            output_df.at[index, 'A1-A3 Emission(kgCO2e)'] = a1_a3_emission
            output_df.at[index, 'A4 Emission(kgCO2e)'] = a4_emission
            output_df.at[index, 'A5 Emission(kgCO2e)'] = a5_emission
            output_df.at[index, 'A1-A5 Emission(tCO2e)'] = a1_a5_emission_ton
            output_df.at[index, 'Total Emission(tCO2e)'] = total_emission
            output_df.at[index, 'Sequestration(kgCO2e)'] = seq_factor * mass
        elif raw_e_id:
            missing_eids.append(raw_e_id)

    if missing_eids:
        unique_missing = sorted(set(missing_eids))
        print(f"WARNING: {len(unique_missing)} e_id(s) not found in catalogue: "
              f"{', '.join(unique_missing[:5])}"
              f"{'...' if len(unique_missing) > 5 else ''}")

    # A5a (construction activities) is a per-area emission: factor × project_area = total kg.
    # Allocate that total across elements proportional to mass so each element row carries
    # its share. This preserves the per-area total while still enabling per-element reports.
    # A5 = A5w (waste, per material) + A5a-share (construction activities).
    if project_area > 0:
        # Exclude no-factor rows from the A5a allocation: they carry no other
        # emissions and would otherwise show a phantom A5 share while being
        # reported as "contributes 0".
        _alloc_idx = [i for i in output_df.index if i not in no_factor_indices]
        total_mass = output_df.loc[_alloc_idx, 'Mass(kg)'].sum() if _alloc_idx else 0
        if total_mass > 0:
            a5a_total_kg = _resolve_a5a_factor(a5a_factor) * project_area
            for index in _alloc_idx:
                mass = safe_float_convert(output_df.at[index, 'Mass(kg)'])
                element_a5a_kg = a5a_total_kg * (mass / total_mass)
                a5_new = safe_float_convert(output_df.at[index, 'A5 Emission(kgCO2e)']) + element_a5a_kg
                a1_a3 = safe_float_convert(output_df.at[index, 'A1-A3 Emission(kgCO2e)'])
                a4 = safe_float_convert(output_df.at[index, 'A4 Emission(kgCO2e)'])
                new_total = (a1_a3 + a4 + a5_new) / 1000
                output_df.at[index, 'A5 Emission(kgCO2e)'] = a5_new
                output_df.at[index, 'A1-A5 Emission(tCO2e)'] = new_total
                output_df.at[index, 'Total Emission(tCO2e)'] = new_total

    return output_df


def create_material_summary(output_df):
    labels = output_df.apply(_material_label_from_row, axis=1)
    concrete_sum = output_df[labels == 'Concrete']['Total Emission(tCO2e)'].sum()
    rebar_sum = output_df[labels == 'Rebar']['Total Emission(tCO2e)'].sum()
    section_sum = output_df[labels == 'Steel Section']['Total Emission(tCO2e)'].sum()
    pt_sum = output_df[labels == 'Post Tensioning']['Total Emission(tCO2e)'].sum()
    timber_sum = output_df[labels == 'Timber']['Total Emission(tCO2e)'].sum()

    types = ['Concrete', 'Rebar', 'Steel Section', 'Post Tensioning']
    sums = [concrete_sum, rebar_sum, section_sum, pt_sum]
    # Add Timber only when the project actually contains it, so concrete/steel
    # projects keep their existing 4-row summary (no regression) while timber
    # emissions are never dropped from the total.
    if (labels == 'Timber').any():
        types.append('Timber')
        sums.append(timber_sum)

    return pd.DataFrame({'Material Type': types, 'Total Emission(tCO2e)': sums})


def calculate_metrics(summary_df, project_area, a5a_factor=None):
    # Total emissions — A5a already distributed into elements via calculate_emissions.
    # A5 per element = A5w (waste) + A5a (construction activities share by mass).
    total_emission_ton = summary_df['Total Emission(tCO2e)'].sum()
    total_emission_kg = total_emission_ton * 1000
    total_emission_per_sqm = total_emission_kg / project_area if project_area > 0 else 0

    # A5a reference values (methodology note — not added again to totals). Uses the
    # user's site-activity override when given, else the SEAI default (28).
    a5a_used = _resolve_a5a_factor(a5a_factor)
    if project_area > 0:
        a5a_kg = a5a_used * project_area
        a5a_data = {'a5a_emission_ton': a5a_kg / 1000, 'a5a_emission_kg': a5a_kg,
                    'a5a_factor': a5a_used}
    else:
        a5a_data = {'a5a_emission_ton': 0, 'a5a_emission_kg': 0,
                    'a5a_factor': a5a_used}

    return {
        'total_emission_ton': total_emission_ton,
        'total_emission_kg': total_emission_kg,
        'total_emission_per_sqm': total_emission_per_sqm,
        # 'material_emission_*' kept for back-compat with templates that still read them.
        # They equal total_emission_* because A5a is already distributed into element rows.
        'material_emission_ton': total_emission_ton,
        'material_emission_kg': total_emission_kg,
        'a5a_emission_ton': a5a_data['a5a_emission_ton'],
        'a5a_emission_kg': a5a_data['a5a_emission_kg'],
        'a5a_factor': a5a_data['a5a_factor'],
    }


def calculate_stage_emissions(detailed_df):
    """
    Calculate emissions by lifecycle stage (upfront carbon A1-A5 only).
    A5 = A5w (waste) + A5a (construction activities), both already
    distributed into each element's A5 column by calculate_emissions.
    """
    return {
        'A1-A3': detailed_df['A1-A3 Emission(kgCO2e)'].sum() / 1000,
        'A4': detailed_df['A4 Emission(kgCO2e)'].sum() / 1000,
        'A5': detailed_df['A5 Emission(kgCO2e)'].sum() / 1000,
    }


def calculate_material_stage_emissions(detailed_df):
    labels = detailed_df.apply(_material_label_from_row, axis=1)
    concrete_df = detailed_df[labels == 'Concrete']
    rebar_df = detailed_df[labels == 'Rebar']
    section_df = detailed_df[labels == 'Steel Section']
    pt_df = detailed_df[labels == 'Post Tensioning']
    timber_df = detailed_df[labels == 'Timber']

    stage_columns = ['A1-A3 Emission(kgCO2e)', 'A4 Emission(kgCO2e)', 'A5 Emission(kgCO2e)']

    concrete_emissions = []
    rebar_emissions = []
    section_emissions = []
    pt_emissions = []
    timber_emissions = []

    for col in stage_columns:
        if col in detailed_df.columns:
            concrete_emissions.append(pd.to_numeric(concrete_df[col], errors='coerce').fillna(0).sum() / 1000)
            rebar_emissions.append(pd.to_numeric(rebar_df[col], errors='coerce').fillna(0).sum() / 1000)
            section_emissions.append(pd.to_numeric(section_df[col], errors='coerce').fillna(0).sum() / 1000)
            pt_emissions.append(pd.to_numeric(pt_df[col], errors='coerce').fillna(0).sum() / 1000)
            timber_emissions.append(pd.to_numeric(timber_df[col], errors='coerce').fillna(0).sum() / 1000)
        else:
            concrete_emissions.append(0)
            rebar_emissions.append(0)
            section_emissions.append(0)
            pt_emissions.append(0)
            timber_emissions.append(0)

    return {
        'concrete': concrete_emissions,
        'rebar': rebar_emissions,
        'structural_steel': section_emissions,
        # Keep 'steel' as combined for any code that still reads it
        'steel': [r + s for r, s in zip(rebar_emissions, section_emissions)],
        'pt': pt_emissions,
        'timber': timber_emissions,
        'stages': ['A1-A3', 'A4', 'A5']
    }


def check_has_pt_slab(detailed_df):
    """Return True if any PT rows come from a slab/floor category."""
    if detailed_df is None or detailed_df.empty or 'e_id' not in detailed_df.columns:
        return False
    pt_rows = detailed_df[detailed_df['e_id'].astype(str).str.startswith('PT_', na=False)]
    if pt_rows.empty:
        return False
    cat_col = None
    for c in ('Category', 'category'):
        if c in pt_rows.columns:
            cat_col = c
            break
    if cat_col is None:
        return False
    slab_cats = {'Slab/Floor', 'slab/floor', 'Slab', 'slab', 'Floor', 'floor',
                 'Roof', 'roof'}
    return pt_rows[cat_col].isin(slab_cats).any()


def prepare_detailed_data(detailed_df):
    detailed_data = []

    for _, row in detailed_df.iterrows():
        e_id = row.get('e_id', '')
        material = _material_label_from_row(row)
        cnt_raw = row.get('Count', row.get('count', 1))
        try:
            cnt = int(cnt_raw) if cnt_raw not in (None, '') else 1
        except (ValueError, TypeError):
            cnt = 1
        a1a3_kg = safe_float_convert(row.get('A1-A3 Emission(kgCO2e)', 0))
        a4_kg   = safe_float_convert(row.get('A4 Emission(kgCO2e)', 0))
        a5_kg   = safe_float_convert(row.get('A5 Emission(kgCO2e)', 0))
        total_t = safe_float_convert(row.get('Total Emission(tCO2e)', 0))
        seq_kg  = safe_float_convert(row.get('Sequestration(kgCO2e)', 0))
        detailed_data.append({
            'e_id': e_id,
            'material': material,
            'category': row.get('Category', ''),
            'description': row.get('Description', ''),
            'family': str(row.get('Family', '') or ''),
            'ifc_type': str(row.get('Type', '') or ''),
            'level': str(row.get('level', '') or ''),
            'count': cnt,
            'mass': round(safe_float_convert(row.get('Mass(kg)', 0)), 3),
            'a1_a3': round(a1a3_kg, 3),         # kgCO₂e
            'a4':    round(a4_kg, 3),           # kgCO₂e
            'a5':    round(a5_kg, 3),           # kgCO₂e
            'total_kg': round(a1a3_kg + a4_kg + a5_kg, 3),  # kgCO₂e — matches a1_a3 + a4 + a5
            'total': round(total_t, 3),         # tCO₂e (legacy field — kept for back-compat)
            'sequestration': round(seq_kg, 3),  # kgCO₂e biogenic (negative), timber only
        })
    return detailed_data


