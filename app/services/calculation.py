"""
The calculation - lifted out of the legacy Flask /run route.
============================================================
Moved from web_app.py:553-1195 with the Flask plumbing removed. The numerical
and classification logic is carried over VERBATIM and in the same order; only
the source of the inputs changed (request JSON + session dict -> RunConfig).

RunConfig deliberately uses the legacy payload's own field names
(`concrete_grade`, `steel_rates`, `element_materials`, ...) so the moved code is
character-for-character what it was. Translating our stored wizard config into
those names is a separate, separately-testable step - not something folded in
here, where a rename would be indistinguishable from a behaviour change.

The riskiest part is `_build_boq_direct` (web_app.py:756-920). It exists because
prepare_boq_from_ifc SKIPS rows with volume <= 0, and steel is quantified by
mass, not volume - so without it every mass-only steel row in a spreadsheet BOQ
silently contributes zero carbon.

Verified against the legacy route: identical totals, ratings, per-material and
per-stage splits for every ingest path (tests/test_calculation_parity.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app import core_bridge  # noqa: F401  - puts src/core + src/ingest on sys.path

from calculations import (A5A_MAX_KGCO2E_PER_SQM, A5A_MIN_KGCO2E_PER_SQM)
from catalogue import (CATALOGUE_VERSION, CONCRETE_GRADE_MAP,
                       DEFAULT_PT_EID, DEFAULT_STEEL_EID,
                       DEFAULT_STEEL_SECTION_EID, DEFAULT_WASTE_FACTORS,
                       EmissionCatalogue, GRADE_LABEL_MAP, PT_TYPES,
                       REBAR_TYPES, STEEL_SECTION_TYPES)
from engine import (prepare_boq_from_ifc, run_calculations_from_boq,
                    run_sensitivity_analysis, _precast_default_eid)
from stages import get_stage_info, uncertainty_range
from web_helpers import _factor_type, _BUCKET_DEFAULT_EID

# Reverse map: concrete e_id -> (grade label, ggbs %). Same construction as
# web_app.py:43-48; used to recover grades a ready BOQ carries implicitly.
_SHORT_TO_LABEL = {v: k for k, v in GRADE_LABEL_MAP.items()}
_EID_TO_GRADE_GGBS = {
    eid: (_SHORT_TO_LABEL.get(grade, f'{grade} MPa'), ggbs)
    for (grade, ggbs), eid in CONCRETE_GRADE_MAP.items()
}


class CalculationError(ValueError):
    """A rejected input, with a message meant for the user."""


@dataclass
class RunConfig:
    """Everything the calculation needs. Field names mirror the legacy payload."""
    # Project
    project_name: str = 'Untitled'
    area: float = 0.0
    stage: str = 'Concept / Schematic Design'
    structural_system: str = ''
    location: str = 'Not specified'
    client: str = 'Not specified'
    project_type: str = 'Not specified'

    # Step 2 - what counts
    included_cats: list[str] | None = None
    excluded_indices: list[int] = field(default_factory=list)
    excluded_levels: list[str] | None = None
    manual_rows: list[dict] = field(default_factory=list)

    # Step 3 - materials
    concrete_grade: str = '32/40'
    ggbs_pct: int = 0
    rebar_type: str = ''
    section_type: str = ''
    pt_type: str = ''
    steel_rates: dict = field(default_factory=dict)
    pt_rates: dict = field(default_factory=dict)
    element_materials: dict = field(default_factory=dict)
    steel_assignments: list[dict] = field(default_factory=list)
    factor_overrides: dict = field(default_factory=dict)
    epd_overrides: dict = field(default_factory=dict)

    # Step 4 - transport, waste, site activity
    distances: dict = field(default_factory=dict)
    waste_factors: dict = field(default_factory=dict)   # MULTIPLIERS (1.05)
    a5a_factor: float | None = None

    # Behaviour
    sensitivity: bool = True


@dataclass
class RunResult:
    """Calculation output plus the derived tables the reports and dashboard use."""
    data: Any                                   # ProjectDataModel
    boq_df: pd.DataFrame
    summary: dict
    no_factor: dict
    concrete_specs_used: list[dict]
    emission_factors_used: list[dict]
    rebar_rates_used: list[dict]


def execute_run(elements_df: pd.DataFrame, geometry_data: list,
                config: RunConfig, *, source_type: str, has_real_eids: bool,
                elements_list: list[dict] | None = None,
                catalogue: EmissionCatalogue | None = None) -> RunResult:
    """Run the full calculation. Pure: no Flask, no session, no filesystem.

    Args:
        elements_df:    the extraction DataFrame (dtype-exact, from Parquet)
        geometry_data:  3D meshes, filtered here to match the element exclusions
        source_type:    'ifc' | 'csv' - drives the use_direct decision
        has_real_eids:  whether the upload carried its own catalogue ids
        elements_list:  the 20-key rows, used to map excluded positions to the
                        (category, name) pairs the geometry filter needs
    """
    if config.area <= 0:
        # GIA is required: without it per-m2 collapses to 0 and the SCORS rating
        # would falsely read A++, while the A5a term (factor x area) is lost.
        raise CalculationError(
            'Gross Internal Area (m²) is required and must be greater than 0 '
            'for a per-m² carbon rating.')

    catalogue = catalogue or EmissionCatalogue()
    stage_info = get_stage_info(config.stage)
    project_info = _project_info(config, stage_info)

    # ── Waste factors (multipliers), merged over the SEAI defaults ──────
    waste_factors = DEFAULT_WASTE_FACTORS.copy()
    for k, v in (config.waste_factors or {}).items():
        try:
            waste_factors[k] = float(v)
        except (TypeError, ValueError):
            pass

    # ── Filter, then append manual rows ─────────────────────────────────
    elements_df = _apply_filters(elements_df, config)
    if config.manual_rows:
        extra = _build_manual_rows(config.manual_rows)
        if not extra.empty:
            elements_df = pd.concat([elements_df, extra], ignore_index=True)

    # ── Per-element overrides -> member_eids + rate dicts ───────────────
    steel_rates_dict = {k: float(v) for k, v in (config.steel_rates or {}).items()
                        if v is not None and v != ''}
    pt_rates_dict = {k: float(v) for k, v in (config.pt_rates or {}).items()
                     if v is not None and v != ''}
    member_eids = _build_member_eids(config, catalogue,
                                     steel_rates_dict, pt_rates_dict)

    # ── Named types -> catalogue e_ids ──────────────────────────────────
    steel_eid = REBAR_TYPES.get(str(config.rebar_type or '').strip(),
                                DEFAULT_STEEL_EID)
    section_eid = STEEL_SECTION_TYPES.get(str(config.section_type or '').strip(),
                                          DEFAULT_STEEL_SECTION_EID)
    pt_eid = PT_TYPES.get(str(config.pt_type or '').strip(), DEFAULT_PT_EID)

    # ── Build the BOQ ───────────────────────────────────────────────────
    boq_df = build_boq(
        elements_df, config, catalogue,
        source_type=source_type, has_real_eids=has_real_eids,
        waste_factors=waste_factors, member_eids=member_eids,
        steel_rates_dict=steel_rates_dict, pt_rates_dict=pt_rates_dict,
        steel_eid=steel_eid, section_eid=section_eid, pt_eid=pt_eid)

    # ── EPD overrides ───────────────────────────────────────────────────
    _apply_epd_overrides(boq_df, config.epd_overrides, catalogue)

    # ── A5w fractions + A5a ─────────────────────────────────────────────
    a5w_waste_pcts = {
        k: max(float(waste_factors.get(k, DEFAULT_WASTE_FACTORS.get(k, 1.0))) - 1.0, 0.0)
        for k in ('Concrete', 'Rebar', 'Steel Section', 'Post Tensioning')
    }
    a5a_override = _validate_a5a(config.a5a_factor)

    results = run_calculations_from_boq(
        boq_df, project_info, config.distances, config.area, catalogue,
        a5w_waste_pcts=a5w_waste_pcts, a5a_factor=a5a_override)

    # ── Geometry: excluded elements must vanish everywhere ──────────────
    results.geometry_data = _filter_geometry(geometry_data, config,
                                             elements_list or [])
    results.steel_type = ''
    results.pt_type = ''
    results.metrics['uncertainty_pct'] = stage_info['uncertainty_pct']
    results.catalogue_version = CATALOGUE_VERSION
    results.a5w_waste_pcts = a5w_waste_pcts

    # ── Zero-blackbox: elements with no factor contribute 0 ─────────────
    no_factor = _no_factor_summary(boq_df)
    project_info['no_factor_summary'] = no_factor

    # ── Sensitivity (optional; a failure is never fatal) ────────────────
    results.sensitivity_results = {}
    if config.sensitivity:
        try:
            results.sensitivity_results = run_sensitivity_analysis(
                boq_df, project_info, config.distances, config.area, catalogue,
                a5w_waste_pcts=a5w_waste_pcts, base_metrics=results.metrics,
                a5a_factor=a5a_override)
        except Exception as exc:
            print(f'[calculation] sensitivity failed (non-fatal): {exc}')

    # ── Assumptions actually applied ────────────────────────────────────
    rebar_rates_used = [{'element': k, 'rate_kg_m3': v}
                        for k, v in steel_rates_dict.items()]
    concrete_specs = _concrete_specs_used(boq_df, catalogue, config)
    factors_used = _emission_factors_used(boq_df, catalogue, config,
                                          steel_eid, section_eid, pt_eid)

    results.rebar_rates_used = rebar_rates_used
    results.concrete_specs_used = concrete_specs
    results.emission_factors_used = factors_used

    return RunResult(
        data=results, boq_df=boq_df,
        summary=_summary(results, stage_info, no_factor),
        no_factor=no_factor,
        concrete_specs_used=concrete_specs,
        emission_factors_used=factors_used,
        rebar_rates_used=rebar_rates_used)


# ═══════════════════════════════════════════════════════════════════════
#  BOQ CONSTRUCTION
# ═══════════════════════════════════════════════════════════════════════

def build_boq(elements_df, config, catalogue, *, source_type, has_real_eids,
              waste_factors, member_eids, steel_rates_dict, pt_rates_dict,
              steel_eid, section_eid, pt_eid) -> pd.DataFrame:
    """Choose and run the BOQ build path. web_app.py:738-945, verbatim.

    A spreadsheet that carries real e_ids OR explicit steel mass takes the direct
    path. Running it through prepare_boq_from_ifc would discard the supplied
    masses (re-deriving them as volume x 7850), add rate-based rebar on top, and
    ignore the steel/PT factor the user assigned - and because that function
    skips rows with volume <= 0, mass-only steel rows would vanish entirely.
    """
    _steel_all = (elements_df['is_steel'].astype(bool)
                  if 'is_steel' in elements_df.columns
                  else pd.Series(False, index=elements_df.index))
    _mass_all = (pd.to_numeric(elements_df['Mass(kg)'], errors='coerce').fillna(0)
                 if 'Mass(kg)' in elements_df.columns
                 else pd.Series(0, index=elements_df.index))
    has_explicit_steel = (source_type == 'csv'
                          and bool((_steel_all & (_mass_all > 0)).any()))
    use_direct = bool(has_real_eids or has_explicit_steel)

    if use_direct:
        boq_df = _build_boq_direct(
            elements_df, config, catalogue, waste_factors=waste_factors,
            member_eids=member_eids, steel_rates_dict=steel_rates_dict,
            pt_rates_dict=pt_rates_dict, steel_eid=steel_eid, pt_eid=pt_eid)
    else:
        boq_df = prepare_boq_from_ifc(
            elements_df,
            concrete_grade=config.concrete_grade, ggbs_pct=config.ggbs_pct,
            steel_rates_dict=steel_rates_dict, waste_factors=waste_factors,
            catalogue=catalogue, pt_rates_dict=pt_rates_dict,
            member_eids=member_eids, steel_eid=steel_eid,
            section_eid=section_eid, pt_eid=pt_eid)

    # Sensitivity's GGBS scenarios remap grades via this hint. The IFC path sets
    # it; ready BOQs carry grades implicitly in their e_ids, so recover them.
    if 'concrete_grade_hint' not in boq_df.columns and 'e_id' in boq_df.columns:
        boq_df['concrete_grade_hint'] = [
            _EID_TO_GRADE_GGBS.get(str(e).strip(), ('', None))[0] or ''
            for e in boq_df['e_id']
        ]
    return boq_df


def _build_boq_direct(elements_df, config, catalogue, *, waste_factors,
                      member_eids, steel_rates_dict, pt_rates_dict,
                      steel_eid, pt_eid) -> pd.DataFrame:
    """web_app.py:756-920, moved verbatim.

    Applies the user's steel factor assignments, backfills blank e_ids, masses
    sections supplied by volume, and generates rate-based rebar/PT on concrete
    members - skipping any member whose factor already includes reinforcement.
    """
    from catalogue import (STEEL_SECTION_EIDS as _SS_EIDS, REBAR_EIDS as _RB_EIDS,
                           DEFAULT_STEEL_EID as _DEF_STEEL,
                           DEFAULT_CONCRETE_EID as _DEF_CONC)
    from engine import is_structural_steel_name as _is_ss_name

    boq_df = elements_df.copy()
    section_eids = set(_SS_EIDS)
    rebar_eids = set(_RB_EIDS)

    _bcat = next((c for c in ('Category', 'category') if c in boq_df.columns), None)
    _bname = next((c for c in ('Description', 'name') if c in boq_df.columns), None)
    _bsteel = (boq_df['is_steel'] if 'is_steel' in boq_df.columns
               else pd.Series(False, index=boq_df.index))

    # ── Per-group steel emission-factor assignments ─────────────────────
    for a in (config.steel_assignments or []):
        eid_new = str(a.get('e_id', '') or '').strip()
        if not eid_new:
            continue
        m = _bsteel.astype(bool)
        if _bcat and a.get('cat') is not None:
            m = m & (boq_df[_bcat].astype(str) == str(a.get('cat')))
        if _bname and a.get('name') is not None:
            m = m & (boq_df[_bname].astype(str) == str(a.get('name')))
        if m.any():
            boq_df.loc[m, 'e_id'] = eid_new
            # Re-set the SEAI waste factor to match the new bucket.
            w = (1.015 if eid_new.startswith('PT_')
                 else (1.01 if eid_new in section_eids else 1.05))
            if 'Waste' in boq_df.columns:
                boq_df.loc[m, 'Waste'] = w

    # ── Step-3 grade/GGBS on concrete, bucket defaults on blank steel ───
    default_conc_eid = (catalogue.get_concrete_eid(config.concrete_grade,
                                                   config.ggbs_pct) or _DEF_CONC)
    _cur = boq_df['e_id'].astype(str).str.strip()
    _blank = _cur.isin(['', 'nan', 'None', '<NA>', 'NaN'])
    _steel_b = _bsteel.astype(bool)
    _bmat = next((c for c in ('Material', 'material') if c in boq_df.columns), None)

    for idx in boq_df.index[~_steel_b]:
        nm = str(boq_df.at[idx, _bname]) if _bname else ''
        cc = str(boq_df.at[idx, _bcat]) if _bcat else ''
        ov = member_eids.get(f'{cc}|||{nm}', member_eids.get(nm))
        if ov:
            boq_df.at[idx, 'e_id'] = ov
        elif str(boq_df.at[idx, 'e_id']).strip() in ('', 'nan', 'None', '<NA>', 'NaN'):
            # Hollowcore / precast beams & columns take their precast factor,
            # else the global in-situ default. Mirrors the engine path.
            boq_df.at[idx, 'e_id'] = _precast_default_eid(nm, cc) or default_conc_eid

    for idx in boq_df.index[_blank & _steel_b]:
        nm = str(boq_df.at[idx, _bname]) if _bname else ''
        cc = str(boq_df.at[idx, _bcat]) if _bcat else ''
        mt = str(boq_df.at[idx, _bmat]) if _bmat else 'Steel'
        ft = _factor_type('', True,
                          bool(boq_df.at[idx, 'is_structural_steel'])
                          if 'is_structural_steel' in boq_df.columns else False,
                          text=f'{mt} {nm} {cc}')
        boq_df.at[idx, 'e_id'] = _BUCKET_DEFAULT_EID.get(ft, _DEF_STEEL)

    # ── Classify sections vs rebar ──────────────────────────────────────
    eids = boq_df['e_id'].astype(str).str.strip()
    desc_col = next((c for c in ('Description', 'name') if c in boq_df.columns), None)
    descriptions = (boq_df[desc_col].astype(str) if desc_col
                    else pd.Series('', index=boq_df.index))
    mat_col2 = next((c for c in ('Material', 'material') if c in boq_df.columns), None)
    material_vals = (boq_df[mat_col2].astype(str).str.lower() if mat_col2
                     else pd.Series('', index=boq_df.index))

    rebar_like = descriptions.str.contains(
        r'rebar|reinforc|mesh|tendon|strand|post[- ]?tension',
        case=False, na=False, regex=True)
    section_like = descriptions.apply(lambda txt: _is_ss_name(str(txt)))
    inferred_sections = (
        eids.isin(section_eids)
        | (eids.str.startswith('R_', na=False) & section_like)
        | ((material_vals == 'steel section') & eids.str.startswith('R_', na=False)))
    inferred_rebar = (
        eids.isin(rebar_eids) | rebar_like | (material_vals == 'rebar')
        | eids.str.startswith('PT_', na=False))
    boq_df['is_structural_steel'] = inferred_sections & ~inferred_rebar

    # Sections given by VOLUME are massed as volume x 7850. Sections supplied
    # directly by mass (volume 0) keep their explicit Mass(kg).
    section_rows = boq_df['is_structural_steel']
    if section_rows.any():
        v_col = next((c for c in ('Volume(m3)', 'Total Volume(m3)', 'volume_m3')
                      if c in boq_df.columns), None)
        if v_col:
            vols = pd.to_numeric(boq_df[v_col], errors='coerce').fillna(0)
            by_vol = section_rows & (vols > 0)
            if by_vol.any():
                boq_df.loc[by_vol, 'Density(kg/m3)'] = 7850
                boq_df.loc[by_vol, 'Mass(kg)'] = vols[by_vol] * 7850

    # ── Rate-based rebar / PT on concrete members ───────────────────────
    _vcol = next((c for c in ('Volume(m3)', 'Total Volume(m3)', 'volume_m3')
                  if c in boq_df.columns), None)
    if _vcol:
        _final_eids = boq_df['e_id'].astype(str).str.strip()
        _steel_now = _bsteel.astype(bool)
        modelled_rebar_names = set(
            boq_df.loc[_steel_now & _final_eids.isin(rebar_eids), _bname].astype(str)
        ) if _bname else set()
        modelled_pt_names = set(
            boq_df.loc[_steel_now & _final_eids.str.startswith('PT_', na=False),
                       _bname].astype(str)) if _bname else set()
        rebar_w = waste_factors.get('Rebar', 1.05)
        pt_w = waste_factors.get('Post Tensioning', 1.015)
        extra_rows = []
        for idx in boq_df.index[~_steel_now]:
            nm = str(boq_df.at[idx, _bname]) if _bname else ''
            cc = str(boq_df.at[idx, _bcat]) if _bcat else ''
            vol = pd.to_numeric(boq_df.at[idx, _vcol], errors='coerce')
            vol = 0.0 if pd.isna(vol) else float(vol)
            if vol <= 0:
                continue
            # Composite factor (precast / prestressed hollowcore) already prices
            # its reinforcement - adding a rate on top double counts the steel.
            if catalogue.factor_includes_reinforcement(
                    str(boq_df.at[idx, 'e_id']).strip()):
                continue
            cnt = boq_df.at[idx, 'Count'] if 'Count' in boq_df.columns else 1

            def _rate(d):
                v = d.get(f'{cc}|||{nm}', d.get(nm, 0))
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return 0.0

            rr = _rate(steel_rates_dict)
            pr = _rate(pt_rates_dict)
            if rr > 0 and nm not in modelled_rebar_names:
                m = vol * rr
                extra_rows.append({
                    'e_id': steel_eid, 'Category': cc, 'Material': 'Steel',
                    'Description': f'{nm} - Rebar ({rr:g} kg/m³)',
                    'Volume(m3)': m / 7850.0, 'Total Volume(m3)': m / 7850.0,
                    'Density(kg/m3)': 7850, 'Mass(kg)': m, 'Count': cnt,
                    'Waste': rebar_w, 'is_steel': True,
                    'is_structural_steel': False})
            if pr > 0 and nm not in modelled_pt_names:
                m = vol * pr
                extra_rows.append({
                    'e_id': pt_eid, 'Category': cc, 'Material': 'Steel',
                    'Description': f'{nm} - PT ({pr:g} kg/m³)',
                    'Volume(m3)': m / 7850.0, 'Total Volume(m3)': m / 7850.0,
                    'Density(kg/m3)': 7850, 'Mass(kg)': m, 'Count': cnt,
                    'Waste': pt_w, 'is_steel': True,
                    'is_structural_steel': False})
        if extra_rows:
            boq_df = pd.concat([boq_df, pd.DataFrame(extra_rows)], ignore_index=True)

    # Concrete rows derive mass from volume when no Mass column exists at all.
    if 'Mass(kg)' not in boq_df.columns:
        v_col = next((c for c in ('Total Volume(m3)', 'Volume(m3)')
                      if c in boq_df.columns), None)
        d_col = 'Density(kg/m3)' if 'Density(kg/m3)' in boq_df.columns else None
        if v_col:
            dens = (pd.to_numeric(boq_df[d_col], errors='coerce').fillna(2400)
                    if d_col else 2400)
            boq_df['Mass(kg)'] = pd.to_numeric(
                boq_df[v_col], errors='coerce').fillna(0) * dens

    return boq_df


# ═══════════════════════════════════════════════════════════════════════
#  SUPPORTING STEPS  (each moved from the named /run block)
# ═══════════════════════════════════════════════════════════════════════

def _project_info(config: RunConfig, stage_info: dict) -> dict:
    """web_app.py:590-602."""
    return {
        'name': config.project_name, 'project_name': config.project_name,
        'location': config.location, 'client': config.client,
        'client_name': config.client,
        'type': config.project_type, 'project_type': config.project_type,
        'stage': stage_info['label'], 'project_stage': stage_info['label'],
        'stage_code': stage_info['code'], 'stage_badge': stage_info['badge'],
        'stage_description': stage_info['description'],
        'stage_expectations': stage_info['expectations'],
        'uncertainty_pct': stage_info['uncertainty_pct'],
        'epd_expected': stage_info['epd_expected'],
        'catalogue_version': CATALOGUE_VERSION,
        'structural_system': config.structural_system,
        'scenario_params': {},
    }


def _apply_filters(elements_df: pd.DataFrame, config: RunConfig) -> pd.DataFrame:
    """web_app.py:635-670. Excluded indices are POSITIONAL (0..N-1) against the
    extraction order, which is why that order must never be reshuffled."""
    cat_col = next((c for c in ('Category', 'category')
                    if c in elements_df.columns), None)
    level_col = next((c for c in ('level', 'Level')
                      if c in elements_df.columns), None)

    mask = pd.Series([True] * len(elements_df), index=elements_df.index)

    if config.included_cats is not None and cat_col:
        mask = mask & elements_df[cat_col].astype(str).isin(set(config.included_cats))

    if config.excluded_levels and level_col:
        mask = mask & ~elements_df[level_col].astype(str).isin(
            set(config.excluded_levels))

    if config.excluded_indices:
        order = list(elements_df.index)
        pos_to_df_idx = {i: df_idx for i, df_idx in enumerate(order)}
        try:
            excluded_pos = {int(i) for i in config.excluded_indices}
        except (TypeError, ValueError):
            excluded_pos = set()
        drop = {pos_to_df_idx[i] for i in excluded_pos if i in pos_to_df_idx}
        if drop:
            mask = mask & ~elements_df.index.isin(drop)

    return elements_df[mask]


def _build_manual_rows(rows) -> pd.DataFrame:
    """web_app.py:209-259. Rows the user typed in Step 2, in the elements schema.

    A manual "Post Tensioning" row must not collapse to generic steel and
    calculate as REBAR - wrong factor and wrong waste - so material_hint drives
    the engine path while the seeded e_id drives the direct path.
    """
    out = []
    for r in rows or []:
        try:
            name = str(r.get('name') or 'Manual item').strip()
            cat = str(r.get('category') or 'Other').strip()
            mat = str(r.get('material') or 'Concrete').strip()
            vol = float(r.get('volume_m3') or 0)
            mass = float(r.get('mass_kg') or 0)
            count = int(float(r.get('count') or 1))
        except (TypeError, ValueError):
            continue
        mat_l = mat.lower()
        is_steel = mat_l in ('steel', 'rebar', 'steel section',
                             'post tensioning', 'pt')
        density = 7850.0 if is_steel else 2400.0
        if vol <= 0 and mass > 0:
            vol = mass / density
        if mass <= 0 and vol > 0:
            mass = vol * density
        if vol <= 0 and mass <= 0:
            continue
        mat_label = 'Steel' if is_steel else 'Concrete'
        is_pt_row = mat_l in ('post tensioning', 'post-tensioning', 'pt')
        is_section_row = mat_l in ('steel section', 'section')
        seed_eid = DEFAULT_PT_EID if is_pt_row else ''
        pt_waste = DEFAULT_WASTE_FACTORS.get('Post Tensioning', 1.015)
        sec_waste = DEFAULT_WASTE_FACTORS.get('Steel Section', 1.01)
        out.append({
            'Category': cat, 'category': cat,
            'Description': name, 'name': name,
            'Material': mat_label, 'material': mat_label,
            'material_hint': mat_l,
            'Volume(m3)': vol, 'volume_m3': vol, 'Total Volume(m3)': vol,
            'Mass(kg)': mass, 'Density(kg/m3)': density,
            'Count': max(count, 1), 'count': max(count, 1),
            'is_steel': is_steel, 'is_structural_steel': is_section_row,
            'has_modeled_rebar': False, 'e_id': seed_eid, 'level': '',
            'Waste': (pt_waste if is_pt_row
                      else sec_waste if is_section_row else 1.05),
        })
    return pd.DataFrame(out)


def _build_member_eids(config, catalogue, steel_rates_dict,
                       pt_rates_dict) -> dict:
    """web_app.py:682-731. Mutates the two rate dicts in place, as the original
    does - per-element rates are merged into the same dicts the engine reads."""
    member_eids: dict[str, str] = {}
    for elem_key, overrides in (config.element_materials or {}).items():
        if '|||' not in elem_key:
            continue
        e_cat, e_name = elem_key.split('|||', 1)
        grade_ov = overrides.get('grade') or config.concrete_grade
        ggbs_ov = int(overrides.get('ggbs') or config.ggbs_pct)
        eid = catalogue.get_concrete_eid(grade_ov, ggbs_ov)
        if eid:
            member_eids[f'{e_cat}|||{e_name}'] = eid
            member_eids[e_name] = eid
        for src, dest in ((overrides.get('rebarRate'), steel_rates_dict),
                          (overrides.get('ptRate'), pt_rates_dict)):
            if src is not None:
                try:
                    v = float(src)
                    dest[f'{e_cat}|||{e_name}'] = v
                    dest[e_name] = v
                except (TypeError, ValueError):
                    pass

    # Full-database factor picks for Unknown/Other rows override the default.
    for _k, _eid in (config.factor_overrides or {}).items():
        _eid = str(_eid or '').strip()
        if not _eid or '|||' not in str(_k):
            continue
        _c, _n = str(_k).split('|||', 1)
        member_eids[f'{_c}|||{_n}'] = _eid
        member_eids[_n] = _eid
    return member_eids


def _apply_epd_overrides(boq_df, epd_overrides, catalogue) -> None:
    """web_app.py:946-983. Concrete EPDs are published per m3, so they are
    divided by 2400 to reach the per-kg form the engine multiplies by mass."""
    if not epd_overrides or 'e_id' not in boq_df.columns:
        return
    for _key, override in epd_overrides.items():
        raw = override.get('a1_a3')
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val <= 0:
            continue
        mat_type = override.get('mat_type', '')
        if mat_type == 'Concrete':
            eid = override.get('e_id', '')
            if not eid:
                eid = catalogue.get_concrete_eid(override.get('grade', ''),
                                                 int(override.get('ggbs', 0) or 0))
            if not eid:
                continue
            mask = boq_df['e_id'].astype(str) == eid
            boq_df.loc[mask, 'a1_a3_override'] = val / 2400.0
        else:
            eid = override.get('e_id', '')
            if not eid:
                continue
            mask = boq_df['e_id'].astype(str) == eid
            boq_df.loc[mask, 'a1_a3_override'] = val


def _validate_a5a(raw) -> float | None:
    """web_app.py:997-1013. A present-but-unusable value is REJECTED, never
    silently replaced: the A5a term is factor x floor area, so substituting a
    number the user never chose would move the result with nothing to show."""
    if raw is None or raw == '':
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise CalculationError(
            f'Site activity factor (A5a) must be a number — got "{raw}". '
            f'Leave it blank to use the SEAI default.') from None
    if not (A5A_MIN_KGCO2E_PER_SQM <= v <= A5A_MAX_KGCO2E_PER_SQM):
        raise CalculationError(
            f'Site activity factor (A5a) must be between '
            f'{A5A_MIN_KGCO2E_PER_SQM:g} and {A5A_MAX_KGCO2E_PER_SQM:g} '
            f'kgCO2e/m² GIA — got {v:g}.')
    return v


def _filter_geometry(geometry_data, config, elements_list) -> list:
    """web_app.py:1021-1055. Removed elements must be zero everywhere - no A4,
    no A5, and nothing left behind in the 3D viewer."""
    excluded_keys = set()
    for idx in (config.excluded_indices or []):
        try:
            i = int(idx)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(elements_list):
            e = elements_list[i]
            excluded_keys.add((e.get('cat', ''), e.get('name', '')))

    included = set(config.included_cats) if config.included_cats is not None else None
    excluded_levels = set(config.excluded_levels or [])

    out = []
    for g in (geometry_data or []):
        g_cat = g.get('category', '')
        if included is not None and g_cat not in included:
            continue
        if str(g.get('level', '') or '') in excluded_levels:
            continue
        if (g_cat, g.get('name', '')) in excluded_keys:
            continue
        out.append(g)
    return out


def _no_factor_summary(boq_df) -> dict:
    """web_app.py:1062-1079. Materials the taxonomy flags as having no emission
    factor calculate as 0 - surfaced so the dashboard warns rather than silently
    under-counting."""
    summary = {'count': 0, 'volume_m3': 0.0, 'categories': []}
    if 'ec_rate_available' not in boq_df.columns:
        return summary
    mask = ~boq_df['ec_rate_available'].fillna(True).astype(bool)
    if not mask.any():
        return summary
    nf = boq_df[mask]
    vol = pd.to_numeric(nf.get('Total Volume(m3)', pd.Series(0, index=nf.index)),
                        errors='coerce').fillna(0).sum()
    return {
        'count': int(mask.sum()),
        'volume_m3': round(float(vol), 1),
        'categories': sorted(set(
            nf.get('Category', pd.Series(dtype=str)).astype(str).tolist()))[:8],
    }


def _concrete_specs_used(boq_df, catalogue, config) -> list[dict]:
    """web_app.py:1102-1160. Derived from the concrete e_ids actually in the BOQ,
    not the Step-3 dropdown - per-element overrides and grades carried in the
    file re-map the e_id, so reading them back is the only way the report shows
    the GGBS that was really applied."""
    cat_col = next((c for c in ('Category', 'category')
                    if c in boq_df.columns), None)
    ordered: list[str] = []
    spec_cats: dict[str, set] = {}
    if 'e_id' in boq_df.columns:
        for idx in boq_df.index:
            eid = str(boq_df.at[idx, 'e_id'] or '').strip()
            if not eid.startswith('C_'):
                continue
            if eid not in spec_cats:
                spec_cats[eid] = set()
                ordered.append(eid)
            if cat_col:
                cv = str(boq_df.at[idx, cat_col] or '').strip()
                if cv:
                    spec_cats[eid].add(cv)

    specs = []
    for eid in ordered:
        grade_lbl, ggbs = _EID_TO_GRADE_GGBS.get(eid, (None, None))
        f = catalogue.get_emission_factor(eid) or {}
        if grade_lbl is None:
            grade_lbl, ggbs = str(f.get('mat_name', eid) or eid), 0
        cats = sorted(spec_cats.get(eid, set()))
        specs.append({
            'element': ('All members' if len(ordered) == 1
                        else (', '.join(cats) if cats else 'All members')),
            'grade': grade_lbl,
            'ggbs_pct': int(ggbs or 0),
            'a1_a3': float(f.get('a1_a3', 0) or 0),
        })

    if specs:
        return specs

    # Steel-only project: fall back to the global Step-3 selection.
    eid = catalogue.get_concrete_eid(config.concrete_grade, config.ggbs_pct) or 'C_020'
    ef = catalogue.get_emission_factor(eid)
    if ef is None:
        raise CalculationError(
            f"Emission factor not found for e_id '{eid}' — check the catalogue CSV.")
    return [{'element': 'All members', 'grade': config.concrete_grade,
             'ggbs_pct': config.ggbs_pct, 'a1_a3': ef['a1_a3']}]


def _emission_factors_used(boq_df, catalogue, config, steel_eid, section_eid,
                           pt_eid) -> list[dict]:
    """web_app.py:1162-1192. Feeds the assumptions panel and the EPD coverage %."""
    efs: list[dict] = []
    try:
        conc_eids = []
        if 'e_id' in boq_df.columns:
            for e in boq_df['e_id']:
                s = str(e or '').strip()
                if s.startswith('C_') and s not in conc_eids:
                    conc_eids.append(s)
        conc_map = [('Concrete', e) for e in conc_eids] or [
            ('Concrete', catalogue.get_concrete_eid(config.concrete_grade,
                                                    config.ggbs_pct) or 'C_020')]
        for mat, eid in conc_map + [('Rebar', steel_eid),
                                    ('Steel Section', section_eid),
                                    ('Post Tensioning', pt_eid)]:
            f = catalogue.get_emission_factor(eid) or {}
            efs.append({'material': mat, 'name': f.get('mat_name', eid) or eid,
                        'e_id': eid, 'a1_a3': f.get('a1_a3', 0),
                        'density': f.get('density', 0),
                        'data_type': f.get('data_type', 'Generic') or 'Generic',
                        'is_custom': False})

        mat_map = {'Concrete': 'Concrete', 'Rebar': 'Rebar',
                   'Steel': 'Steel Section', 'PT': 'Post Tensioning'}
        for _k, ov in (config.epd_overrides or {}).items():
            try:
                val = float(ov.get('a1_a3') or 0)
            except (TypeError, ValueError):
                val = 0.0
            m = mat_map.get(str(ov.get('mat_type', '') or ''), '')
            if val > 0 and m:
                efs.append({'material': m,
                            'name': str(ov.get('source') or 'Custom EPD override'),
                            'e_id': str(ov.get('e_id', '') or ''),
                            'a1_a3': val, 'density': 0,
                            'data_type': 'EPD (custom)', 'is_custom': True})
    except Exception as exc:
        print(f'[calculation] emission_factors_used build failed (non-fatal): {exc}')
    return efs


def _summary(results, stage_info, no_factor) -> dict:
    """web_app.py:1359-1400 - the headline figures."""
    from calculations import A5A_EMISSION_FACTOR_KGCO2E_PER_SQM as A5A_DEFAULT

    m = results.metrics
    per_sqm = m.get('total_emission_per_sqm', 0)
    lo, hi = uncertainty_range(per_sqm, stage_info['uncertainty_pct'])
    a5a = float(m.get('a5a_factor', A5A_DEFAULT) or A5A_DEFAULT)
    return {
        'total_ton': round(m.get('total_emission_ton', 0), 2),
        'per_sqm': round(per_sqm, 1),
        'rating': results.efficiency_rating,
        'rating_row_index': results.rating_row_index,
        'material_types': results.material_types,
        'material_emissions': [round(v, 2) for v in results.material_emissions],
        'stage_emissions': {k: round(v, 2)
                            for k, v in results.stage_emissions.items()},
        'stage_label': stage_info['label'],
        'stage_badge': stage_info['badge'],
        'uncertainty_pct': stage_info['uncertainty_pct'],
        'per_sqm_range': [round(lo, 1), round(hi, 1)],
        'n_sensitivity': len(results.sensitivity_results or {}),
        'catalogue_version': CATALOGUE_VERSION,
        'no_factor_count': no_factor['count'],
        'sequestration_ton': round(m.get('sequestration_ton', 0) or 0, 2),
        'a5a_factor': round(a5a, 1),
        'a5a_is_override': abs(a5a - A5A_DEFAULT) > 1e-6,
        'a5a_default': A5A_DEFAULT,
        'a5a_ton': round(float(m.get('a5a_emission_ton', 0) or 0), 1),
    }
