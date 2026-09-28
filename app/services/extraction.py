"""
Quantity extraction - the single entry point for every upload type.
===================================================================
Lifted from the legacy `web_app._parse_file_background` (web_app.py:286-453),
with the Flask/session plumbing removed. The numerical and classification logic
is carried over verbatim and in the same order; only the storage of the result
changed (returns an ExtractionResult instead of mutating a global SESSIONS dict).

ONE function handles all four ingest paths - IFC, ready BOQ, Revit takeoff and
unknown/scratch spreadsheet - because they are only two branches at the top
(`.ifc` vs everything else; the 3-way BOQ split is auto-detected inside
`parse_universal_boq`) and they share ~120 lines of normalisation that decides
which emission factor each row gets. Duplicating that tail per file type would
be a drift bug in exactly the code that must not drift.

Verified against the legacy implementation: identical element contract and
identical values for all four paths (see tests/test_extraction_parity.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app import core_bridge  # noqa: F401  - puts src/core + src/ingest on sys.path

from catalogue import CONCRETE_GRADE_MAP, GRADE_LABEL_MAP, DEFAULT_STEEL_EID
from steel_rates import get_steel_rate
from engine import process_ifc, _taxonomy_lookup, _precast_default_eid
from web_helpers import (_default_steel_rates, _pt_applicable, _factor_type,
                         _kg_per_m_from_name, _BUCKET_DEFAULT_EID)

# Reverse map: concrete e_id -> (grade dropdown label, ggbs %) so a "ready" BOQ's
# concrete rows initialise the grade/GGBS selectors to match the file instead of
# silently defaulting to 32/40 / 0%. Pure derivation from catalogue constants -
# same construction as web_app.py:43-48.
_SHORT_TO_LABEL = {v: k for k, v in GRADE_LABEL_MAP.items()}
_EID_TO_GRADE_GGBS = {
    eid: (_SHORT_TO_LABEL.get(grade, f'{grade} MPa'), ggbs)
    for (grade, ggbs), eid in CONCRETE_GRADE_MAP.items()
}

SUPPORTED_EXTENSIONS = {'.ifc', '.csv', '.xlsx', '.xls'}


@dataclass
class ExtractionResult:
    """Everything the wizard's Step-1/Step-2 needs from a parsed upload.

    `elements_df` and `geometry_data` are heavyweight in-process artefacts that
    later stages consume; the API serialises only the light fields. Keeping the
    DataFrame here (rather than re-deriving it) preserves the legacy contract
    where /run reuses the exact frame produced at parse time.
    """
    source_type: str                       # 'ifc' | 'csv'
    n_elements: int
    categories: list[str]
    default_rates: dict[str, float]
    has_real_eids: bool
    elements: list[dict[str, Any]]
    csv_fmt: str | None = None
    elements_df: pd.DataFrame | None = field(default=None, repr=False)
    geometry_data: list = field(default_factory=list, repr=False)
    processor: Any = field(default=None, repr=False)   # IFCProcessor, for meshes

    def to_public_dict(self) -> dict:
        """The JSON-safe subset the frontend receives (no DataFrame, no meshes)."""
        return {
            'source_type': self.source_type,
            'n_elements': self.n_elements,
            'categories': self.categories,
            'default_rates': self.default_rates,
            'has_real_eids': self.has_real_eids,
            'elements': self.elements,
            'csv_fmt': self.csv_fmt,
        }


def extract_quantities(file_path: str, ext: str, *, gemini_key: str | None = None,
                       defer_meshes: bool = True) -> ExtractionResult:
    """Parse an uploaded IFC/CSV/Excel file into the uniform element contract.

    Args:
        file_path:    path to the saved upload
        ext:          lowercase extension including the dot ('.ifc', '.csv', ...)
        gemini_key:   optional Google AI key; used ONLY on the tabular path, to
                      map columns the rule + fuzzy matchers could not resolve.
        defer_meshes: IFC only - skip cosmetic 3D-viewer tessellation so
                      quantities return without waiting on geometry. Call
                      build_viewer_meshes(result) afterwards to fill them in.

    Returns:
        ExtractionResult - identical element contract for every input type.
    """
    ext = (ext or '').lower()
    csv_fmt = None
    processor = None

    if ext == '.ifc':
        elements_df, geometry_data, processor = process_ifc(
            file_path, defer_viewer_meshes=defer_meshes, return_processor=True)
        source_type = 'ifc'
    else:
        # The universal parser is a superset of the ready/Revit readers: it picks
        # the real BOQ sheet (skipping README/Cover/...), splits units, and only
        # calls the LLM for columns the rules and fuzzy matcher could not resolve.
        from boq_parser import parse_universal_boq
        result = parse_universal_boq(file_path, api_key=gemini_key)
        _sdf = result.get('structural_df')
        elements_df = _sdf if _sdf is not None else pd.DataFrame()
        csv_fmt = result.get('format').fmt if result.get('format') else 'unknown'
        geometry_data = []
        source_type = 'csv'

    # A "ready" BOQ already carries explicit rebar/steel/PT mass rows (and real
    # e_ids). Those masses must be used DIRECTLY - not re-estimated by rate,
    # which would discard them and double-count rebar downstream.
    has_real_eids = bool(
        source_type == 'csv'
        and 'e_id' in elements_df.columns
        and elements_df['e_id'].astype(str).str.strip()
                      .replace('', pd.NA).notna().any()
    )

    elements = _build_element_contract(elements_df)
    cat_col = next((c for c in ('Category', 'category')
                    if c in elements_df.columns), None)
    categories = (sorted(elements_df[cat_col].dropna().unique().tolist())
                  if cat_col else [])

    return ExtractionResult(
        source_type=source_type,
        n_elements=len(elements_df),
        categories=categories,
        default_rates=_default_steel_rates(categories),
        has_real_eids=has_real_eids,
        elements=elements,
        csv_fmt=csv_fmt,
        elements_df=elements_df,
        geometry_data=geometry_data,
        processor=processor,
    )


def _build_element_contract(elements_df: pd.DataFrame) -> list[dict]:
    """Normalise any reader's DataFrame into the uniform 20-key element rows.

    This is the shared tail every ingest path runs through, and it is NOT
    cosmetic: it seeds blank steel e_ids to their inferred bucket, resolves the
    factor_type, auto-routes precast/hollowcore members to their precast factor,
    and recovers grade/GGBS from a file-supplied e_id. Those choices decide which
    emission factor is applied later, so all paths must share this one copy.

    Mutates `elements_df` in place for seeded steel e_ids - deliberate, and
    carried over from the legacy behaviour: the calculation path must agree with
    what the UI shows, or a blank steel row would silently calculate as rebar.
    """
    cat_col   = next((c for c in ('Category', 'category') if c in elements_df.columns), None)
    name_col  = next((c for c in ('Description', 'name') if c in elements_df.columns), None)
    vol_col   = next((c for c in ('Volume(m3)', 'volume_m3') if c in elements_df.columns), None)
    cnt_col   = next((c for c in ('Count', 'count') if c in elements_df.columns), None)
    level_col = next((c for c in ('level', 'Level') if c in elements_df.columns), None)
    mat_col   = next((c for c in ('Material', 'material') if c in elements_df.columns), None)

    out: list[dict] = []
    for i, (_ridx, row) in enumerate(elements_df.iterrows()):
        cat   = str(row[cat_col]) if cat_col else ''
        name  = str(row[name_col]) if name_col else ''
        vol   = float(row[vol_col]) if vol_col and row[vol_col] == row[vol_col] else 0.0
        cnt   = int(row[cnt_col]) if cnt_col and row[cnt_col] == row[cnt_col] else 1
        level = str(row[level_col]) if level_col else ''
        w = round(float(row.get('width', 0) or 0))
        d = round(float(row.get('depth', 0) or 0))
        size_str = f'{w}×{d}' if w > 0 and d > 0 else ''
        is_steel = bool(row.get('is_steel', False))
        material = str(row[mat_col]) if mat_col else ('Steel' if is_steel else 'Concrete')

        # IFC extraction stores mass under 'weight_kg'; BOQ/CSV under 'Mass(kg)'.
        # Display only - the calculation derives steel mass independently.
        try:
            mass = float(row.get('Mass(kg)', 0) or row.get('weight_kg', 0) or 0)
        except (TypeError, ValueError):
            mass = 0.0

        eid = str(row.get('e_id', '') or '').strip()
        factor_type = _factor_type(eid, is_steel,
                                   bool(row.get('is_structural_steel', False)),
                                   text=f"{material} {name} {cat}")

        # Seed a blank steel e_id with its inferred bucket default, and write it
        # back to the frame so the calculation agrees with the UI.
        if is_steel and not eid:
            eid = _BUCKET_DEFAULT_EID.get(factor_type, DEFAULT_STEEL_EID)
            if 'e_id' in elements_df.columns:
                elements_df.at[_ridx, 'e_id'] = eid
            else:
                elements_df['e_id'] = elements_df.get('e_id', '')
                elements_df.at[_ridx, 'e_id'] = eid

        default_rebar = _default_steel_rates([cat]).get(cat, 150)
        _rinfo = get_steel_rate(cat)
        rebar_hint = f"{_rinfo['min']}–{_rinfo['max']} kg/m³"
        pt_applicable = _pt_applicable(cat, is_steel)

        # Hollowcore / precast beams & columns pre-select the precast factor the
        # calculation will use. A row with a real file e_id keeps its own.
        _pc_auto = (_precast_default_eid(name, cat)
                    if (not is_steel and not eid) else None)
        if _pc_auto:
            eid = _pc_auto

        # Steel mass provenance (display only): 'file' when the row carries an
        # explicit mass that isn't merely volume x 7850.
        kg_per_m_hint, mass_source = 0.0, ''
        if is_steel:
            kg_per_m_hint = _kg_per_m_from_name(name)
            mass_source = ('file' if mass > 0 and
                           (vol <= 0 or abs(mass - vol * 7850) > 0.005 * mass)
                           else 'volume')

        # Surface the grade/GGBS implied by a concrete row's file e_id so the UI
        # selectors start matching the file (and changing them then works).
        grade_label, ggbs_pct_file = ('', None)
        if not is_steel:
            grade_label, ggbs_pct_file = _EID_TO_GRADE_GGBS.get(eid, ('', None))

        out.append({
            'idx': i, 'cat': cat, 'name': name, 'level': level,
            'vol': round(vol, 4), 'count': cnt, 'size': size_str,
            'is_steel': is_steel, 'material': material,
            'e_id': eid, 'factor_type': factor_type,
            'grade': grade_label, 'ggbs': ggbs_pct_file,
            'mass': round(mass, 2), 'default_rebar': default_rebar,
            'rebar_hint': rebar_hint, 'pt_applicable': pt_applicable,
            'kg_per_m_hint': kg_per_m_hint, 'mass_source': mass_source,
            # Only genuinely UNKNOWN-material rows get the full-database picker.
            # Concrete rows keep grade + rebar inputs; steel keeps its factor
            # dropdown. No-factor materials (masonry/timber) also get the picker
            # so the user can assign a real factor instead of being stuck at 0.
            'allow_full_factor': (
                str(material).strip().lower() in ('unknown', 'other', '')
                or (not is_steel and not _taxonomy_lookup(name, cat)[2])
                or _pc_auto is not None
            ),
        })
    return out


def build_viewer_meshes(result: ExtractionResult) -> None:
    """Fill in the deferred 3D-viewer meshes for an IFC extraction.

    Cosmetic only - quantities are unaffected, and a failure is non-fatal (the
    viewer falls back to box geometry). Mutates result.geometry_data in place.
    """
    if result.processor is None:
        return
    try:
        result.processor.build_viewer_meshes()
    except Exception:
        pass
