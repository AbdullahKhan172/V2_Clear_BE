"""
Dashboard data - everything the React dashboard reads.
======================================================
Aggregations over result_rows plus the 3D viewer payload. No calculation
happens here: a run is calculated once, and the dashboard is pure reads.

The geometry payload is built by REUSING templates/html_template_2's own
_prepare_geometry_json_mesh, not by reimplementing it. That function carries
real domain logic - it matches each 3D element to its priced BOQ rows by
stripped name and divides by the instance count, and it deliberately gives an
element EXCLUDED in Step 2 a value of zero rather than a category average, so
the cost of a filtering decision is never hidden. It is a pure function with no
Flask dependency, so importing it keeps one implementation instead of two that
can drift.
"""

from __future__ import annotations

from app import core_bridge  # noqa: F401  - puts src/core on sys.path

# Substructure = everything at or below ground. The split matters because
# foundations are often assessed and value-engineered separately from the frame.
SUBSTRUCTURE_CATEGORIES = {
    'Foundation/Footing', 'Pile', 'Pile Cap', 'Raft Foundation',
    'Pad Footing', 'Strip Footing', 'Basement Slab', 'Basement Wall',
    'Ground Floor Slab', 'Ground Beam',
}


def build_geometry_payload(geometry_data, detailed_df) -> list[dict]:
    """The 3D viewer's element list, with meshes where the model provided them.

    Each entry: {x,y,z, w,d,h, cat, name, level, total, a1_a3_t, a1_a3_kg,
    per_m3, per_m3_a1a3} plus {v, t} - flat vertex and triangle arrays - when
    real tessellated geometry exists. Without those the viewer draws a box from
    w/d/h, which is the same fallback the legacy dashboard uses.
    """
    from templates.html_template_2 import _prepare_geometry_json_mesh
    return _prepare_geometry_json_mesh(geometry_data or [], detailed_df)


def hotspots(rows: list[dict], limit: int = 5) -> list[dict]:
    """Biggest carbon contributors by element, for the "Top N hotspots" panel."""
    ordered = sorted(rows, key=lambda r: r.get('total_kg', 0), reverse=True)
    return [{
        'description': r['description'],
        'category': r['category'],
        'material': r['material'],
        'mass': r['mass'],
        'total_kg': r['total_kg'],
        'total_ton': round(r['total_kg'] / 1000, 4),
    } for r in ordered[:limit]]


def substructure_split(breakdown_by_category: list[dict]) -> dict:
    """Substructure vs superstructure totals, from the category breakdown."""
    sub = sum(b['total_ton'] for b in breakdown_by_category
              if b['key'] in SUBSTRUCTURE_CATEGORIES)
    total = sum(b['total_ton'] for b in breakdown_by_category)
    return {
        'substructure': round(sub, 4),
        'superstructure': round(total - sub, 4),
        'total': round(total, 4),
        'substructure_pct': round(sub / total * 100, 1) if total else 0.0,
    }


def member_efficiency(breakdown_by_category: list[dict], area: float) -> list[dict]:
    """Carbon per m2 of floor area, by element type.

    A proxy for how hard each member type is working: two projects with the same
    total can differ sharply in where that carbon sits.
    """
    if not area or area <= 0:
        return []
    return [{
        'key': b['key'],
        'total_ton': b['total_ton'],
        'per_sqm': round(b['total_kg'] / area, 2),
        'mass_kg': b['mass_kg'],
        # kgCO2e per kg of material - high values flag a carbon-dense choice
        # rather than simply a lot of material.
        'intensity': round(b['total_kg'] / b['mass_kg'], 4) if b['mass_kg'] else 0.0,
    } for b in breakdown_by_category]


# Shadow price of carbon, EUR per tonne. Matches the legacy dashboard, which
# computes `total_emission * 1000 * 0.070` (html_template_2.py:949) - i.e. EUR
# 0.070 per kg. A policy assumption, not a measurement.
SOCIAL_COST_PER_TON = 70.0

# Benchmark and target intensities, kgCO2e/m2. 200 is the IStructE SCORS B band;
# 220 is the industry-average comparator the legacy dashboard uses.
SCORS_B_TARGET = 200.0
INDUSTRY_BENCHMARK = 220.0


def social_cost(total_ton: float,
                price_per_ton: float = SOCIAL_COST_PER_TON) -> dict:
    """Monetised carbon at a shadow price.

    Returned WITH the price it used, rather than as a bare number: the figure is
    a policy assumption, and a reader cannot sanity-check it otherwise.
    """
    return {
        'price_per_ton': price_per_ton,
        'currency': 'EUR',
        'cost': round((total_ton or 0) * price_per_ton),
    }


def benchmark_comparison(per_sqm: float) -> dict:
    """How this project sits against the industry-average comparator."""
    per_sqm = per_sqm or 0
    diff = ((per_sqm - INDUSTRY_BENCHMARK) / INDUSTRY_BENCHMARK * 100
            if INDUSTRY_BENCHMARK else 0)
    return {
        'benchmark': INDUSTRY_BENCHMARK,
        'actual': round(per_sqm, 1),
        'diff_pct': round(diff, 1),
        'better': diff < 0,
    }


def scors_bands() -> list[dict]:
    """The SCORS A++ to G scale, for the rating strip.

    Read from compliance.py rather than restated here - the legacy dashboard
    hardcoded its bands in HTML, and that is exactly how the Data Check screen's
    factor tables drifted from the catalogue.
    """
    from compliance import RATING_COLORS, SCORS_BANDS
    out = []
    for rating, (low, high) in SCORS_BANDS.items():
        colours = RATING_COLORS.get(rating, {})
        out.append({
            'rating': rating,
            'low': low,
            'high': None if high == float('inf') else high,
            'color': colours.get('start', '#94a3b8'),
            'color_end': colours.get('end', '#cbd5e1'),
        })
    return out


def budget_vs_scors_b(per_sqm: float, area: float,
                      target: float = SCORS_B_TARGET) -> dict:
    """Headroom against the SCORS B band (<= 200 kgCO2e/m2).

    B is the IStructE target band, which is why the legacy KPI tile compares
    against it rather than against the project's own rating.
    """
    per_sqm = per_sqm or 0
    delta = target - per_sqm
    return {
        'target': target,
        'actual': round(per_sqm, 1),
        'headroom': round(delta, 1),
        'within': delta >= 0,
        'pct_of_target': round(per_sqm / target * 100, 1) if target else 0.0,
        # The tonnes that separate this project from the target, which is the
        # form a design team can actually act on.
        'delta_ton': round(delta * area / 1000, 1) if area else None,
        # Bar fill and colour, matching html_template_2.py:944-947 so the React
        # tile reads the same as the HTML one rather than reinventing the bands.
        'bar_pct': round(min(100, max(0, per_sqm / max(1, target) * 100)), 1),
        'color': ('#16a34a' if per_sqm <= 200
                  else '#e67e22' if per_sqm <= 350 else '#dc2626'),
    }


# ═══════════════════════════════════════════════════════════════════════
#  DERIVED INSIGHTS
# ═══════════════════════════════════════════════════════════════════════

def build_insights(result) -> dict:
    """The derived panels the raw breakdowns cannot give you.

    Zone intensity (office / core / basement), per-floor intensity, the
    slab-system figure, and the two member-level proxies - steel kg per m3 of
    concrete, and kgCO2e per m3 of concrete. Each one is a real analytical
    statement, not a re-slice of `result_rows`: the zone tagger reads element
    names for "core", "lift", "stair", "basement"; the slab figure needs the 3D
    footprint areas; and both member proxies divide by CONCRETE volume only,
    which no GROUP BY over the priced rows produces.

    Reuses templates.html_template_2._build_dashboard_insights rather than
    reimplementing it - the same decision, for the same reason, as
    build_geometry_payload above. It is a pure function of a DataFrame, and a
    second copy of a keyword-matching heuristic is a guaranteed future drift.

    Computed ONCE, at calculation time, and stored with the results: the slab
    figure needs the geometry blob, which can be tens of megabytes, and the
    dashboard is meant to be a pure read.
    """
    from templates.html_template_2 import (_build_dashboard_insights,
                                           _prepare_geometry_json_mesh)

    data = result.data
    detailed_df = getattr(data, 'detailed_df', None)
    metrics = getattr(data, 'metrics', {}) or {}
    stages = getattr(data, 'stage_emissions', {}) or {}
    project_info = getattr(data, 'project_info', {}) or {}

    total_emission = float(metrics.get('total_emission_ton', 0) or 0)
    geometry_json = _prepare_geometry_json_mesh(
        getattr(data, 'geometry_data', []) or [], detailed_df)

    return _build_dashboard_insights(
        detailed_df,
        getattr(data, 'detailed_data', []) or [],
        geometry_json,
        float(getattr(data, 'project_area', 0) or 0),
        total_emission,
        str(project_info.get('stage') or 'Concept / Schematic Design'),
        getattr(data, 'sensitivity_results', {}) or {},
        {'A1-A3': float(stages.get('A1-A3', 0) or 0),
         'A4': float(stages.get('A4', 0) or 0),
         'A5': float(stages.get('A5', 0) or 0)},
        metrics,
        str(project_info.get('structural_system') or ''),
        getattr(data, 'emission_factors_used', []) or [],
    )

def methodology() -> dict:
    """The constants every figure on the dashboard is built from.

    Served rather than restated in the view for the same reason the Data Check
    screen stopped hardcoding its factor tables: a constant copied into a
    template is a constant that drifts. These are read from calculations.py, so
    the methodology note and the arithmetic can never disagree.
    """
    from calculations import (A5A_EMISSION_FACTOR_KGCO2E_PER_SQM,
                              SEAI_CF_OUTWARD_ROAD, SEAI_CF_RETURN_ROAD,
                              SEAI_CF_SEA)

    return {
        'standard': 'EN 15978 (subset) — A1–A5 upfront carbon, structural scope',
        'a5a': {
            'factor': A5A_EMISSION_FACTOR_KGCO2E_PER_SQM,
            # The SEAI figure is for a WHOLE building; this tool assesses the
            # structure only, which is where the 70% comes from. Stated so the
            # 28 is traceable rather than arbitrary.
            'whole_building': round(
                A5A_EMISSION_FACTOR_KGCO2E_PER_SQM / 0.7),
            'structural_share_pct': 70,
            'distribution': 'by mass fraction across elements',
        },
        'transport': {
            'road_outward': SEAI_CF_OUTWARD_ROAD,
            'road_return': SEAI_CF_RETURN_ROAD,
            'sea': SEAI_CF_SEA,
            'note': ('Road is an outward leg at average laden plus a separate '
                     'empty-return leg; sea is a single leg with no return.'),
        },
        'densities': {'concrete': 2400, 'steel': 7850},
        'social_cost_per_ton': SOCIAL_COST_PER_TON,
    }

