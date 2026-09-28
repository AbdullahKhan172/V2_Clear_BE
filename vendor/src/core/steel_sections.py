"""
Steel Section Classification
=============================
Detects whether a name string describes a structural steel section
(UB, UC, CHS, RHS, etc.) as opposed to rebar/tendon or a non-steel element.
Used by the IFC processor, csv_processor and boq_parser to classify
elements when the raw category/material text doesn't say so explicitly.

Structural steel sections (UB, UC, CHS, RHS, etc.) have their mass
calculated as:  mass_kg = length_m * kg_per_m

This is distinct from reinforcing bars, which are expressed as kg/m³
of concrete volume.
"""

import re

# Section type keywords that indicate a structural steel member in a name string
SECTION_TYPE_KEYWORDS = [
    'ub ', 'uc ', 'ubp ', 'pfc ', 'chs ', 'shs ', 'rhs ', 'ehs ',
    'hfshs', 'cfshs', 'hfrhs', 'cfrhs', 'hfchs', 'cfchs', 'hfehs',
    ' ub', ' uc', '/ub', '/uc',
    'universal beam', 'universal column', 'universal bearing pile',
    'parallel flange channel', 'circular hollow', 'square hollow',
    'rectangular hollow', 'elliptical hollow', 'hollow section',
    'structural tee', 'equal leg angle', 'unequal leg angle',
    'back-to-back',
    # Slimflor / shallow-floor steel beam family (steel sections, not concrete):
    # ASB = Asymmetric Slimflor Beam (e.g. "300ASB249"), SFB = Slimflor Beam,
    # IFB = Integrated Floor Beam, plus the Slimdek system.
    'asb', 'sfb', 'ifb', 'slimflor', 'slim flor', 'slimdek', 'slim floor',
    'asymmetric slimflor', 'integrated floor beam',
    # Light-gauge steel framing systems (purlins / multibeams)
    'multibeam', 'light gauge', 'metsec',
]

# Section designations written WITHOUT a trailing space ("UC203x203x46",
# "IPE400", "HEB-300", "W14x90") never hit the substring keywords above, so
# real steel members fell through and were priced as concrete. Word boundary +
# required size digits keep short codes (UC, UB, EA…) from matching inside
# ordinary words ("pipe", "ripe", "product"). Covers UK (UB/UC/PFC/RSJ + UK*
# designations), European (IPE/HE/UPN) and US (W/HP shapes) families.
_SECTION_CODE_RE = re.compile(
    r'\b(?:ub|uc|ubp|ukb|ukc|ukbp|pfc|ukpfc|rsj|chs|shs|rhs|ehs|'
    r'ipe|hea|heb|hem|upe|upn)\s*[-–]?\s*\d'
    r'|\b(?:w|hp)\d{1,2}\s*[x×]\s*\d{1,3}\b'
    r'|\b(?:ea|ua|uea|rsa)\s*[-–]?\s*\d{2,3}\s*[x×]',
    re.IGNORECASE)


def is_structural_steel_name(name_str: str) -> bool:
    """
    Return True if the name string looks like a structural steel section
    (not rebar/tendon).  Used by csv_processor for Revit raw exports.
    """
    if not name_str:
        return False
    t = name_str.lower()
    if any(kw in t for kw in SECTION_TYPE_KEYWORDS):
        return True
    return bool(_SECTION_CODE_RE.search(t))
