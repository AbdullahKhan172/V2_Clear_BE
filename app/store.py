"""
Run store - now database-backed.
================================
Same four verbs as the in-memory version it replaces (create / get / update /
delete), so api/runs.py and jobs/parse.py did not change when the database
landed. That was the point of putting this seam in from the start.

What lives where, and why:

  runs table       status, summary, project + wizard config    (small, queried)
  elements table   the 20 display fields, one row per element  (filtered, paged)
  blobs/           frame.parquet + geometry.json               (big, or typed)

The DataFrame is NOT reconstructed from the elements table. It carries columns
the UI contract omits - Waste, Density, is_structural_steel, has_modeled_rebar,
family, ifc_type, width/depth, weight_kg - and those decide which emission factor
each row receives. Rebuilding it from 20 fields would change the carbon results.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete as sa_delete, func, select

from app import blobs
from app.db import SessionLocal
from app.models import Element, ResultRow, Run

# Retention cap. Real deployments expire by age and owner instead; this keeps a
# single-user dev machine from growing without bound.
_MAX_RUNS = 50


def create(filename: str, ext: str, upload_key: str = '',
           gemini_key: str | None = None,
           project: dict | None = None,
           owner_id: str | None = None) -> Run:
    """Register a new run.

    gemini_key is deliberately NOT persisted - it is a user credential, and the
    parse job is handed it in memory. Once this is multi-user it becomes a
    server-side secret rather than a per-request field.
    """
    run = Run(
        id=str(uuid.uuid4()),
        owner_id=owner_id,
        filename=filename,
        ext=ext,
        upload_key=upload_key,
        status='created',
        project=project or {},
        config={},
        categories=[],
        default_rates={},
    )
    with SessionLocal() as s:
        s.add(run)
        s.commit()
        s.refresh(run)
    _evict_old(owner_id)
    return run


def get(run_id: str) -> Run | None:
    with SessionLocal() as s:
        return s.get(Run, run_id)


def get_for_owner(run_id: str, owner_id: str) -> Run | None:
    """A run this owner may see, or None. Read-only: never changes ownership.

    None covers both "no such run" and "not yours" deliberately: telling an
    unauthorised caller that a run EXISTS is itself a disclosure, and the API
    returns 404 for both cases.

    A legacy run (owner NULL) is readable by anyone. It is claimed when someone
    WRITES to it (see adopt_if_unowned), not when they read it - opening a link
    somebody shared should not quietly take the run away from them.
    """
    with SessionLocal() as s:
        run = s.get(Run, run_id)
        if run is None:
            return None
        return run if (run.owner_id is None or run.owner_id == owner_id) else None


def adopt_if_unowned(run_id: str, owner_id: str) -> None:
    """Claim a legacy run for whoever is editing it.

    Called from the WRITE paths only. A run created before ownership existed has
    no owner; the person who changes it is the one with a claim to it, not the
    first person to glance at it. Runs that already have an owner are untouched.
    """
    with SessionLocal() as s:
        run = s.get(Run, run_id)
        if run is not None and run.owner_id is None:
            run.owner_id = owner_id
            s.commit()


def list_for_owner(owner_id: str, *, limit: int = 50) -> list[dict]:
    """This owner's runs, newest first — the "your projects" list.

    Legacy runs (owner NULL) are included so nothing created before ownership
    existed disappears from view. They are adopted when opened, not when
    listed: listing should not mutate.
    """
    with SessionLocal() as s:
        rows = s.scalars(
            select(Run)
            .where((Run.owner_id == owner_id) | (Run.owner_id.is_(None)))
            .order_by(Run.created_at.desc())
            .limit(limit)
        ).all()
        return [r.listing() for r in rows]


def claim_runs(from_owner: str, to_owner: str) -> int:
    """Reassign every run from one owner to another. Returns how many moved.

    This is the sign-in migration: on a user's first login, the runs their
    BROWSER accumulated anonymously become theirs. Written now so the auth work
    is only an auth provider, not a data rescue.
    """
    if not from_owner or not to_owner or from_owner == to_owner:
        return 0
    with SessionLocal() as s:
        runs = s.scalars(select(Run).where(Run.owner_id == from_owner)).all()
        for run in runs:
            run.owner_id = to_owner
        s.commit()
        return len(runs)


def update(run_id: str, **fields) -> Run | None:
    """Set columns on a run. Unknown names are ignored, matching the old store."""
    with SessionLocal() as s:
        run = s.get(Run, run_id)
        if run is None:
            return None
        for key, value in fields.items():
            if hasattr(run, key):
                setattr(run, key, value)
        s.commit()
        s.refresh(run)
        return run


def save_extraction(run_id: str, result: Any) -> None:
    """Persist a finished extraction: summary to the run, rows to the elements
    table, DataFrame and meshes to blob storage.

    Ordering matters. Blobs are written first and the run is flipped to 'parsed'
    last, so a client that sees 'parsed' is guaranteed everything behind it is
    readable - there is no window where the status promises data that has not
    landed yet.
    """
    frame_key = None
    geometry_key = None
    if result.elements_df is not None and not result.elements_df.empty:
        frame_key = blobs.put_frame(run_id, result.elements_df)
    geometry_key = blobs.put_geometry(run_id, result.geometry_data)

    with SessionLocal() as s:
        run = s.get(Run, run_id)
        if run is None:
            return

        # Re-parsing the same upload replaces its rows rather than appending.
        s.execute(sa_delete(Element).where(Element.run_id == run_id))

        s.bulk_save_objects([
            Element(run_id=run_id, **{f: row.get(f) for f in Element.FIELDS})
            for row in result.elements
        ])

        run.source_type = result.source_type
        run.csv_fmt = result.csv_fmt
        run.n_elements = result.n_elements
        run.has_real_eids = result.has_real_eids
        run.categories = result.categories
        run.default_rates = result.default_rates
        run.frame_key = frame_key
        run.geometry_key = geometry_key
        run.status = 'parsed'
        run.error = None
        s.commit()


def get_elements(run_id: str, *, categories: list[str] | None = None,
                 levels: list[str] | None = None,
                 steel_only: bool | None = None,
                 limit: int | None = None, offset: int = 0) -> tuple[list[dict], int]:
    """Return (rows, total_matching).

    Filtering and paging happen in SQL rather than in Python: a real model runs
    to thousands of elements, and Step 2 slices them by category and level.
    """
    with SessionLocal() as s:
        stmt = select(Element).where(Element.run_id == run_id)
        if categories:
            stmt = stmt.where(Element.cat.in_(categories))
        if levels:
            stmt = stmt.where(Element.level.in_(levels))
        if steel_only is not None:
            stmt = stmt.where(Element.is_steel.is_(steel_only))

        total = s.scalar(
            select(func.count()).select_from(stmt.subquery())) or 0

        stmt = stmt.order_by(Element.idx)
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)

        return [e.public() for e in s.scalars(stmt)], total


def category_totals(run_id: str) -> list[dict]:
    """Per-category counts and volume - the numbers Step 2's bar chart needs.

    Aggregated in SQL so the browser never has to receive every row just to sum
    them.
    """
    with SessionLocal() as s:
        rows = s.execute(
            select(Element.cat,
                   func.count(Element.id),
                   func.coalesce(func.sum(Element.vol), 0.0),
                   func.coalesce(func.sum(Element.mass), 0.0))
            .where(Element.run_id == run_id)
            .group_by(Element.cat)
            .order_by(func.sum(Element.vol).desc())
        ).all()
    return [{'cat': c, 'count': n, 'volume_m3': round(v or 0, 3),
             'mass_kg': round(m or 0, 1)} for c, n, v, m in rows]


# Separator for a group key. Matches the legacy UI's grpKey() so a key written by
# either front end means the same thing. Chosen because it cannot occur in a real
# category, element name or level.
GROUP_SEP = '|||'


def group_key(cat: str, name: str, material: str, is_steel: bool,
              level: str) -> str:
    """Stable identity for one table row.

    Steel carries its material in the key so an explicit rebar/steel/PT row never
    merges into the concrete row of the same category and name - they are
    different quantities with different factors. Mirrors the legacy grpKey().

    Level is part of the key so the browser can switch a level off and recompute
    every total locally, with no request. Group without it and the totals become
    unfilterable, because a level cut changes what each row contains.
    """
    parts = [cat or '', name or '']
    if is_steel:
        parts.append(material or 'Steel')
    parts.append(level or '')
    return GROUP_SEP.join(parts)


def element_groups(run_id: str) -> list[dict]:
    """The Step-2 table, aggregated in SQL and fetched once.

    Deliberately carries NO element row numbers. Those are positions, and a
    position silently means something different if the file is ever re-parsed;
    a group key does not. The server expands keys back to rows at filter time
    (see resolve_excluded_indices), so a saved selection stays meaningful.
    """
    with SessionLocal() as s:
        rows = s.execute(
            select(Element.cat, Element.name, Element.material,
                   Element.is_steel, Element.level,
                   func.coalesce(func.sum(Element.vol), 0.0),
                   func.coalesce(func.sum(Element.count), 0),
                   func.count(Element.id),
                   func.coalesce(func.sum(Element.mass), 0.0))
            .where(Element.run_id == run_id)
            .group_by(Element.cat, Element.name, Element.material,
                      Element.is_steel, Element.level)
            .order_by(Element.cat, Element.name, Element.level)
        ).all()

    return [{
        'key': group_key(cat, name, material, bool(is_steel), level),
        'cat': cat, 'name': name, 'material': material,
        'is_steel': bool(is_steel), 'level': level,
        'vol': round(vol or 0, 4),
        'count': int(cnt or 0),
        'n_elements': int(n or 0),
        'mass': round(mass or 0, 2),
    } for cat, name, material, is_steel, level, vol, cnt, n, mass in rows]


def level_totals(run_id: str) -> list[dict]:
    """Distinct levels with counts and volume - powers the level filter.

    The legacy backend has always supported excluding levels; the legacy UI has
    no control that populates it. This is what that missing control needs.
    """
    with SessionLocal() as s:
        rows = s.execute(
            select(Element.level,
                   func.count(Element.id),
                   func.coalesce(func.sum(Element.vol), 0.0))
            .where(Element.run_id == run_id)
            .group_by(Element.level)
            .order_by(Element.level)
        ).all()
    return [{'level': lv or '', 'count': n, 'volume_m3': round(v or 0, 3)}
            for lv, n, v in rows]


def resolve_excluded_indices(run_id: str, selection: dict) -> list[int]:
    """Turn a saved selection into the positional row numbers the engine filters on.

    The browser stores WHAT was excluded (categories, levels, named groups); the
    calculation needs WHICH rows. Resolving here, at calculation time, means the
    stored selection never goes stale - re-parse the file and the same names
    still resolve, to whatever rows they now occupy.
    """
    excluded_cats = set(selection.get('excluded_categories') or [])
    excluded_levels = set(selection.get('excluded_levels') or [])
    excluded_groups = set(selection.get('excluded_groups') or [])
    if not (excluded_cats or excluded_levels or excluded_groups):
        return []

    with SessionLocal() as s:
        rows = s.execute(
            select(Element.idx, Element.cat, Element.name, Element.material,
                   Element.is_steel, Element.level)
            .where(Element.run_id == run_id)
        ).all()

    return sorted(
        idx for idx, cat, name, material, is_steel, level in rows
        if (cat in excluded_cats
            or level in excluded_levels
            or group_key(cat, name, material, bool(is_steel), level)
            in excluded_groups)
    )


def get_selection(run_id: str) -> dict:
    """The saved Step-2 selection, with empty defaults."""
    run = get(run_id)
    stored = (run.config or {}).get('selection', {}) if run else {}
    return {
        'excluded_categories': stored.get('excluded_categories', []),
        'excluded_levels': stored.get('excluded_levels', []),
        'excluded_groups': stored.get('excluded_groups', []),
    }


def save_selection(run_id: str, selection: dict) -> dict:
    """Persist the Step-2 selection.

    Written on every change so a closed tab or a refresh costs nothing. Stored
    under config['selection'] rather than as columns because it is a list-shaped
    choice, not a queryable fact - and because steps 3-5 will add their own keys
    beside it.
    """
    run = get(run_id)
    if run is None:
        return {}
    clean = {
        'excluded_categories': [str(x) for x in
                                (selection.get('excluded_categories') or [])],
        'excluded_levels': [str(x) for x in
                            (selection.get('excluded_levels') or [])],
        'excluded_groups': [str(x) for x in
                            (selection.get('excluded_groups') or [])],
    }
    # Replace the whole dict: SQLAlchemy does not track in-place mutation of a
    # JSON column, so mutating run.config would not be written back.
    config = dict(run.config or {})
    config['selection'] = clean
    update(run_id, config=config)
    return clean


def material_key(cat: str, name: str, material: str, is_steel: bool) -> str:
    """Identity of one MATERIALS row.

    Deliberately different from group_key(): no level. Step 3 sets a grade per
    element TYPE, not per storey, so rows merge across levels here.

    The format must stay exactly `cat|||name` for concrete, because the engine
    splits per-element overrides on the FIRST separator
    (web_app.py:756 - `e_cat, e_name = elem_key.split('|||', 1)`). Steel appends
    its material so an explicit rebar/steel/PT row never merges into the concrete
    row of the same name - steel is routed by `steel_assignments`, which is keyed
    on (cat, name), so the longer key never reaches that split.
    """
    return (f'{cat}{GROUP_SEP}{name}{GROUP_SEP}{material or "Steel"}'
            if is_steel else f'{cat}{GROUP_SEP}{name}')


def material_rows(run_id: str) -> list[dict]:
    """The Step-3 table: elements that survived Step 2, merged across levels.

    Volume and count are summed; the per-element hints (rebar default, PT
    applicability, whether the full-catalogue picker is offered) are taken from
    the first row of each group, exactly as the legacy UI does - they are
    properties of the element TYPE, identical across its instances.
    """
    selection = get_selection(run_id)
    excluded_cats = set(selection['excluded_categories'])
    excluded_levels = set(selection['excluded_levels'])
    excluded_groups = set(selection['excluded_groups'])

    with SessionLocal() as s:
        elements = list(s.scalars(
            select(Element).where(Element.run_id == run_id)
            .order_by(Element.idx)))

    merged: dict[str, dict] = {}
    for e in elements:
        if e.cat in excluded_cats or e.level in excluded_levels:
            continue
        if group_key(e.cat, e.name, e.material, e.is_steel,
                     e.level) in excluded_groups:
            continue

        key = material_key(e.cat, e.name, e.material, e.is_steel)
        row = merged.get(key)
        if row is None:
            row = {
                'key': key,
                'cat': e.cat, 'name': e.name, 'material': e.material,
                'is_steel': e.is_steel,
                'vol': 0.0, 'count': 0, 'mass': 0.0,
                # Element-type properties - same for every instance.
                'e_id': e.e_id,
                'factor_type': e.factor_type,
                'grade': e.grade,
                'ggbs': e.ggbs,
                'default_rebar': e.default_rebar,
                'rebar_hint': e.rebar_hint,
                'pt_applicable': e.pt_applicable,
                'allow_full_factor': e.allow_full_factor,
                'kg_per_m_hint': e.kg_per_m_hint,
                'mass_source': e.mass_source,
            }
            merged[key] = row
        row['vol'] += e.vol or 0.0
        row['count'] += e.count or 0
        row['mass'] += e.mass or 0.0

    for row in merged.values():
        row['vol'] = round(row['vol'], 4)
        row['mass'] = round(row['mass'], 2)

    return sorted(merged.values(), key=lambda r: (r['cat'], r['name']))


def get_materials(run_id: str) -> dict:
    """Saved Step-3 settings, with empty defaults."""
    run = get(run_id)
    stored = (run.config or {}).get('materials', {}) if run else {}
    return {
        'defaults': stored.get('defaults', {}),
        'rows': stored.get('rows', {}),
        'epd_overrides': stored.get('epd_overrides', {}),
    }


def save_materials(run_id: str, materials: dict) -> dict:
    """Persist Step-3 settings alongside the Step-2 selection.

    Stored whole rather than merged per field: the client holds the complete
    state, and a partial merge would make removing an override impossible.
    """
    run = get(run_id)
    if run is None:
        return {}
    clean = {
        'defaults': dict(materials.get('defaults') or {}),
        'rows': {str(k): dict(v) for k, v in
                 (materials.get('rows') or {}).items()},
        'epd_overrides': {str(k): dict(v) for k, v in
                          (materials.get('epd_overrides') or {}).items()},
    }
    config = dict(run.config or {})
    config['materials'] = clean
    update(run_id, config=config)
    return clean


# ── Step 4: transport, waste, site activity ─────────────────────────────────
# Waste passes through TWO conversions between the screen and the engine, and
# skipping either changes the A5w emission without any error:
#
#     screen   5      percent      what the user types and reads
#     wire     1.05   multiplier   waste_factors, as /run receives them
#     engine   0.05   fraction     a5w_waste_pcts, what calculate_emissions uses
#
# The percent is stored (it is what was chosen, and it is readable in the
# database); both conversions live in the two helpers below and nowhere else.

def waste_pct_to_multiplier(waste_pct: dict) -> dict:
    """Percent -> multiplier: 5 -> 1.05. The shape /run expects."""
    return {k: 1.0 + (float(v) / 100.0) for k, v in (waste_pct or {}).items()}


def waste_pct_to_fraction(waste_pct: dict) -> dict:
    """Percent -> fraction: 5 -> 0.05. What the engine's a5w_waste_pcts wants.

    Negative waste is meaningless, so it clamps at zero - matching
    web_app.py:990, which does `max(multiplier - 1.0, 0.0)`.
    """
    return {k: max(float(v) / 100.0, 0.0) for k, v in (waste_pct or {}).items()}


def distances_to_engine(distances: dict) -> dict:
    """Nested per-family distances -> the flat `<family>_sea_distance` keys the
    engine reads (calculations._transport_distances).

    Families the user never touched are simply absent, and the engine falls back
    to its SEAI default for them - the same behaviour as the legacy wizard, which
    never rendered blockwork or sheet steel at all.
    """
    out: dict[str, float] = {}
    for family, legs in (distances or {}).items():
        if not isinstance(legs, dict):
            continue
        if legs.get('sea') is not None:
            out[f'{family}_sea_distance'] = float(legs['sea'])
        if legs.get('road') is not None:
            out[f'{family}_road_distance'] = float(legs['road'])
    return out


def get_transport(run_id: str) -> dict:
    """Saved Step-4 values. Empty dicts mean "nothing chosen - use the defaults"."""
    run = get(run_id)
    stored = (run.config or {}).get('transport', {}) if run else {}
    return {
        'distances': stored.get('distances', {}),
        'waste_pct': stored.get('waste_pct', {}),
        # None (not 0) means "not overridden": 0 is a legitimate A5a value,
        # meaning site activity is deliberately excluded or reported elsewhere.
        'a5a_factor': stored.get('a5a_factor'),
    }


def save_transport(run_id: str, transport: dict) -> dict:
    """Persist Step-4 values alongside the selection and materials."""
    run = get(run_id)
    if run is None:
        return {}
    clean = {
        'distances': {
            str(fam): {'sea': float(legs.get('sea', 0) or 0),
                       'road': float(legs.get('road', 0) or 0)}
            for fam, legs in (transport.get('distances') or {}).items()
            if isinstance(legs, dict)
        },
        'waste_pct': {str(k): float(v)
                      for k, v in (transport.get('waste_pct') or {}).items()},
        'a5a_factor': (None if transport.get('a5a_factor') is None
                       else float(transport['a5a_factor'])),
    }
    config = dict(run.config or {})
    config['transport'] = clean
    update(run_id, config=config)
    return clean


# ── Wizard progress ─────────────────────────────────────────────────────────
# HOW FAR the user got - and only that.
#
# The current step is deliberately NOT stored. The URL already carries it, which
# covers a refresh and the browser's back/forward; the only other way back into a
# run is "your projects", which resumes to the furthest step reached. Recording
# the current position as well meant a write on every Back and every click in the
# step rail, to save a number nothing read.
#
# What must survive is the high-water mark, because it is what the rail enables
# and a URL cannot carry it to a new device.

WIZARD_STEPS = 6


def get_progress(run_id: str) -> dict:
    """The furthest step this run has reached.

    Floored by Run.implied_max_step(): a run that has been parsed has finished
    Step 1 whether or not anyone recorded that, and a calculated run has been
    through all six. Without that floor, every run created before progress was
    tracked opens locked to Step 1 with no way forward.
    """
    run = get(run_id)
    if run is None:
        return {'max_step': 1}
    stored = (run.config or {}).get('wizard', {})
    return {'max_step': max(_clamp_step(stored.get('max_step', 1)),
                            run.implied_max_step())}


def save_progress(run_id: str, reached: int) -> dict:
    """Raise the high-water mark. Never lowers it.

    Called only when the user opens a step they have not opened before, so a run
    records at most five of these in its life rather than one per click.
    Re-recording a step already reached would tell the server nothing, and
    LOWERING the mark would re-lock every step ahead.
    """
    run = get(run_id)
    if run is None:
        return {'max_step': 1}
    max_step = max(_clamp_step(stored_max(run)), _clamp_step(reached),
                   run.implied_max_step())

    config = dict(run.config or {})
    config['wizard'] = {'max_step': max_step}
    update(run_id, config=config)
    return {'max_step': max_step}


def stored_max(run) -> int:
    """The recorded high-water mark, ignoring any legacy `step` key."""
    return int(((run.config or {}).get('wizard') or {}).get('max_step') or 1)


def _clamp_step(value) -> int:
    """Coerce anything to a real step number. The step arrives from a URL."""
    try:
        return max(1, min(WIZARD_STEPS, int(value)))
    except (TypeError, ValueError):
        return 1


# ── Step 6: calculation results ─────────────────────────────────────────────

def save_results(run_id: str, result) -> None:
    """Persist a finished calculation.

    Ordering matters, as with save_extraction: the rows and the results JSON land
    BEFORE the status flips to 'complete', so a client that sees 'complete' can
    always read everything behind it.
    """
    from datetime import datetime, timezone

    summary = result.summary
    data = result.data
    detailed = data.detailed_data or []
    stages = data.stage_emissions or {}

    with SessionLocal() as s:
        run = s.get(Run, run_id)
        if run is None:
            return

        # Re-calculating replaces the previous rows rather than appending.
        s.execute(sa_delete(ResultRow).where(ResultRow.run_id == run_id))
        # `volume` is not in prepare_detailed_data's output, so it is taken from
        # the detailed frame alongside it - the two are in the same row order.
        volumes = []
        ddf = data.detailed_df
        if ddf is not None and 'Total Volume(m3)' in ddf.columns:
            import pandas as _pd
            volumes = _pd.to_numeric(
                ddf['Total Volume(m3)'], errors='coerce').fillna(0).tolist()

        s.bulk_save_objects([
            ResultRow(run_id=run_id, idx=i,
                      volume=round(float(volumes[i]), 6) if i < len(volumes) else 0.0,
                      **{f: row.get(f) for f in ResultRow.FIELDS
                         if f not in ('idx', 'volume')})
            for i, row in enumerate(detailed)
        ])

        run.total_ton = summary['total_ton']
        run.per_sqm = summary['per_sqm']
        run.rating = summary['rating']
        run.rating_row_index = summary.get('rating_row_index')
        run.a1_a3_t = round(float(stages.get('A1-A3', 0) or 0), 6)
        run.a4_t = round(float(stages.get('A4', 0) or 0), 6)
        run.a5_t = round(float(stages.get('A5', 0) or 0), 6)
        run.sequestration_t = summary.get('sequestration_ton')
        run.a5a_factor = summary.get('a5a_factor')
        run.a5a_ton = summary.get('a5a_ton')
        run.uncertainty_pct = summary.get('uncertainty_pct')
        run.catalogue_version = summary.get('catalogue_version')
        run.calculated_at = datetime.now(timezone.utc)

        # Sensitivity and the assumptions tables are read whole, never queried
        # across, so they live in JSON beside the wizard config.
        config = dict(run.config or {})
        config['results'] = {
            'sensitivity': data.sensitivity_results or {},
            # The calculate job saves TWICE: headline figures first so the
            # dashboard paints immediately, then again once the decarbonisation
            # scenarios finish. Without this flag a client that polls for
            # 'complete' can land in the gap and render "no scenarios" for a run
            # that is merely still computing them.
            'sensitivity_status': 'ready' if data.sensitivity_results else 'pending',
            'no_factor': result.no_factor,
            # Zone / floor intensity, slab-system EC and the two member proxies.
            # Derived here rather than per request because the slab figure reads
            # the geometry blob, which is large. Never fatal: the dashboard
            # simply omits those panels if it is absent.
            'insights': _safe_insights(result),
            'concrete_specs_used': result.concrete_specs_used,
            'emission_factors_used': result.emission_factors_used,
            'rebar_rates_used': result.rebar_rates_used,
            'material_types': summary.get('material_types', []),
            'material_emissions': summary.get('material_emissions', []),
            'per_sqm_range': summary.get('per_sqm_range', []),
            'stage_label': summary.get('stage_label', ''),
            'stage_badge': summary.get('stage_badge', ''),
        }
        run.config = config
        run.status = 'complete'
        run.error = None
        s.commit()

    # Built AFTER the commit, and outside the session: it is a derived artefact,
    # it can take seconds on a real model, and holding a transaction open for it
    # would block the reads the dashboard is about to make.
    _safe_viewer(run_id, result)


def _safe_viewer(run_id: str, result) -> None:
    """Precompute what the 3D viewer draws, so a dashboard load is a file read.

    The join is real work - every mesh matched to its priced rows by stripped
    name - and it used to run on the REQUEST thread, every single time the
    dashboard was opened: 6.2 seconds on a 36-element model, while the user
    waited. Both inputs are frozen once the calculation finishes, so it produced
    an identical answer every time.

    Uses the geometry as EXTRACTED, not the filtered copy the calculation ran on.
    That is deliberate and load-bearing: an element excluded in Step 2 must stay
    visible in the model reading zero, so the cost of a filtering decision is
    seen rather than hidden. Passing the filtered geometry would make excluded
    elements vanish instead.

    Never allowed to fail a calculation - it is a picture, not a number.
    """
    try:
        from app.services.dashboard import build_geometry_payload
        run = get(run_id)
        if run is None or not run.geometry_key:
            return                                  # a spreadsheet run: no 3D
        geometry = blobs.get_geometry(run.geometry_key)
        if not geometry:
            return
        elements = build_geometry_payload(geometry, result.data.detailed_df)
        blobs.put_viewer(run_id, {
            'run_id': run_id,
            'elements': elements,
            'has_meshes': any('v' in e for e in elements),
            'count': len(elements),
        })
    except Exception as exc:
        print(f'[store] viewer payload build failed (non-fatal): {exc}')


def _safe_insights(result) -> dict:
    """Derived insight panels; never allowed to fail a calculation.

    These are analysis, not the result. A headline total that is correct must
    not be lost because a keyword heuristic tripped over an odd element name.
    """
    try:
        from app.services.dashboard import build_insights
        return build_insights(result)
    except Exception as exc:
        print(f'[store] insights build failed (non-fatal): {exc}')
        return {}


def get_results(run_id: str) -> dict | None:
    """Headline figures plus the derived tables. None until calculated."""
    run = get(run_id)
    if run is None or run.calculated_at is None:
        return None
    stored = (run.config or {}).get('results', {})
    return {
        'total_ton': run.total_ton,
        'per_sqm': run.per_sqm,
        'per_sqm_range': stored.get('per_sqm_range', []),
        'rating': run.rating,
        'rating_row_index': run.rating_row_index,
        'stage_emissions': {'A1-A3': run.a1_a3_t, 'A4': run.a4_t, 'A5': run.a5_t},
        'sequestration_ton': run.sequestration_t,
        'a5a_factor': run.a5a_factor,
        'a5a_ton': run.a5a_ton,
        'uncertainty_pct': run.uncertainty_pct,
        'catalogue_version': run.catalogue_version,
        'calculated_at': run.calculated_at.isoformat(),
        'material_types': stored.get('material_types', []),
        'material_emissions': stored.get('material_emissions', []),
        'stage_label': stored.get('stage_label', ''),
        'stage_badge': stored.get('stage_badge', ''),
        'sensitivity': stored.get('sensitivity', {}),
        'sensitivity_status': stored.get('sensitivity_status', 'ready'),
        'no_factor': stored.get('no_factor', {}),
        'insights': stored.get('insights', {}),
        'concrete_specs_used': stored.get('concrete_specs_used', []),
        'emission_factors_used': stored.get('emission_factors_used', []),
        'rebar_rates_used': stored.get('rebar_rates_used', []),
    }


def result_rows(run_id: str, *, limit: int | None = None,
                offset: int = 0) -> tuple[list[dict], int]:
    """Priced BOQ lines, biggest contributor first."""
    with SessionLocal() as s:
        base = select(ResultRow).where(ResultRow.run_id == run_id)
        total = s.scalar(select(func.count()).select_from(base.subquery())) or 0
        stmt = base.order_by(ResultRow.total_kg.desc())
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return [r.public() for r in s.scalars(stmt)], total


_BREAKDOWN_FIELDS = {'material': ResultRow.material,
                     'category': ResultRow.category,
                     'level': ResultRow.level}


def result_breakdown(run_id: str, by: str = 'material') -> list[dict]:
    """Emissions grouped by material, category or level - the chart queries.

    Aggregated in SQL so a new chart is a new query, not a re-calculation.
    """
    col = _BREAKDOWN_FIELDS.get(by)
    if col is None:
        raise ValueError(f'Cannot group by {by!r}; '
                         f'choose one of {sorted(_BREAKDOWN_FIELDS)}.')
    with SessionLocal() as s:
        rows = s.execute(
            select(col,
                   func.count(ResultRow.id),
                   func.coalesce(func.sum(ResultRow.mass), 0.0),
                   func.coalesce(func.sum(ResultRow.a1_a3), 0.0),
                   func.coalesce(func.sum(ResultRow.a4), 0.0),
                   func.coalesce(func.sum(ResultRow.a5), 0.0),
                   func.coalesce(func.sum(ResultRow.total_kg), 0.0),
                   func.coalesce(func.sum(ResultRow.sequestration), 0.0))
            .where(ResultRow.run_id == run_id)
            .group_by(col)
            .order_by(func.sum(ResultRow.total_kg).desc())
        ).all()
    return [{
        'key': k or '(unassigned)', 'rows': n,
        'mass_kg': round(mass or 0, 1),
        # kgCO2e per stage; tonnes for the total - the units each chart uses.
        'a1_a3': round(a13 or 0, 3), 'a4': round(a4 or 0, 3),
        'a5': round(a5 or 0, 3),
        'total_kg': round(tot or 0, 3), 'total_ton': round((tot or 0) / 1000, 4),
        'sequestration': round(seq or 0, 3),
    } for k, n, mass, a13, a4, a5, tot, seq in rows]


# ── Step 6.3: generated report files ────────────────────────────────────────
# The manifest lives in config rather than its own table: it is a handful of
# entries, always read whole, and never queried across. The files themselves are
# blobs (local now, object storage later), referenced by key.

def get_reports(run_id: str) -> dict:
    """Manifest of generated reports, plus the state of the last generation."""
    run = get(run_id)
    stored = (run.config or {}).get('reports', {}) if run else {}
    return {
        'files': stored.get('files', {}),
        'status': stored.get('status', 'idle'),
        'error': stored.get('error'),
        'requested': stored.get('requested', []),
    }


def save_reports(run_id: str, files: dict) -> dict:
    """Record generated reports and mark the run's report state as ready."""
    run = get(run_id)
    if run is None:
        return {}
    config = dict(run.config or {})
    config['reports'] = {'files': files, 'status': 'ready', 'error': None,
                         'requested': list(files)}
    update(run_id, config=config)
    return files


def set_report_status(run_id: str, status: str, *, kinds: list[str] | None = None,
                      error: str | None = None) -> None:
    """Track generation separately from the run's own status.

    Deliberately NOT reusing run.status: generating a report must not make a
    completed run look unfinished, and a failed report must not mark a valid
    calculation as failed.
    """
    run = get(run_id)
    if run is None:
        return
    config = dict(run.config or {})
    reports = dict(config.get('reports') or {})
    reports['status'] = status
    reports['error'] = error
    if kinds is not None:
        reports['requested'] = list(kinds)
    reports.setdefault('files', {})
    config['reports'] = reports
    update(run_id, config=config)


def get_frame(run_id: str):
    """The DataFrame the calculation consumes, dtype-exact. None if not parsed."""
    run = get(run_id)
    if run is None or not run.frame_key:
        return None
    return blobs.get_frame(run.frame_key)


def get_geometry(run_id: str) -> list:
    run = get(run_id)
    return blobs.get_geometry(run.geometry_key) if run else []


def delete(run_id: str) -> None:
    with SessionLocal() as s:
        run = s.get(Run, run_id)
        if run is None:
            return
        s.delete(run)          # cascade removes the element rows
        s.commit()
    # One call: the upload lives under the run's own prefix with everything else.
    blobs.delete_run(run_id)


def _evict_old(owner_id: str | None = None) -> None:
    """Drop this OWNER's oldest runs beyond the cap, with uploads and blobs.

    Per owner, not global. A global count would mean one busy account evicting
    another account's work - which is a data-loss bug that only appears once
    there is more than one user, i.e. exactly when it is hardest to notice.
    """
    if owner_id is None:
        return
    with SessionLocal() as s:
        scope = select(Run).where(Run.owner_id == owner_id)
        total = s.scalar(
            select(func.count()).select_from(scope.subquery())) or 0
        if total <= _MAX_RUNS:
            return
        stale = s.scalars(
            scope.order_by(Run.created_at).limit(total - _MAX_RUNS)
        ).all()
        victims = [r.id for r in stale]
        for run in stale:
            s.delete(run)
        s.commit()

    for victim in victims:
        blobs.delete_run(victim)


def mark_sensitivity(run_id: str, status: str) -> None:
    """Record how the decarbonisation pass ended, independently of the run.

    A failed sensitivity pass must not mark a valid calculation as failed - the
    headline figures are already saved and correct.
    """
    run = get(run_id)
    if run is None:
        return
    config = dict(run.config or {})
    results = dict(config.get('results') or {})
    results['sensitivity_status'] = status
    config['results'] = results
    update(run_id, config=config)
