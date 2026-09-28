"""
Steel Reinforcement Rates Reference Data
=========================================
Based on real project data from Irish structural engineering firms:
MMOS, Waterman Moylan, OCSC, Barret Mahony, Lohan Donnelly
"""

# Rebar rates in kg/m³ of concrete, organized by element category
STEEL_RATES = {
    # ── Slabs ──────────────────────────────────────────────────────────────
    'Slab/Floor':        {'min': 100, 'max': 200, 'default': 150},
    'Ground Floor Slab': {'min': 125, 'max': 175, 'default': 150},
    'Basement Slab':     {'min': 125, 'max': 175, 'default': 150},
    'Podium Slab':       {'min': 125, 'max': 150, 'default': 130},
    'Transfer Slab':     {'min': 150, 'max': 225, 'default': 175},
    'Roof Slab':         {'min': 100, 'max': 175, 'default': 140},
    'Hollowcore Slab':   {'min':  80, 'max': 120, 'default':  90},
    'Waffle Slab':       {'min': 100, 'max': 175, 'default': 140},
    'Ribbed Slab':       {'min':  90, 'max': 160, 'default': 125},
    'Precast Slab':      {'min':  80, 'max': 130, 'default': 100},
    'Composite Deck':    {'min':  60, 'max': 120, 'default':  90},
    # ── Beams ──────────────────────────────────────────────────────────────
    'Beam':              {'min': 150, 'max': 350, 'default': 225},
    'Ground Beam':       {'min': 225, 'max': 300, 'default': 250},
    'Transfer Beam':     {'min': 300, 'max': 400, 'default': 350},
    'Lintel':            {'min': 100, 'max': 200, 'default': 150},
    # ── Columns ────────────────────────────────────────────────────────────
    'Column':            {'min': 200, 'max': 450, 'default': 325},
    # ── Walls ──────────────────────────────────────────────────────────────
    'Wall':              {'min':  80, 'max': 200, 'default': 125},
    'Shear Wall':        {'min':  80, 'max': 175, 'default': 130},
    'Core Wall':         {'min': 150, 'max': 200, 'default': 160},
    'Retaining Wall':    {'min': 125, 'max': 200, 'default': 150},
    'Basement Wall':     {'min': 115, 'max': 200, 'default': 150},
    # ── Foundations ────────────────────────────────────────────────────────
    'Foundation/Footing':{'min': 120, 'max': 225, 'default': 175},
    'Pad Footing':       {'min': 120, 'max': 175, 'default': 150},
    'Strip Footing':     {'min': 120, 'max': 150, 'default': 130},
    'Pile Cap':          {'min': 120, 'max': 175, 'default': 140},
    'Raft Foundation':   {'min': 175, 'max': 225, 'default': 200},
    # ── Piles ──────────────────────────────────────────────────────────────
    'Pile':              {'min': 100, 'max': 175, 'default': 130},
    # ── Other ──────────────────────────────────────────────────────────────
    'Stair':             {'min': 120, 'max': 150, 'default': 135},
    'Ramp':              {'min': 100, 'max': 175, 'default': 140},
    'Roof':              {'min': 100, 'max': 175, 'default': 140},
}

# Aliases — maps any incoming category string to a STEEL_RATES key
CATEGORY_ALIASES = {
    'slab': 'Slab/Floor', 'floor': 'Slab/Floor', 'slab/floor': 'Slab/Floor',
    'plate': 'Slab/Floor', 'flat slab': 'Slab/Floor',
    'ground floor slab': 'Ground Floor Slab', 'ground slab': 'Ground Floor Slab',
    'slab on grade': 'Ground Floor Slab', 'sog': 'Ground Floor Slab',
    'basement slab': 'Basement Slab', 'basement floor': 'Basement Slab',
    'podium slab': 'Podium Slab', 'podium deck': 'Podium Slab',
    'transfer slab': 'Transfer Slab', 'transfer plate': 'Transfer Slab',
    'roof slab': 'Roof Slab', 'roof deck': 'Roof Slab',
    'hollowcore slab': 'Hollowcore Slab', 'hollowcore': 'Hollowcore Slab',
    'hollow core': 'Hollowcore Slab', 'hc slab': 'Hollowcore Slab',
    'waffle slab': 'Waffle Slab', 'waffle': 'Waffle Slab', 'coffered slab': 'Waffle Slab',
    'ribbed slab': 'Ribbed Slab', 'rib slab': 'Ribbed Slab',
    'precast slab': 'Precast Slab', 'double tee': 'Precast Slab',
    'composite deck': 'Composite Deck', 'metal deck': 'Composite Deck', 'decking': 'Composite Deck',
    'beam': 'Beam', 'structural member': 'Beam',
    'ground beam': 'Ground Beam', 'grade beam': 'Ground Beam',
    'transfer beam': 'Transfer Beam', 'transfer girder': 'Transfer Beam',
    'lintel': 'Lintel',
    'column': 'Column',
    'wall': 'Wall', 'general wall': 'Wall',
    'shear wall': 'Shear Wall',
    'core wall': 'Core Wall', 'lift core': 'Core Wall',
    'retaining wall': 'Retaining Wall',
    'basement wall': 'Basement Wall',
    'foundation': 'Foundation/Footing', 'footing': 'Foundation/Footing',
    'foundation/footing': 'Foundation/Footing', 'general foundation': 'Foundation/Footing',
    'pad footing': 'Pad Footing', 'pad foundation': 'Pad Footing', 'isolated footing': 'Pad Footing',
    'strip footing': 'Strip Footing', 'strip foundation': 'Strip Footing',
    'pile cap': 'Pile Cap', 'pilecap': 'Pile Cap',
    'raft foundation': 'Raft Foundation', 'raft slab': 'Raft Foundation', 'mat foundation': 'Raft Foundation',
    'pile': 'Pile', 'bored pile': 'Pile', 'cfa pile': 'Pile', 'driven pile': 'Pile',
    'stair': 'Stair', 'staircase': 'Stair', 'stair flight': 'Stair',
    'ramp': 'Ramp', 'car ramp': 'Ramp',
    'roof': 'Roof',
}


def get_steel_rate(category, element_name=""):
    """
    Get suggested rebar rate for a structural element (kg/m³ of concrete).
    Tries category directly, then aliases, then partial match, then default.
    """
    cat_lower = category.lower().strip()

    # Direct key match first
    if category in STEEL_RATES:
        r = STEEL_RATES[category]
        return {'min': r['min'], 'max': r['max'], 'default': r['default']}

    # Alias lookup
    cat_key = CATEGORY_ALIASES.get(cat_lower)
    if cat_key and cat_key in STEEL_RATES:
        r = STEEL_RATES[cat_key]
        return {'min': r['min'], 'max': r['max'], 'default': r['default']}

    # Partial alias match
    for alias, mapped in CATEGORY_ALIASES.items():
        if alias in cat_lower:
            r = STEEL_RATES.get(mapped, {})
            if r:
                return {'min': r['min'], 'max': r['max'], 'default': r['default']}

    return {'min': 100, 'max': 200, 'default': 150}


def format_rate_hint(category, element_name=""):
    """Get a formatted hint string for the steel rate."""
    rate = get_steel_rate(category, element_name)
    return f"{rate['min']}-{rate['max']} kg/m³ (typical: {rate['default']})"
