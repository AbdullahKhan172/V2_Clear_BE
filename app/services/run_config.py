"""
Stored wizard config -> RunConfig.
==================================
Translates what steps 1-4 saved in the database into the field names the
calculation expects. Kept separate from calculation.py on purpose: that module
holds logic moved verbatim from the legacy route, and mixing a rename in there
would be indistinguishable from a behaviour change.

This is a faithful port of the legacy front end's payload construction
(templates/web_ui.html:2092-2180). The subtle parts, all of which change the
carbon result if lost:

  * INCLUDED, not excluded. The engine takes a list of categories to KEEP;
    the wizard stores what was switched off. Inverted here against the run's
    full category list.

  * Rates are keyed BY NAME, element materials by "cat|||name". The engine looks
    up both, most specific first, so both are emitted exactly as the legacy did.

  * Composite-factor guard. A factor that already prices its reinforcement
    (precast, prestressed hollowcore) must be sent with rebar and PT rates of
    ZERO, or the steel is counted twice - once inside the factor and once as a
    rate-based row. The backend guards this too; the legacy front end also sent
    zeros, and this does the same so both paths agree.

  * Non-in-situ factors carry no rebar/PT at all. Blockwork and steel factors
    picked for a concrete row are not reinforced concrete.
"""

from __future__ import annotations

from app import core_bridge  # noqa: F401  - puts src/core on sys.path

from catalogue import EmissionCatalogue
from web_helpers import _build_distances

from app import store
from app.services.calculation import RunConfig


def build_run_config(run, *, sensitivity: bool = True,
                     catalogue: EmissionCatalogue | None = None) -> RunConfig:
    """Assemble a RunConfig from everything stored against `run`."""
    catalogue = catalogue or EmissionCatalogue()
    project = run.project or {}
    config = run.config or {}
    materials = config.get('materials') or {}
    transport = config.get('transport') or {}
    selection = config.get('selection') or {}

    defaults = materials.get('defaults') or {}
    overrides = materials.get('rows') or {}

    rows = store.material_rows(run.id)
    factor_meta = _factor_metadata(catalogue)

    steel_rates: dict[str, float] = {}
    pt_rates: dict[str, float] = {}
    element_materials: dict[str, dict] = {}
    steel_assignments: list[dict] = []
    factor_overrides: dict[str, str] = {}

    global_grade = str(defaults.get('concrete_grade', '32/40'))
    global_ggbs = int(defaults.get('ggbs_pct', 0) or 0)

    for r in rows:
        ov = overrides.get(r['key'], {})
        grade = ov.get('grade') or r.get('grade') or global_grade
        ggbs = int(ov.get('ggbs') if ov.get('ggbs') is not None
                   else (r.get('ggbs') if r.get('ggbs') is not None
                         else global_ggbs))
        rebar_rate = float(ov.get('rebarRate') if ov.get('rebarRate') is not None
                           else r.get('default_rebar') or 0)
        pt_rate = float(ov.get('ptRate') or 0)
        name = r['name']

        # ── Unknown-material row given any catalogue factor ─────────────
        if r['allow_full_factor'] and not r['is_steel']:
            full_eid = ov.get('fullEid')
            if full_eid:
                factor_overrides[r['key']] = full_eid
                # Only a CONCRETE factor carries separate rebar / PT.
                if str(full_eid).startswith('C_'):
                    inc = factor_meta.get(full_eid, {}).get('inc_reinf', False)
                    rr = 0.0 if inc else rebar_rate
                    pr = 0.0 if inc else pt_rate
                    steel_rates[name] = rr
                    pt_rates[name] = pr
                    element_materials[r['key']] = {
                        'grade': grade, 'ggbs': ggbs,
                        'rebarRate': rr, 'ptRate': pr}
                continue
            # No factor chosen yet - fall through and treat as ordinary concrete.

        if not r['is_steel']:
            conc_eid = ov.get('concFactorEid')
            if conc_eid:
                factor_overrides[r['key']] = conc_eid
                meta = factor_meta.get(conc_eid, {})
                keep = meta.get('is_insitu', False) and not meta.get('inc_reinf', False)
                rr = rebar_rate if keep else 0.0
                pr = pt_rate if keep else 0.0
                steel_rates[name] = rr
                pt_rates[name] = pr
                element_materials[r['key']] = {
                    'grade': grade, 'ggbs': ggbs, 'rebarRate': rr, 'ptRate': pr}
                continue

            # ── Auto-routed factor, untouched by the user ────────────────
            # Extraction routes hollowcore / precast members and timber to their
            # OWN catalogue factor, which is a statement about what the element
            # is, not a grade choice. Such a row must not be given a
            # grade-derived e_id: the engine treats any member_eids entry as
            # "the user assigned this factor themselves, which always wins"
            # (engine.py:550-561) and then skips BOTH its precast/timber
            # auto-routing AND the composite-factor guard that suppresses
            # rate-based rebar. Overriding it therefore re-prices a hollowcore
            # plank as in-situ concrete and adds reinforcement the factor
            # already includes - which is exactly what the legacy wizard
            # avoided by leaving these rows unassigned.
            #
            # A PLAIN in-situ row (or one with no factor yet) still falls
            # through below and follows the chosen grade, as it always has.
            auto_meta = factor_meta.get(str(r.get('e_id') or '').strip())
            auto_routed = bool(auto_meta) and not (
                auto_meta.get('is_insitu') and not auto_meta.get('inc_reinf'))
            if auto_routed:
                if auto_meta.get('inc_reinf'):
                    # The factor prices its own reinforcement. Report zero so
                    # the assumptions panel quotes the rate actually applied.
                    rebar_rate = pt_rate = 0.0
                steel_rates[name] = rebar_rate
                pt_rates[name] = pt_rate
                continue

            rr, pr = rebar_rate, pt_rate
            steel_rates[name] = rr
            pt_rates[name] = pr
            element_materials[r['key']] = {
                'grade': grade, 'ggbs': ggbs, 'rebarRate': rr, 'ptRate': pr}
            continue

        # ── Steel row: only its factor assignment travels ───────────────
        steel_eid = ov.get('steelEid') or r.get('e_id')
        if steel_eid:
            steel_assignments.append(
                {'cat': r['cat'], 'name': name, 'e_id': steel_eid})

    return RunConfig(
        project_name=str(project.get('name') or 'Untitled'),
        area=float(project.get('area') or 0),
        stage=str(project.get('stage') or 'Concept / Schematic Design'),
        structural_system=str(project.get('structural_system') or ''),
        location=str(project.get('location') or 'Not specified'),
        client=str(project.get('client') or 'Not specified'),

        included_cats=_included_categories(run.id, selection),
        excluded_indices=store.resolve_excluded_indices(run.id, selection),
        excluded_levels=list(selection.get('excluded_levels') or []) or None,
        manual_rows=list(config.get('manual_rows') or []),

        concrete_grade=global_grade,
        ggbs_pct=global_ggbs,
        rebar_type=str(defaults.get('rebar_type') or ''),
        section_type=str(defaults.get('section_type') or ''),
        pt_type=str(defaults.get('pt_type') or ''),
        steel_rates=steel_rates,
        pt_rates=pt_rates,
        element_materials=element_materials,
        steel_assignments=steel_assignments,
        factor_overrides=factor_overrides,
        epd_overrides=materials.get('epd_overrides') or {},

        distances=_distances(transport),
        waste_factors=store.waste_pct_to_multiplier(
            transport.get('waste_pct') or {}),
        a5a_factor=transport.get('a5a_factor'),

        sensitivity=sensitivity,
    )


def _included_categories(run_id: str, selection: dict) -> list[str] | None:
    """Invert the stored exclusions into the inclusion list the engine wants.

    None means "everything", which is what the engine treats as no filter -
    distinct from an empty list, which would exclude the entire model.
    """
    excluded = set(selection.get('excluded_categories') or [])
    if not excluded:
        return None
    all_cats = [c['cat'] for c in store.category_totals(run_id)]
    return [c for c in all_cats if c not in excluded]


def _distances(transport: dict) -> dict:
    """SEAI defaults with the user's per-family overrides laid over the top.

    Starting from _build_distances({}) rather than an empty dict matters: the
    engine falls back to its own defaults for a missing family, but the reports
    quote whatever is in this dict, so it must be complete.
    """
    distances = _build_distances({})
    distances.update(store.distances_to_engine(transport.get('distances') or {}))
    return distances


_FACTOR_META: dict[str, dict] | None = None


def _factor_metadata(catalogue: EmissionCatalogue) -> dict[str, dict]:
    """e_id -> {inc_reinf, is_insitu}. Cached: the catalogue does not change
    between runs, and this is consulted once per element row."""
    global _FACTOR_META
    if _FACTOR_META is None:
        meta = {}
        for e_id in catalogue.get_raw_dataframe().index:
            e = str(e_id)
            meta[e] = {
                'inc_reinf': catalogue.factor_includes_reinforcement(e),
                'is_insitu': catalogue.factor_family(e) == 'in_situ',
            }
        _FACTOR_META = meta
    return _FACTOR_META
