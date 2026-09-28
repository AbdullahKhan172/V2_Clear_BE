"""
Emission Catalogue Management & Auto-Mapping
=============================================
Loads the A1-A5 emission catalogue and provides intelligent
auto-mapping from concrete grades and GGBS percentages to e_ids.
"""

import os
from pathlib import Path

import pandas as pd

# Version of the factor database — bump on every manual CSV edit and record the
# change in 'docs/Additional Documents (reference)/CATALOGUE_NOTES.md'. Stamped
# into reports so every result is traceable to the exact factor set used.
CATALOGUE_VERSION = '2026.07-r4'

# Path to the catalogue CSV. This file lives at src/core/catalogue.py, so
# parents[2] is the project root (core -> src -> root); the live CSV is at
# <root>/data/A1_A5_Emission_Catalogue.csv.
CATALOGUE_PATH = str(
    Path(__file__).resolve().parents[2] / 'data' / 'A1_A5_Emission_Catalogue.csv'
)

# ── Concrete grade → e_id mapping ──────────────────────────────────────────
# Maps (grade_label, ggbs_percent) → e_id
CONCRETE_GRADE_MAP = {
    # GEN 0 (6/8 MPa)
    ('6/8',   0):  'C_001', ('6/8',  25): 'C_002', ('6/8',  50): 'C_003', ('6/8',  70): 'C_004',
    # GEN 1 (8/10 MPa)
    ('8/10',  0):  'C_005', ('8/10', 25): 'C_006', ('8/10', 50): 'C_007', ('8/10', 70): 'C_008',
    # GEN 2 (12/15 MPa)
    ('12/15', 0):  'C_009', ('12/15',25): 'C_010', ('12/15',50): 'C_011', ('12/15',70): 'C_012',
    # GEN 3 (16/20 MPa)
    ('16/20', 0):  'C_013', ('16/20',25): 'C_014', ('16/20',50): 'C_015', ('16/20',70): 'C_016',
    # Structural grades (0% GGBS only for base, with GGBS variants below)
    ('20/25', 0):  'C_017', ('20/25',25): 'C_032', ('20/25',50): 'C_039', ('20/25',70): 'C_045',
    ('25/30', 0):  'C_018', ('25/30',25): 'C_033', ('25/30',50): 'C_040', ('25/30',70): 'C_046',
    ('28/35', 0):  'C_019', ('28/35',25): 'C_034', ('28/35',50): 'C_041', ('28/35',70): 'C_047',
    ('30/37', 0):  'C_023',
    ('32/40', 0):  'C_020', ('32/40',25): 'C_036', ('32/40',50): 'C_042', ('32/40',70): 'C_048',
    ('35/45', 0):  'C_021', ('35/45',25): 'C_037', ('35/45',50): 'C_043', ('35/45',70): 'C_049',
    ('40/50', 0):  'C_022', ('40/50',25): 'C_038', ('40/50',50): 'C_044', ('40/50',70): 'C_050',
}

# Available concrete grades (for dropdown)
CONCRETE_GRADES = [
    '6/8 MPa (GEN 0)',
    '8/10 MPa (GEN 1)',
    '12/15 MPa (GEN 2)',
    '16/20 MPa (GEN 3)',
    '20/25 MPa',
    '25/30 MPa',
    '28/35 MPa',
    '30/37 MPa',
    '32/40 MPa',
    '35/45 MPa',
    '40/50 MPa',
]

# Grade label extraction from dropdown text
GRADE_LABEL_MAP = {
    '6/8 MPa (GEN 0)': '6/8',
    '8/10 MPa (GEN 1)': '8/10',
    '12/15 MPa (GEN 2)': '12/15',
    '16/20 MPa (GEN 3)': '16/20',
    '20/25 MPa': '20/25',
    '25/30 MPa': '25/30',
    '28/35 MPa': '28/35',
    '30/37 MPa': '30/37',
    '32/40 MPa': '32/40',
    '35/45 MPa': '35/45',
    '40/50 MPa': '40/50',
}

# Available GGBS percentages
GGBS_OPTIONS = [0, 25, 50, 70]

# ── Maximum GGBS substitution by concrete grade (IS EN 206 + Irish NDA) ──────
# Based on IGBC catalogue availability and Irish structural engineering practice.
# C40/50: 70% GGBS causes slow strength development and workability issues in
#         most exposure classes — 50% is the practical limit (IS EN 206 NA).
# C30/37: No GGBS variants exist in the IGBC catalogue.
# All other structural grades: 70% achievable with early supplier engagement.
GGBS_MAX_BY_GRADE = {
    '6/8':   70,
    '8/10':  70,
    '12/15': 70,
    '16/20': 70,
    '20/25': 70,
    '25/30': 70,
    '28/35': 70,
    '30/37': 0,   # no GGBS variants in IGBC catalogue
    '32/40': 70,
    '35/45': 70,
    '40/50': 50,  # IS EN 206 / NDA: 50% max in most exposure classes
}

def get_ggbs_max(grade_label: str) -> int:
    """Return the maximum allowable GGBS % for a concrete grade label (e.g. '32/40')."""
    return GGBS_MAX_BY_GRADE.get(grade_label, 70)

# ── Steel type mapping ─────────────────────────────────────────────────────
STEEL_TYPES = {
    'Ireland Average Reinforcing Steel': 'R_005',
    'Reinforcement Steel (ICE Database)': 'R_022',
    'World Average Reinforcement': 'R_024',
    'Steel Section (Ireland)': 'R_004',
    'Engineering Steel': 'R_020',
    'Hot Rolled Steel Coil': 'R_001',
    'Steel Plate': 'R_014',
}

# Structural steel section e_ids (UB, UC, CHS, etc. — mass = length × kg/m)
STEEL_SECTION_EIDS = ['R_001', 'R_004', 'R_014', 'R_020']
STEEL_SECTION_TYPES = {
    'Steel Section (Ireland)': 'R_004',
    'Hot Rolled Steel Coil': 'R_001',
    'Steel Plate': 'R_014',
    'Engineering Steel': 'R_020',
    # UI dropdown aliases
    'Steel Section (UK)': 'R_004',
    'Steel Section - EPD': 'R_004',
}

# Rebar / reinforcing bar e_ids (embedded in concrete — mass = volume × kg/m³)
# Structural timber / mass-timber e_ids (IGBC National Inventory 2023). These
# carry biogenic carbon in the `sequestration_kgco2e_kg` column (negative). They
# are quantified by volume × their own density (NOT concrete's 2400) — see
# CATALOGUE_NOTES.md and the timber-wiring note before enabling on the IFC path.
TIMBER_EIDS = ['T_001', 'T_002', 'T_003', 'T_004', 'T_005', 'T_006', 'T_007',
               'T_008', 'T_009', 'T_010', 'T_011', 'T_012', 'T_013']

REBAR_EIDS = ['R_005', 'R_022', 'R_024']
REBAR_TYPES = {
    'Ireland Average Reinforcing Steel': 'R_005',
    'Reinforcement Steel (ICE Database)': 'R_022',
    'World Average Reinforcement': 'R_024',
    # NOTE: the former aliases 'Reinforcement Steel - EPD (low carbon)' and
    # 'Stainless Steel' were removed 2026-07: both silently mapped to R_022
    # (1.72 kgCO2e/kg) — "low carbon" actually 2.3× the Ireland default, and
    # far below real stainless (~6 kgCO2e/kg). Unknown labels fall back to
    # DEFAULT_STEEL_EID. Re-add only with genuine catalogue entries.
}

# Default steel e_ids
DEFAULT_STEEL_EID = 'R_005'  # Ireland average rebar (IStructE default reinforcement factor)
DEFAULT_STEEL_SECTION_EID = 'R_004'  # Steel Section (Ireland) — section default
IRELAND_STEEL_EID = 'R_005'  # Ireland average rebar

# ── Post-Tensioning types ──────────────────────────────────────────────────
# Post-tensioning stays its own material bucket (category, waste %, transport,
# reporting) — it is NOT merged into rebar. What changed is the DEFAULT factor:
# the IGBC generic database has no PT/strand entry at all; every PT_xxx row in
# this catalogue besides PT_032 is a real supplier EPD (Celsa, Fapricela, Galcore,
# GreenStrand), not a generic value. Per IStructE guidance — "where no database
# factor or EPD exists for a material, use the reinforcement (rebar) factor as
# the default" — PT_032 (the entry DEFAULT_PT_EID resolves to) is set to the
# Ireland rebar factor (R_005, 0.737 kgCO2e/kg), not a strand-specific number.
# Selecting a named supplier EPD below still uses that supplier's real factor.
PT_TYPES = {
    'PT Default (Ireland rebar factor — IStructE)': 'PT_032',
    'Celsa 3-wire strand PC4': 'PT_010',
    'Celsa 7-wire strand P61': 'PT_011',
    'Celsa 7-wire strand P62': 'PT_012',
    'Fapricela PC strand 7-wire': 'PT_004',
    'Galcore Strands 15.2mm': 'PT_007',
    'GreenStrand (HJULSBRO)': 'PT_018',
}

DEFAULT_PT_EID = 'PT_032'  # no IGBC generic PT factor exists — uses Ireland rebar value
DEFAULT_CONCRETE_EID = 'C_020'  # C32/40 0% GGBS — canonical fallback

# ── Waste factors ──────────────────────────────────────────────────────────
DEFAULT_WASTE_FACTORS = {
    'Concrete': 1.05,          # 5% waste (SEAI Table 4)
    'Rebar': 1.05,             # 5% waste (SEAI Table 4)
    'Steel Section': 1.01,     # 1% waste (SEAI Table 4)
    'Steel': 1.05,             # legacy key — treated as rebar
    'Post Tensioning': 1.015,  # 1.5% waste
}

# ── Transport defaults ──────────────────────────────────────────────────────
# Per-material SEAI Table 2 default transport scenarios (km): (road, sea).
# This is the authoritative default table the UI renders; the calculation engine
# holds the same numbers in calculations.SEAI_TRANSPORT_DEFAULTS (kept in sync).
SEAI_TRANSPORT_TABLE = {
    'in_situ': {'label': 'In-situ / ready-mix concrete', 'road': 20,   'sea': 0,
                'scenario': 'Locally manufactured'},
    'precast': {'label': 'Precast concrete',             'road': 120,  'sea': 0,
                'scenario': 'Nationally manufactured (short range)'},
    'block':   {'label': 'Blockwork / masonry',          'road': 80,   'sea': 0,
                'scenario': 'Regionally manufactured'},
    'rebar':   {'label': 'Reinforcement (rebar)',        'road': 100,  'sea': 1000,
                'scenario': 'Imported'},
    'section': {'label': 'Structural steel',             'road': 100,  'sea': 1000,
                'scenario': 'Imported'},
    'sheet':   {'label': 'Sheet / coil steel',           'road': 100,  'sea': 1000,
                'scenario': 'Imported'},
    'strand':  {'label': 'Post-tensioning',              'road': 100,  'sea': 1000,
                'scenario': 'Imported'},
    'timber':  {'label': 'Timber (CLT / glulam)',        'road': 1500, 'sea': 100,
                'scenario': 'European manufactured'},
}
# Kept in the same key order as calculations.SEAI_TRANSPORT_DEFAULTS — this dict
# is display-only (reports/dashboard "distances assumed" tables); the calculation
# itself reads SEAI_TRANSPORT_DEFAULTS. Keep both in sync when adding a family.

# Legacy country presets (concrete/steel/pt groups). Kept for the desktop app and
# backward compatibility; the Ireland preset now mirrors the SEAI defaults above.
DEFAULT_TRANSPORT = {
    'Ireland': {
        'concrete_sea': 0, 'concrete_road': 20,
        'steel_sea': 1000, 'steel_road': 100,
        'pt_sea': 1000, 'pt_road': 100,
    },
    'UK': {
        'concrete_sea': 0, 'concrete_road': 80,
        'steel_sea': 500, 'steel_road': 150,
        'pt_sea': 500, 'pt_road': 150,
    },
    'Other': {
        'concrete_sea': 0, 'concrete_road': 100,
        'steel_sea': 1000, 'steel_road': 100,
        'pt_sea': 1000, 'pt_road': 100,
    },
}


class EmissionCatalogue:
    """Manages the emission factor catalogue and provides auto-mapping."""

    def __init__(self, catalogue_path=None):
        self.path = catalogue_path or CATALOGUE_PATH
        self.df = None
        self.version = CATALOGUE_VERSION
        self._load()

    def _load(self):
        """Load the catalogue CSV into a pandas DataFrame."""
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"Emission catalogue not found: {self.path}")
        self.df = pd.read_csv(self.path)
        if 'e_id' not in self.df.columns:
            raise ValueError("Catalogue CSV missing required 'e_id' column")
        if self.df['e_id'].duplicated().any():
            dupes = self.df['e_id'][self.df['e_id'].duplicated()].tolist()
            raise ValueError(f"Duplicate e_id entries in catalogue: {dupes}")
        self.df = self.df.set_index('e_id')
        # Convert numeric columns
        numeric_cols = [
            'carbon_a1_a3_kgco2e_kg', 'density_kg_per_m3',
            'a4_kgco2e_kg_sea', 'a4_kgco2e_kg_road', 'a5_kgco2e_kg'
        ]
        for col in numeric_cols:
            if col in self.df.columns:
                self.df[col] = pd.to_numeric(self.df[col], errors='coerce').fillna(0)

    # ── Factor classification metadata (family / includes_reinforcement /
    #    structural_use columns, catalogue 2026.07-r2). Defaults keep older
    #    catalogue files without the columns working unchanged. ──────────────
    def factor_family(self, e_id):
        """'in_situ' | 'precast' | 'block' | 'section' | 'rebar' | 'sheet' | 'strand'."""
        try:
            v = str(self.df.loc[str(e_id).strip(), 'family']).strip().lower()
            return v if v and v != 'nan' else ''
        except (KeyError, TypeError):
            return ''

    def factor_includes_reinforcement(self, e_id):
        """True when the factor already covers reinforcement (e.g. precast,
        prestressed hollowcore) — rate-based rebar/PT must not be added on top."""
        try:
            v = str(self.df.loc[str(e_id).strip(), 'includes_reinforcement']).strip().upper()
            return v == 'Y'
        except (KeyError, TypeError):
            return False

    def factor_structural_use(self, e_id):
        """False for sheet/coil/pipe steel products that are not structural members."""
        try:
            v = str(self.df.loc[str(e_id).strip(), 'structural_use']).strip().upper()
            return v != 'N'
        except (KeyError, TypeError):
            return True

    def get_concrete_eid(self, grade_label, ggbs_pct=0):
        """
        Get the e_id for a concrete grade and GGBS percentage.

        Args:
            grade_label: e.g. '32/40' or '32/40 MPa'
            ggbs_pct: 0, 25, 50, or 70

        Returns:
            str: e_id like 'C_020' or None if not found
        """
        # Clean grade label
        clean = grade_label.replace(' MPa', '').strip()
        # Try from GRADE_LABEL_MAP first
        for key, val in GRADE_LABEL_MAP.items():
            if val == clean or key == grade_label:
                clean = val
                break

        # Normalize GGBS
        ggbs = int(ggbs_pct)
        if ggbs not in GGBS_OPTIONS:
            ggbs = 0

        eid = CONCRETE_GRADE_MAP.get((clean, ggbs))

        # Fallback: if GGBS variant not available for this grade, use 0%
        if eid is None and ggbs > 0:
            eid = CONCRETE_GRADE_MAP.get((clean, 0))
            if eid is not None:
                print(f"[catalogue] WARNING: No {ggbs}% GGBS variant for grade '{clean}' "
                      f"— falling back to 0% GGBS ({eid})")

        return eid

    def get_emission_factor(self, e_id):
        """Get all emission factors for a given e_id."""
        if e_id in self.df.index:
            row = self.df.loc[e_id]

            def _val(col, default):
                return row[col] if col in self.df.columns else default

            return {
                'e_id': e_id,
                'item_name': _val('item_name', ''),
                'mat_name': _val('mat_name', ''),
                'a1_a3': float(_val('carbon_a1_a3_kgco2e_kg', 0)),
                'density': float(_val('density_kg_per_m3', 0)),
                'a4_sea': float(_val('a4_kgco2e_kg_sea', 0)),
                'a4_road': float(_val('a4_kgco2e_kg_road', 0)),
                'a5': float(_val('a5_kgco2e_kg', 0)),
                # Biogenic carbon stored (negative kgCO2e/kg) for timber/bio-based
                # materials; 0 for everything else. Reported separately per
                # EN 16485 — never netted into A1-A3 silently.
                'sequestration': self._seq_value(_val('sequestration_kgco2e_kg', 0)),
                'data_type': _val('data_type', 'Generic'),
                'data_quality': _val('data_quality', ''),
            }
        return None

    @staticmethod
    def _seq_value(raw):
        """Parse the sequestration cell ('-' / '' → 0.0, else the float)."""
        try:
            s = str(raw).strip()
            if s in ('', '-', 'nan', 'None'):
                return 0.0
            return float(s)
        except (TypeError, ValueError):
            return 0.0

    def get_description(self, e_id):
        """Get human-readable description for an e_id."""
        if e_id in self.df.index:
            val = self.df.loc[e_id, 'mat_name'] if 'mat_name' in self.df.columns else None
            return str(val) if val is not None else e_id
        return None

    def get_concrete_entries(self):
        """Return all concrete catalogue entries."""
        return self.df[self.df.index.str.startswith('C_')]

    def get_steel_entries(self):
        """Return all steel catalogue entries (rebar + sections combined)."""
        return self.df[self.df.index.str.startswith('R_')]

    def get_steel_section_entries(self):
        """Return steel section catalogue entries (structural sections: UB, UC, CHS, etc.)."""
        return self.df[self.df.index.isin(STEEL_SECTION_EIDS)]

    def get_rebar_entries(self):
        """Return rebar/reinforcing bar catalogue entries."""
        return self.df[self.df.index.isin(REBAR_EIDS)]

    def get_pt_entries(self):
        """Return all post-tensioning catalogue entries."""
        return self.df[self.df.index.str.startswith('PT_')]

    def get_raw_dataframe(self):
        """Return the raw catalogue DataFrame for the calculation engine."""
        return self.df

    def has_ggbs_variant(self, grade_label):
        """Check if GGBS variants exist for a given concrete grade."""
        clean = grade_label.replace(' MPa', '').strip()
        for key, val in GRADE_LABEL_MAP.items():
            if val == clean or key == grade_label:
                clean = val
                break
        return any(
            (clean, ggbs) in CONCRETE_GRADE_MAP
            for ggbs in [25, 50, 70]
        )

    def get_available_ggbs(self, grade_label):
        """Get available GGBS options for a concrete grade."""
        clean = grade_label.replace(' MPa', '').strip()
        for key, val in GRADE_LABEL_MAP.items():
            if val == clean or key == grade_label:
                clean = val
                break
        available = [0]
        for ggbs in [25, 50, 70]:
            if (clean, ggbs) in CONCRETE_GRADE_MAP:
                available.append(ggbs)
        return available
