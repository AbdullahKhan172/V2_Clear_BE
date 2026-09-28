"""
Decarbonisation reference data and stage logic.
===============================================
Two things live here:

  1. The Ireland office case-study benchmarks - the same 5,062 m2 building
     modelled with different structural systems, holding transport, A5a and
     waste identical so the only variable is the structure itself. Carried over
     from the legacy dashboard, which hardcoded them in HTML.

  2. Which decarbonisation levers are still OPEN at each design stage. This is
     the substance of the tab: proposing a structural-system swap at Tender is
     not advice, it is noise, and the legacy dashboard was careful about it.

Scenario savings themselves are NOT computed here - they come from the engine's
own run_sensitivity_analysis, which re-runs the real BOQ with each modification.
This module only labels and orders them.
"""

from __future__ import annotations

# Like-for-like: same GIA, same transport, same A5a, same waste. Only the
# structure differs. kgCO2e/m2 is A1-A5; cost is indicative EUR/m2.
CASE_STUDY_GIA = 5062

SYSTEM_BENCHMARKS = [
    {'system': 'RC Flat Slab', 'per_sqm': 278, 'cost': 181},
    {'system': 'Steel + Hollowcore', 'per_sqm': 258, 'cost': 300},
    {'system': 'In-Situ + Hollowcore', 'per_sqm': 236, 'cost': 195},
    {'system': 'PT Band Beam', 'per_sqm': 233, 'cost': 161},
    {'system': 'PT Slab with Caps', 'per_sqm': 227, 'cost': 168},
    {'system': 'PT Flat Slab', 'per_sqm': 223, 'cost': 170.5},
]

# Scenarios that cannot be applied together. Only the best of each group counts
# toward the combined opportunity: the same concrete cannot be both 50% and 70%
# GGBS, and summing the two would overstate the saving by roughly double.
EXCLUSIVE_GROUPS = [{'ggbs_50pct', 'ggbs_70pct'}]

# Engine scenario key -> how it is presented. Order is the order shown.
SCENARIOS = [
    # A pseudo-scenario: the engine cannot compute a structural-system swap, but
    # it is the single largest lever at Concept and vanishes afterwards. Listing
    # it keeps the stage difference honest instead of showing Concept and
    # Detailed Design as identical.
    ('__system__', 'Structural system choice',
     'The largest single lever, and only available while the frame is still a '
     'choice. See the system comparison below.', 'system'),
    ('slab_thickness_30pct', 'Slab thickness reduction (30%)',
     'In-situ RC flat slabs only — hollowcore and PT are precast or '
     'prestressed, so thickness is not a design variable.', 'geometry'),
    ('ggbs_50pct', 'Concrete mix: 50% GGBS',
     'Applied per grade using the real IGBC catalogue rates for each '
     '50% GGBS variant.', 'specification'),
    ('ggbs_70pct', 'Concrete mix: 70% GGBS (CEM III equivalent)',
     'Applied to the highest GGBS level each grade allows under IS EN 206. '
     'Confirm supplier availability and early-strength requirements.',
     'specification'),
    ('rebar_slab_15pct', 'Rebar reduction — slabs (15%)',
     'Layout optimisation, reduced grid, yield-line analysis.', 'efficiency'),
    ('rebar_beam_15pct', 'Rebar reduction — beams (15%)',
     'Right-sizing, composite action, eliminating over-design.', 'efficiency'),
    ('rebar_col_10pct', 'Rebar reduction — columns (10%)',
     'Higher-strength concrete, smaller sections, shorter laps.', 'efficiency'),
    ('rebar_found_10pct', 'Rebar reduction — foundations (10%)',
     'Optimised pile and cap layout, yield-line design for pad footings.',
     'efficiency'),
    ('rebar_wall_10pct', 'Rebar reduction — walls (10%)',
     'Rationalised shear wall layout, minimum reinforcement in low-demand '
     'zones.', 'efficiency'),
]

# Which lever families are still open at each stage, and why the others are not.
STAGE_LEVERS = {
    'Concept / Schematic Design': {
        'headline': 'This is where 70–90% of embodied carbon is locked in.',
        'lead': 'Every lever below is still open — the structural system itself '
                'is still a choice. Acting now has the most effect it ever will.',
        'open': {'system', 'geometry', 'specification', 'efficiency'},
        'locked': {
            'procurement': 'Supplier EPDs are not expected this early — there '
                           'are no confirmed suppliers to get them from.',
        },
    },
    'Detailed Design': {
        'headline': 'Specification and efficiency are still open.',
        'lead': 'The structural system is fixed, but concrete mix and '
                'reinforcement efficiency can still move the result.',
        'open': {'geometry', 'specification', 'efficiency'},
        'locked': {
            'system': 'The structural system was settled at Concept — changing '
                      'it now is a redesign, not a saving.',
            'procurement': 'EPDs become available once suppliers are confirmed '
                           'at Tender.',
        },
    },
    'Tender': {
        'headline': 'Procurement is the remaining lever.',
        'lead': 'Design is fixed. What is left is replacing generic factors '
                'with confirmed supplier EPDs, and confirming transport and '
                'waste with the contractor.',
        'open': {'procurement'},
        'locked': {
            'system': 'Fixed at Concept.',
            'geometry': 'Fixed at Detailed Design.',
            'specification': 'The mix is specified; changing it now needs a '
                             'variation, not a design decision.',
            'efficiency': 'Reinforcement is detailed — savings here needed to '
                          'happen at Detailed Design.',
        },
    },
}

# Typical EPD saving against generic factors, used for the Tender-stage estimate.
# A planning figure, not a measurement - stated as such wherever it is shown.
EPD_TYPICAL_SAVING = 0.12


def build_decarbonisation(*, stage_label: str, sensitivity: dict,
                          total_ton: float, per_sqm: float, area: float,
                          structural_system: str,
                          category_breakdown: list[dict]) -> dict:
    """Assemble the Decarbonisation tab's content for one run."""
    cfg = STAGE_LEVERS.get(stage_label) or STAGE_LEVERS['Detailed Design']

    scenarios = []
    for key, label, note, family in SCENARIOS:
        res = (sensitivity or {}).get(key)
        open_now = family in cfg['open']
        is_pseudo = key.startswith('__')
        delta = float((res or {}).get('delta', 0) or 0)
        scenarios.append({
            'key': key, 'label': label, 'note': note, 'family': family,
            'available': res is not None or is_pseudo,
            'is_estimate': is_pseudo,
            'open': open_now,
            'locked_reason': cfg['locked'].get(family) if not open_now else None,
            'delta_ton': round(delta, 2),
            'delta_per_sqm': round(delta * 1000 / area, 1) if area > 0 else 0,
            'delta_pct': round(delta / total_ton * 100, 1) if total_ton > 0 else 0,
            'n_rows': int((res or {}).get('n_rows', 0) or 0),
            'engine_note': (res or {}).get('note', ''),
        })

    # Top actions: only levers that are open AND actually computed a saving.
    # Ranking locked scenarios would be advice the team cannot act on.
    candidates = sorted(
        [s for s in scenarios if s['open'] and s['available'] and s['delta_ton'] > 0],
        key=lambda s: s['delta_ton'], reverse=True)

    # Drop the weaker member of any mutually exclusive pair before ranking, so
    # the combined figure is a saving that could actually all be taken.
    dropped: set[str] = set()
    for group in EXCLUSIVE_GROUPS:
        present = [c for c in candidates if c['key'] in group]
        for c in present[1:]:
            dropped.add(c['key'])
    actions = [c for c in candidates if c['key'] not in dropped][:4]

    combined = round(sum(a['delta_ton'] for a in actions), 2)

    # Procurement is a stage lever with no engine scenario behind it, so its
    # figure is an estimate and is labelled as one.
    epd = None
    if 'procurement' in cfg['open']:
        saving = total_ton * EPD_TYPICAL_SAVING
        epd = {
            'delta_ton': round(saving, 2),
            'delta_per_sqm': round(saving * 1000 / area, 1) if area > 0 else 0,
            'basis': f'Indicative: supplier EPDs typically sit around '
                     f'{int(EPD_TYPICAL_SAVING * 100)}% below generic catalogue '
                     f'factors. Replace with real EPDs in the Materials step.',
        }

    return {
        'stage': stage_label,
        'headline': cfg['headline'],
        'lead': cfg['lead'],
        'scenarios': scenarios,
        'actions': actions,
        'combined_ton': combined,
        'combined_per_sqm': round(combined * 1000 / area, 1) if area > 0 else 0,
        'combined_pct': round(combined / total_ton * 100, 1) if total_ton else 0,
        'epd_opportunity': epd,
        'benchmarks': _benchmarks(per_sqm, structural_system),
        'hotspots': _hotspots(category_breakdown, total_ton),
        'case_study_gia': CASE_STUDY_GIA,
    }


def _benchmarks(per_sqm: float, structural_system: str) -> dict:
    """Case-study systems with this run inserted, for the comparison table."""
    rows = [{**b, 'is_current': False} for b in SYSTEM_BENCHMARKS]
    rows.append({
        # None, not a placeholder: the structural system is an optional field on
        # upload, and the row already carries a "This run" marker. Filling in
        # "This run" here made the cell read "This run This run".
        'system': structural_system or None,
        'per_sqm': round(per_sqm, 1),
        'cost': None,           # unknown for a real project
        'is_current': True,
    })
    rows.sort(key=lambda r: r['per_sqm'])
    best = min((r['per_sqm'] for r in rows), default=0)
    for r in rows:
        r['vs_best_pct'] = (round((r['per_sqm'] / best - 1) * 100, 1)
                            if best > 0 else 0)
    return {'gia': CASE_STUDY_GIA, 'rows': rows,
            'note': 'Like-for-like: the same building modelled with different '
                    'structural systems, holding transport, site activity and '
                    'waste identical so the structure is the only variable.'}


# Industry reduction targets by element type, for the hotspot panel.
HOTSPOT_TARGETS = {
    'Slab/Floor': 'Thickness, PT, voided systems and GGBS all act here first.',
    'Hollowcore Slab': 'Precast: the factor already prices its reinforcement — '
                       'target the concrete mix and transport instead.',
    'Column': 'Higher-strength concrete allows smaller sections and less rebar.',
    'Beam': 'Right-sizing and composite action; check for over-design.',
    'Wall': 'Rationalise the shear wall layout; minimum reinforcement in '
            'low-demand zones.',
    'Foundation/Footing': 'Optimise the pile and cap layout — often the least '
                          'interrogated part of the frame.',
    'Pile': 'Re-check the ground model; pile length is frequently conservative.',
}


def _hotspots(category_breakdown: list[dict], total_ton: float) -> list[dict]:
    """The three element types carrying the most carbon, with where to look."""
    return [{
        'category': c['key'],
        # Rounded here, like every other figure this module returns, so the tab
        # does not show 621.219 t beside a headline of 1,314.07.
        'total_ton': round(c['total_ton'], 2),
        'pct': round(c['total_ton'] / total_ton * 100, 1) if total_ton else 0,
        'target': HOTSPOT_TARGETS.get(
            c['key'], 'Review quantities and specification for this element type.'),
    } for c in category_breakdown[:3]]


# What the dashboard should FOREGROUND at each stage, and what it should stop
# showing. Carried over from the legacy dashboard (html_template_2.py:952-986),
# which hardcoded it in the HTML builder. It lives here for the same reason the
# factor tables moved out of web_ui.html: a table restated in the view is a
# table that drifts.
STAGE_FOCUS = {
    'Concept / Schematic Design': {
        'show': [
            'Structural system comparison while options are still open.',
            'Slab thickness sensitivity and 25/50/70% GGBS scenarios.',
            'Honest uncertainty band of ±25% for early-stage decisions.',
        ],
        'hide': [
            'EPD substitution and procurement checklist.',
            'Transport refinement and detailed connection assumptions.',
        ],
    },
    'Detailed Design': {
        'show': [
            'Per-member rebar efficiency versus typical ranges.',
            'Concrete grade and GGBS specification confirmation.',
            'Member-level hotspot targeting with narrowed uncertainty.',
        ],
        'hide': [
            'Structural system swap scenarios (locked after Concept).',
            'Tender procurement controls and checklists.',
        ],
    },
    'Tender': {
        'show': [
            'EPD coverage, procurement readiness, and transport confirmation.',
            'A5a refinement and contractor-confirmed waste assumptions.',
            'Narrow uncertainty band around ±5–10% for final reporting.',
        ],
        'hide': [
            'GGBS and structural strategy sliders (already locked).',
            'Generic early-stage optimisation advice.',
        ],
    },
}


def stage_focus(stage_label: str) -> dict:
    """What to foreground and what to retire at this stage."""
    cfg = STAGE_FOCUS.get(stage_label) or {}
    return {'stage': stage_label,
            'show': list(cfg.get('show') or []),
            'hide': list(cfg.get('hide') or [])}
