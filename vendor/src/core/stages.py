"""
Design-stage contract for C.L.E.A.R.
====================================
Single source of truth for how the assessment behaves at each RIBA-style design
stage. Used by the web app and the report templates so a Concept run and a
Tender run are labelled, validated and uncertainty-banded differently.

Zero-blackboxing: the uncertainty percentages below are DISPLAY bands applied to
the headline results (they never change the calculated numbers) and are stated
on the report next to the values they qualify. They follow the accuracy ranges
commonly quoted for structural embodied-carbon assessments (IStructE "How to
calculate embodied carbon" / RICS PS guidance): benchmark-based concept
estimates ±25-30%, model-based design assessments ±10-15%, measured tender
quantities with supplier data ±5%.
"""

STAGE_CONTRACT = {
    'Concept / Schematic Design': {
        'code': 'S1',
        'short': 'Concept',
        'badge': 'Concept estimate',
        'uncertainty_pct': 27.5,
        'epd_expected': False,
        'description': ('Early-stage estimate from schematic quantities and generic '
                        'industry factors. Use for option comparison, not compliance.'),
        'expectations': [
            'Quantities: model or benchmark rates — approximate',
            'Factors: generic catalogue values are appropriate',
            'Rebar: rate-based estimates (kg/m³) are appropriate',
        ],
    },
    'Detailed Design': {
        'code': 'S2',
        'short': 'Detailed',
        'badge': 'Design assessment',
        'uncertainty_pct': 12.5,
        'epd_expected': False,
        'description': ('Design-stage assessment from modelled quantities. Generic '
                        'factors acceptable; supplier EPDs improve confidence.'),
        'expectations': [
            'Quantities: modelled volumes from IFC/Revit',
            'Factors: generic acceptable, EPDs where suppliers are known',
            'Rebar: rate-based or modelled reinforcement',
        ],
    },
    'Tender': {
        'code': 'S3',
        'short': 'Tender',
        'badge': 'Tender assessment',
        'uncertainty_pct': 5.0,
        'epd_expected': True,
        'description': ('Tender/final assessment. Measured quantities and '
                        'supplier-specific EPDs are expected at this stage.'),
        'expectations': [
            'Quantities: measured BOQ masses (rebar from BBS, not kg/m³ rates)',
            'Factors: supplier EPDs expected — generic factors reduce data quality',
            'Transport & waste: supplier-confirmed values expected',
        ],
    },
}

_DEFAULT_STAGE = 'Detailed Design'


def get_stage_info(stage_label: str) -> dict:
    """Resolve a stage label (as sent by the UI) to its contract entry.

    Tolerant of partial labels ('Tender', 'Concept', ...). Unknown labels fall
    back to Detailed Design so an unrecognised stage never breaks a run.
    Returns a copy including the resolved 'label'.
    """
    label = str(stage_label or '').strip()
    entry = STAGE_CONTRACT.get(label)
    if entry is None:
        low = label.lower()
        for key, val in STAGE_CONTRACT.items():
            if (low and (low in key.lower()
                         or val['short'].lower() in low
                         or val['code'].lower() == low)):
                label, entry = key, val
                break
    if entry is None:
        label, entry = _DEFAULT_STAGE, STAGE_CONTRACT[_DEFAULT_STAGE]
    out = dict(entry)
    out['label'] = label
    return out


def uncertainty_range(value: float, uncertainty_pct: float) -> tuple:
    """Return (low, high) display band for a value at the given ±%."""
    try:
        v = float(value)
        u = float(uncertainty_pct) / 100.0
    except (TypeError, ValueError):
        return (0.0, 0.0)
    return (v * (1.0 - u), v * (1.0 + u))
