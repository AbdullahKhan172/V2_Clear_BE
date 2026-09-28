import json
import math
import re
from datetime import datetime
from html import escape as _he
from utils import round_floats


# ── 3D-viewer geometry helpers ──────────────────────────────────────────────
# Moved here verbatim from the retired html_template_1.py (which is now deleted);
# this is the only code from template 1 that was still in use. Behaviour is
# unchanged — same functions, same logic.
def _strip_instance_id(name):
    """Strip Revit instance IDs (trailing :DIGITS) from element names."""
    if not name or ':' not in name:
        return name
    parts = name.rsplit(':', 1)
    if len(parts) == 2 and parts[1].strip().isdigit():
        return parts[0]
    return name


# engine.prepare_boq_from_ifc appends exactly one of these to an element's own
# name when it adds a paired concrete/rebar/PT row (see engine.py's
# f"{name} - Concrete" / f"{name} - Rebar (...)" / f"{name} - PT Steel (...)").
# Anchored to end-of-string and to these three exact labels only, so a name
# that legitimately contains " - " itself (e.g. "Structural Beam - SHS
# 100x100x5.0 - 304L SS", "Kickplate - FB 6mm - 304L SS" — pervasive in
# Revit-exported steel BOQs) is never mistaken for a suffixed pair-row.
_BOQ_PAIR_SUFFIX_RE = re.compile(r' - (?:Concrete|Rebar \([^()]*\)|PT Steel \([^()]*\))$')


def _strip_boq_pair_suffix(desc):
    return _BOQ_PAIR_SUFFIX_RE.sub('', desc)


def _geometry_origin_offset(geometry_data):
    """(min_x, min_y, min_z) across every element's own position.

    Real-world survey/site coordinates (e.g. Irish Grid easting/northing in
    the hundreds of thousands) are fine for the volume/carbon calculations,
    which don't care where the origin is — but Three.js renders nothing
    visible for geometry that far from (0,0,0): float32 precision loss and
    the default camera/clipping planes both break down at that distance.
    Subtracting this offset from every position and vertex keeps element
    shapes and spacing identical while bringing the scene back near origin.
    Falls back to (0,0,0) — a no-op shift — when geometry_data is empty.
    """
    if not geometry_data:
        return (0.0, 0.0, 0.0)
    try:
        min_x = min(float(g.get('x', 0) or 0) for g in geometry_data)
        min_y = min(float(g.get('y', 0) or 0) for g in geometry_data)
        min_z = min(float(g.get('z', 0) or 0) for g in geometry_data)
        return (min_x, min_y, min_z)
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0)


def _prepare_geometry_json(geometry_data, detailed_df):
    """
    Build compact geometry + emission list for the 3D viewer.
    Each entry: {x,y,z, w,d,h, cat, name, total, per_m3}
    Positions are shifted so the model's own bounding-box min corner sits at
    (0,0,0) — see _geometry_origin_offset.
    """
    if not geometry_data:
        return []
    ox, oy, oz = _geometry_origin_offset(geometry_data)

    # Build emission maps from detailed_df
    total_map = {}
    a1a3_map = {}
    vol_map = {}
    concrete_vol_map = {}
    if detailed_df is not None and len(detailed_df) > 0:
        for _, row in detailed_df.iterrows():
            desc = str(row.get('Description', ''))
            base = _strip_boq_pair_suffix(desc).strip()
            base = _strip_instance_id(base)  # normalise to match stripped geometry names
            total_map[base] = total_map.get(base, 0) + \
                float(row.get('Total Emission(tCO2e)', 0) or 0)
            # Include A1-A3 for all mapped members (concrete, steel, PT, etc.).
            a1a3_map[base] = a1a3_map.get(base, 0) + \
                float(row.get('A1-A3 Emission(kgCO2e)', 0) or 0)
            row_vol = float(row.get('Total Volume(m3)', 0) or 0)
            vol_map[base] = vol_map.get(base, 0) + row_vol
            eid = str(row.get('e_id', '') or '').strip()
            material = str(row.get('Material', '') or '').strip().lower()
            if eid.startswith('C_') or material == 'concrete':
                concrete_vol_map[base] = concrete_vol_map.get(base, 0) + row_vol

    # Count geometry elements per name (for per-instance emission share)
    name_counts = {}
    stripped_names = []
    for g in geometry_data:
        nm = _strip_instance_id(str(g.get('name', '')))
        stripped_names.append(nm)
        name_counts[nm] = name_counts.get(nm, 0) + 1

    entries = []
    for i, g in enumerate(geometry_data):
        nm = stripped_names[i]
        cat = g.get('category', 'Other')
        cnt = max(name_counts.get(nm, 1), 1)

        # Direct name match only — no category-level fallback. If the element
        # was excluded in Step 2 filtering, it will have no match in detailed_df
        # and must read zero everywhere (total = 0, per_m3 = 0). Assigning a
        # category-average to excluded elements would understate the cost of
        # filtering decisions and confuse the user.
        t = total_map.get(nm, 0) / cnt
        a1a3 = a1a3_map.get(nm, 0) / cnt
        v = vol_map.get(nm, 0) / cnt
        v_a1a3 = concrete_vol_map.get(nm, 0) / cnt
        if v_a1a3 <= 0:
            v_a1a3 = v

        per_m3 = round((t * 1000 / v), 1) if v > 0 else 0
        per_m3_a1a3 = round((a1a3 / v_a1a3), 1) if v_a1a3 > 0 else 0

        entry = {
            'x': round(float(g.get('x', 0) or 0) - ox, 3),
            'y': round(float(g.get('y', 0) or 0) - oy, 3),
            'z': round(float(g.get('z', 0) or 0) - oz, 3),
            'w': round(float(g.get('width', 0.3) or 0.3), 3),
            'd': round(float(g.get('depth', 0.3) or 0.3), 3),
            'h': round(float(g.get('height', 0.3) or 0.3), 3),
            'cat': cat,
            'name': nm,
            'level': str(g.get('level', '') or ''),
            'total': round(t, 4),
            'a1_a3_t': round(a1a3 / 1000, 4),
            'a1_a3_kg': round(a1a3, 3),
            'per_m3': per_m3,
            'per_m3_a1a3': per_m3_a1a3,
        }
        entries.append(entry)
    return entries


def _prepare_geometry_json_mesh(geometry_data, detailed_df):
    """
    Build geometry + emission list WITH real mesh data for the advanced viewer.
    Falls back to box dims when vertices/triangles are absent. Vertices are
    shifted by the same origin offset _prepare_geometry_json applies to
    entry x/y/z, so mesh geometry and box-fallback geometry stay consistent.
    """
    # Get base entries (box + emission data)
    entries = _prepare_geometry_json(geometry_data, detailed_df)
    if not entries or not geometry_data:
        return entries
    ox, oy, oz = _geometry_origin_offset(geometry_data)

    # Attach real mesh data where available
    for i, g in enumerate(geometry_data):
        if i >= len(entries):
            break
        verts = g.get('vertices')
        tris = g.get('triangles')
        if verts and tris and len(verts) >= 3 and len(tris) >= 1:
            # Flatten vertices: [[x,y,z],...] → [x,y,z,x,y,z,...]
            flat_v = []
            for v in verts:
                flat_v.extend([round(v[0] - ox, 4), round(v[1] - oy, 4), round(v[2] - oz, 4)])
            # Flatten triangles: [[i0,i1,i2],...] → [i0,i1,i2,...]
            flat_t = []
            for t in tris:
                flat_t.extend([t[0], t[1], t[2]])
            entries[i]['v'] = flat_v
            entries[i]['t'] = flat_t
    return entries


class _SafeEncoder(json.JSONEncoder):
    """JSON encoder that converts NaN/Inf to 0 and numpy types to Python natives."""
    def default(self, obj):
        try:
            import numpy as _np
            if isinstance(obj, _np.floating):
                v = float(obj)
                return 0.0 if (math.isnan(v) or math.isinf(v)) else round(v, 6)
            if isinstance(obj, _np.integer):
                return int(obj)
            if isinstance(obj, _np.ndarray):
                return obj.tolist()
        except ImportError:
            pass
        return super().default(obj)

    def encode(self, obj):
        # Pre-process to sanitize NaN/Inf in floats before encoding
        obj = round_floats(obj)
        return super().encode(obj)

    def iterencode(self, obj, _one_shot=False):
        obj = round_floats(obj)
        return super().iterencode(obj, _one_shot)


def _js_embed_safe(s):
    """Neutralise HTML-significant characters in a JSON string that will be
    embedded inside an inline <script> block. Escaping '<' and '>' to their
    \\u00XX forms means no substring (</script>, <img …>, <!--) can ever break
    out of the script element or be sniffed as markup. The values are identical
    JSON. (json.dumps ensure_ascii=True already handles U+2028/U+2029.)"""
    return s.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def _safe_json(obj):
    """Serialize obj to JSON-safe string with NaN/Inf replaced by 0.
    Hardened for inline-<script> embedding (see _js_embed_safe)."""
    return _js_embed_safe(json.dumps(round_floats(obj), cls=_SafeEncoder))


def _safe_json_geometry(obj, wrap=12):
    """Serialize the 3D geometry list to a JSON-safe string broken across many
    short lines (one element per line, with long vertex/index arrays wrapped
    every <wrap> numbers).

    The data is identical to _safe_json() output (same NaN/Inf sanitisation,
    same rounding, same values) — only the whitespace/line layout differs.
    This avoids emitting a single multi-MB line: some hosting/embedding
    pipelines (e.g. an iframe srcdoc that wraps over-long lines) inject a
    newline inside that giant line, which lands inside a JS string/number and
    breaks the whole inline script. Keeping every line short prevents that
    while preserving full geometry fidelity."""
    arr = round_floats(obj)
    if not isinstance(arr, list):
        return _safe_json(obj)
    out = ['[']
    n = len(arr)
    for ei, el in enumerate(arr):
        tail = ',' if ei < n - 1 else ''
        if isinstance(el, dict):
            parts = []
            for k, val in el.items():
                ks = json.dumps(k)
                if isinstance(val, (list, tuple)) and len(val) > wrap:
                    chunks = [','.join(json.dumps(x) for x in val[i:i + wrap])
                              for i in range(0, len(val), wrap)]
                    vs = '[' + ',\n'.join(chunks) + ']'
                else:
                    vs = json.dumps(val, separators=(',', ':'))
                parts.append(ks + ':' + vs)
            out.append('{' + ','.join(parts) + '}' + tail)
        else:
            out.append(json.dumps(el, separators=(',', ':')) + tail)
    out.append(']')
    # Same inline-<script> hardening as _safe_json. JSON structural characters
    # ([ ] { } , : ") contain no <, > or &, so only data content is affected.
    return _js_embed_safe('\n'.join(out))

def _to_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _stage_uncertainty_pct(stage):
    # Single source of truth: the stage contract (stages.py). Fallback values
    # keep old reports rendering if stages.py is unavailable.
    try:
        from stages import get_stage_info
        return get_stage_info(stage)['uncertainty_pct']
    except Exception:
        pass
    if stage == "Tender":
        return 7
    if stage == "Detailed Design":
        return 17
    return 25


def _row_material_label(row):
    e_id = str(row.get("e_id", "") or "").strip()
    mat = str(row.get("Material", "") or row.get("material", "")).strip().lower()
    if e_id.startswith("T_") or mat == "timber":
        return "Timber"
    if e_id.startswith("C_") or mat == "concrete":
        return "Concrete"
    if e_id.startswith("PT_") or mat == "post tensioning":
        return "Post Tensioning"
    if e_id.startswith("R_") or mat in ("steel", "rebar", "steel section"):
        desc = f"{row.get('Description', '')} {row.get('Category', '')}".lower()
        if "rebar" in desc or "reinforc" in desc or mat == "rebar":
            return "Rebar"
        if "section" in desc or mat == "steel section":
            return "Steel Section"
        return "Rebar"
    return "Concrete"


def _build_dashboard_insights(detailed_df, detailed_data, geometry_json, project_area,
                              total_emission, stage, sensitivity_results,
                              material_based, metrics, structural_system,
                              emission_factors_used):
    import pandas as _pd

    insights = {
        "stage_uncertainty_pct": _stage_uncertainty_pct(stage),
        "material_mass_by_type": {"Concrete": 0.0, "Rebar": 0.0, "Steel Section": 0.0, "Post Tensioning": 0.0, "Timber": 0.0},
        "floor_intensity": [],
        "zone_intensity": [],
        "substructure_split": {"sub_ton": 0.0, "super_ton": 0.0, "sub_pct": 0.0},
        "slab_metrics": {"ton": 0.0, "kg_per_m2_gia": 0.0, "kg_per_m2_slab": 0.0, "area_m2_est": 0.0},
        "member_intensity": [],
        "member_efficiency": [],
        "factors": {
            "concrete_baseline": 0.0,
            "steel_baseline": 0.0,
            "ggbs_50_opt": 0.0,
            "ggbs_70_opt": 0.0,
            "ggbs_50_reduction_pct": 0.0,
            "ggbs_70_reduction_pct": 0.0,
        },
        "strategy_deltas": {
            "structural": 0.0,
            "slab": 0.0,
            "ggbs_50": 0.0,
            "ggbs_70": 0.0,
            "rebar": 0.0,
            "epd": 0.0,
            "transport": 0.0,
            "a5_refine": 0.0,
        },
        "epd_coverage_pct": 0.0,
        # Biogenic carbon stored in timber (negative tCO2e). Shown as a separate
        # indicator — never netted into the A1-A5 total.
        "sequestration_ton": 0.0,
    }

    if detailed_df is None or getattr(detailed_df, "empty", True):
        return insights

    ddf = detailed_df.copy()
    for col in [
        "Mass(kg)",
        "A1-A3 Emission(kgCO2e)",
        "A4 Emission(kgCO2e)",
        "A5 Emission(kgCO2e)",
        "Total Emission(tCO2e)",
        "Total Volume(m3)",
        "Sequestration(kgCO2e)",
    ]:
        if col not in ddf.columns:
            ddf[col] = 0.0
        ddf[col] = _pd.to_numeric(ddf[col], errors="coerce").fillna(0.0)

    insights["sequestration_ton"] = round(_to_float(ddf["Sequestration(kgCO2e)"].sum()) / 1000.0, 3)

    ddf["_material"] = ddf.apply(_row_material_label, axis=1)
    ddf["_category"] = (ddf["Category"].astype(str) if "Category" in ddf.columns
                         else _pd.Series("", index=ddf.index, dtype="string").astype(str))
    ddf["_level"] = (ddf["level"].astype(str).str.strip() if "level" in ddf.columns
                      else _pd.Series("", index=ddf.index, dtype="string").astype(str))
    ddf["_description"] = (ddf["Description"].astype(str) if "Description" in ddf.columns
                            else _pd.Series("", index=ddf.index, dtype="string").astype(str))

    mass_by_material = ddf.groupby("_material")["Mass(kg)"].sum().to_dict()
    for key in insights["material_mass_by_type"]:
        insights["material_mass_by_type"][key] = round(_to_float(mass_by_material.get(key, 0.0)), 3)

    concrete_df = ddf[ddf["_material"] == "Concrete"]
    steel_df = ddf[ddf["_material"].isin(["Rebar", "Steel Section"])]
    concrete_mass = concrete_df["Mass(kg)"].sum()
    steel_mass = steel_df["Mass(kg)"].sum()
    concrete_a1a3 = concrete_df["A1-A3 Emission(kgCO2e)"].sum() / 1000.0
    steel_a1a3 = steel_df["A1-A3 Emission(kgCO2e)"].sum() / 1000.0

    concrete_factor = (concrete_a1a3 * 1000.0 / concrete_mass) if concrete_mass > 0 else 0.0
    steel_factor = (steel_a1a3 * 1000.0 / steel_mass) if steel_mass > 0 else 0.0
    insights["factors"]["concrete_baseline"] = round(concrete_factor, 4)
    insights["factors"]["steel_baseline"] = round(steel_factor, 4)

    def _delta(key):
        obj = (sensitivity_results or {}).get(key, {})
        return max(0.0, _to_float(obj.get("delta", 0.0)))

    ggbs50_delta = _delta("ggbs_50pct")
    ggbs70_delta = _delta("ggbs_70pct")
    red50 = min(1.0, ggbs50_delta / concrete_a1a3) if concrete_a1a3 > 0 else 0.0
    red70 = min(1.0, ggbs70_delta / concrete_a1a3) if concrete_a1a3 > 0 else 0.0
    insights["factors"]["ggbs_50_reduction_pct"] = round(red50 * 100.0, 1)
    insights["factors"]["ggbs_70_reduction_pct"] = round(red70 * 100.0, 1)
    insights["factors"]["ggbs_50_opt"] = round(concrete_factor * (1.0 - red50), 4)
    insights["factors"]["ggbs_70_opt"] = round(concrete_factor * (1.0 - red70), 4)

    slab_delta = _delta("slab_thickness_30pct")
    pt_switch_delta = 0.0
    pt_locked = any(kw in (structural_system or "").lower() for kw in ["pt", "post-tension", "post tension"])
    current_per_sqm = _to_float(metrics.get("total_emission_per_sqm", 0.0))
    if project_area > 0 and not pt_locked and current_per_sqm > 198:
        pt_switch_delta = max(0.0, (current_per_sqm - 198.0) * project_area / 1000.0)
    rebar_delta = sum(_delta(k) for k in [
        "rebar_slab_15pct", "rebar_beam_15pct", "rebar_col_10pct",
        "rebar_found_10pct", "rebar_wall_10pct",
    ])
    epd_delta = max(0.0, total_emission * 0.055)
    transport_delta = max(0.0, _to_float(material_based.get("A4", 0.0)) * 0.20)
    a5_refine_delta = max(0.0, _to_float(material_based.get("A5", 0.0)) * 0.10)

    insights["strategy_deltas"] = {
        "structural": round(max(slab_delta, pt_switch_delta), 3),
        "slab": round(slab_delta, 3),
        "ggbs_50": round(ggbs50_delta, 3),
        "ggbs_70": round(ggbs70_delta, 3),
        "rebar": round(rebar_delta, 3),
        "epd": round(epd_delta, 3),
        "transport": round(transport_delta, 3),
        "a5_refine": round(a5_refine_delta, 3),
    }

    level_df = ddf[ddf["_level"].astype(str).str.len() > 0]
    if not level_df.empty and project_area > 0:
        lv = level_df.groupby("_level")["Total Emission(tCO2e)"].sum().sort_values(ascending=False)
        insights["floor_intensity"] = [
            {
                "label": str(idx),
                "ton": round(_to_float(val), 3),
                "kg_per_m2_gia": round((_to_float(val) * 1000.0) / project_area, 1),
                "share_pct": round((_to_float(val) / total_emission) * 100.0, 1) if total_emission > 0 else 0.0,
            }
            for idx, val in lv.head(6).items()
        ]

    def _zone_tag(row):
        txt = f"{row.get('_level', '')} {row.get('_description', '')} {row.get('_category', '')}".lower()
        if any(k in txt for k in ["basement", "b1", "b2", "foundation", "pile"]):
            return "Basement / Substructure"
        if any(k in txt for k in ["core", "lift", "stair", "shear wall"]):
            return "Core"
        return "Office / Floorplate"

    ddf["_zone"] = ddf.apply(_zone_tag, axis=1)
    zone = ddf.groupby("_zone")["Total Emission(tCO2e)"].sum().sort_values(ascending=False)
    if not zone.empty and project_area > 0:
        insights["zone_intensity"] = [
            {
                "label": str(idx),
                "ton": round(_to_float(val), 3),
                "kg_per_m2_gia": round((_to_float(val) * 1000.0) / project_area, 1),
                "share_pct": round((_to_float(val) / total_emission) * 100.0, 1) if total_emission > 0 else 0.0,
            }
            for idx, val in zone.items()
        ]

    sub_mask = ddf["_category"].str.lower().str.contains(
        r"foundation|footing|pile|basement|ground beam|raft|retaining", regex=True
    )
    sub_ton = _to_float(ddf.loc[sub_mask, "Total Emission(tCO2e)"].sum())
    super_ton = max(0.0, _to_float(total_emission) - sub_ton)
    sub_pct = (sub_ton / total_emission * 100.0) if total_emission > 0 else 0.0
    insights["substructure_split"] = {
        "sub_ton": round(sub_ton, 3),
        "super_ton": round(super_ton, 3),
        "sub_pct": round(sub_pct, 1),
    }

    slab_mask = ddf["_category"].str.lower().str.contains(r"slab|floor|roof", regex=True)
    slab_ton = _to_float(ddf.loc[slab_mask, "Total Emission(tCO2e)"].sum())
    slab_area_est = 0.0
    for g in geometry_json or []:
        cat = str(g.get("cat", "")).lower()
        if any(k in cat for k in ["slab", "floor", "roof"]):
            slab_area_est += max(0.0, _to_float(g.get("w", 0.0)) * _to_float(g.get("d", 0.0)))
    kg_per_m2_gia = (slab_ton * 1000.0 / project_area) if project_area > 0 else 0.0
    kg_per_m2_slab = (slab_ton * 1000.0 / slab_area_est) if slab_area_est > 0 else kg_per_m2_gia
    insights["slab_metrics"] = {
        "ton": round(slab_ton, 3),
        "kg_per_m2_gia": round(kg_per_m2_gia, 1),
        "kg_per_m2_slab": round(kg_per_m2_slab, 1),
        "area_m2_est": round(slab_area_est, 1),
    }

    cat_grp = ddf.groupby("_category", dropna=False)
    member_intensity = []
    member_eff = []
    for cat, grp in cat_grp:
        cat_name = str(cat) if str(cat).strip() else "Unclassified"
        conc_grp = grp[grp["_material"] == "Concrete"]
        conc_vol = _to_float(conc_grp["Total Volume(m3)"].sum())
        total_ton = _to_float(grp["Total Emission(tCO2e)"].sum())
        steel_mass_cat = _to_float(grp[grp["_material"].isin(["Rebar", "Steel Section"])]["Mass(kg)"].sum())
        if conc_vol > 0:
            member_intensity.append({
                "label": cat_name,
                "kgco2e_per_m3": round((total_ton * 1000.0) / conc_vol, 1),
                "ton": round(total_ton, 3),
                "conc_vol": round(conc_vol, 3),
            })
            member_eff.append({
                "label": cat_name,
                "steel_kg_per_m3": round(steel_mass_cat / conc_vol, 1),
                "steel_mass_t": round(steel_mass_cat / 1000.0, 3),
                "conc_vol": round(conc_vol, 3),
            })
    insights["member_intensity"] = sorted(member_intensity, key=lambda x: x["kgco2e_per_m3"], reverse=True)[:6]
    insights["member_efficiency"] = sorted(member_eff, key=lambda x: x["steel_kg_per_m3"], reverse=True)[:6]

    if emission_factors_used:
        epd_materials = set()
        for ef in emission_factors_used:
            dt = str(ef.get("data_type", "") or "").lower()
            is_custom = bool(ef.get("is_custom", False))
            if is_custom or "epd" in dt or "specific" in dt:
                epd_materials.add(str(ef.get("material", "")))
        total_mass = max(1.0, _to_float(ddf["Mass(kg)"].sum()))
        covered = 0.0
        if "Concrete" in epd_materials:
            covered += _to_float(mass_by_material.get("Concrete", 0.0))
        if "Steel" in epd_materials or "Rebar" in epd_materials:
            covered += _to_float(mass_by_material.get("Rebar", 0.0))
        if "Steel Section" in epd_materials:
            covered += _to_float(mass_by_material.get("Steel Section", 0.0))
        if "Post Tensioning" in epd_materials:
            covered += _to_float(mass_by_material.get("Post Tensioning", 0.0))
        insights["epd_coverage_pct"] = round(min(100.0, (covered / total_mass) * 100.0), 1)

    return insights


def render(data):
    project_info = data.project_info
    project_area = data.project_area
    metrics = data.metrics
    summary_df = data.summary_df
    detailed_data = data.detailed_data
    efficiency_rating = data.efficiency_rating
    rating_row_index = data.rating_row_index
    model_type = data.model_type
    model_base64 = data.model_base64
    distances = getattr(data, 'distances', {})
    steel_type = getattr(data, 'steel_type', '')
    pt_type = getattr(data, 'pt_type', '')
    _EAF_STEEL_NAMES = {'Ireland Average Reinforcing Steel', 'Steel Section (Ireland)'}
    is_eaf_steel = steel_type in _EAF_STEEL_NAMES

    material_types = data.material_types
    material_emissions = data.material_emissions
    emission_per_sqm_data = data.emission_per_sqm_data
    stage_emissions = list(data.stage_emissions.values())[:3]
    material_stage = data.material_stage_emissions
    concrete_emissions = material_stage['concrete']
    rebar_emissions = material_stage.get('rebar', material_stage.get('steel', [0, 0, 0]))
    structural_steel_emissions = material_stage.get('structural_steel', [0, 0, 0])
    steel_emissions = material_stage['steel']  # combined — kept for backwards-compat
    pt_emissions = material_stage['pt']
    timber_emissions = material_stage.get('timber', [0, 0, 0])
    has_pt_slab = getattr(data, 'has_pt_slab', False)

    # Sanitize all numeric fields — replace NaN/Inf with 0 so HTML generation never crashes
    def _clean(v, default=0.0):
        try:
            f = float(v)
            return default if (math.isnan(f) or math.isinf(f)) else f
        except (TypeError, ValueError):
            return default

    metrics = {k: (_clean(v) if isinstance(v, (int, float)) else v)
               for k, v in metrics.items()}

    total_emission = _clean(metrics.get('total_emission_ton', 0))
    if total_emission == 0:
        total_emission = _clean(sum(material_emissions), 0)

    material_based = {
        'A1-A3': _clean(data.stage_emissions.get('A1-A3', 0)),
        'A4': _clean(data.stage_emissions.get('A4', 0)),
        'A5': _clean(data.stage_emissions.get('A5', 0)),
    }

    # Extract unique categories from detailed data
    categories = list(set([item.get('category', 'Unknown') for item in detailed_data]))

    # Prepare 3D geometry data
    geometry_data = getattr(data, 'geometry_data', []) or []
    detailed_df = getattr(data, 'detailed_df', None)
    geometry_json = _prepare_geometry_json_mesh(geometry_data, detailed_df)

    sensitivity_results = getattr(data, 'sensitivity_results', {}) or {}
    comparison_runs     = getattr(data, 'comparison_runs', {}) or {}
    structural_system   = project_info.get('structural_system', '')
    stage               = project_info.get('stage', 'Concept / Schematic Design')
    scenario_params     = project_info.get('scenario_params', {})
    rebar_rates_used = getattr(data, 'rebar_rates_used', []) or []
    concrete_specs_used = getattr(data, 'concrete_specs_used', []) or []
    emission_factors_used = getattr(data, 'emission_factors_used', []) or []

    # Member breakdown for hotspot (from detailed_df)
    member_hotspot = {}
    detailed_df = getattr(data, 'detailed_df', None)
    if detailed_df is not None and 'Category' in detailed_df.columns:
        em_col = 'Total Emission(tCO2e)'
        if em_col in detailed_df.columns:
            import pandas as _pd
            _ddf = detailed_df.copy()
            _ddf[em_col] = _pd.to_numeric(_ddf[em_col], errors='coerce').fillna(0)
            member_hotspot = _ddf.groupby('Category')[em_col].sum().to_dict()

    dashboard_insights = _build_dashboard_insights(
        detailed_df, detailed_data, geometry_json, project_area,
        total_emission, stage, sensitivity_results,
        material_based, metrics, structural_system,
        emission_factors_used,
    )

    html_content = _get_html_head()
    html_content += _get_project_bar(project_info, project_area)
    html_content += _get_alert_banners(project_info, dashboard_insights)
    html_content += _get_tab_navigation()
    html_content += _get_analysis_tab(
        model_type, model_base64, metrics, efficiency_rating, rating_row_index,
        summary_df, total_emission, project_area, material_based, detailed_data, categories,
        geometry_json, stage, dashboard_insights
    )
    # Value Engineering tab merged into the Decarbonisation tab (see _get_decarbonisation_tab).
    html_content += _get_decarbonisation_tab(
        metrics, total_emission, project_area, stage, structural_system,
        sensitivity_results, comparison_runs, member_hotspot, scenario_params
    )
    html_content += _get_methodology_appendix(data)
    html_content += _get_methodology_bar(metrics)
    html_content += "</div>"  # close dashboard-container
    html_content += _get_sidebar_buttons()
    html_content += _get_slide_panels(detailed_data, distances, metrics,
                                      rebar_rates_used, concrete_specs_used,
                                      emission_factors_used, stage)
    html_content += _get_javascript(
        model_type, detailed_data, project_area, metrics,
        material_types, material_emissions, stage_emissions,
        concrete_emissions, rebar_emissions, structural_steel_emissions, steel_emissions, pt_emissions,
        summary_df, total_emission, material_based, categories, distances,
        geometry_json, is_eaf_steel=is_eaf_steel,
        timber_emissions=timber_emissions,
        stage=stage,
        sensitivity_results=sensitivity_results,
        dashboard_insights=dashboard_insights
    )
    html_content += "</body></html>"

    return html_content


# Static HTML head + CSS moved verbatim to dashboard_styles.py (zero
# parameters, zero dynamic data) so this file stays focused on the
# functions that actually build page content from `data`.
from .dashboard_styles import _get_html_head, _get_styles


def _get_project_bar(project_info, project_area):
    # Stage badge: makes the report's confidence level unmissable. Colours by
    # stage code (S1 concept amber, S2 detailed blue, S3 tender green).
    stage_badge = str(project_info.get('stage_badge', '') or '')
    stage_code = str(project_info.get('stage_code', '') or '')
    unc = project_info.get('uncertainty_pct', None)
    badge_html = ''
    if stage_badge:
        colors = {'S1': ('#92400e', '#fef3c7', '#f59e0b'),
                  'S2': ('#1e40af', '#dbeafe', '#3b82f6'),
                  'S3': ('#166534', '#dcfce7', '#22c55e')}
        fg, bg, bd = colors.get(stage_code, ('#334155', '#e2e8f0', '#94a3b8'))
        unc_txt = f' &middot; &plusmn;{unc:g}%' if unc else ''
        badge_html = (f'<div class="divider"></div>'
                      f'<span class="stage-badge" title="Design-stage confidence band — '
                      f'see Methodology &amp; Assumptions appendix" '
                      f'style="background:{bg};color:{fg};border:1px solid {bd};'
                      f'padding:2px 10px;border-radius:12px;font-weight:700;">'
                      f'<i class="fas fa-stamp"></i> {_he(stage_badge)}{unc_txt}</span>')
    # Dataset version intentionally NOT shown here — the project bar is for the
    # reader, not the developer. It lives in the Methodology appendix instead.
    ver_html = ''
    return f"""
<div class="dashboard-container" id="dashboard-content">
    <div class="project-bar">
        <span class="project-name"><i class="fas fa-building"></i> {_he(str(project_info.get('project_name', 'N/A')))}</span>
        <div class="divider"></div>
        <span><i class="fas fa-map-marker-alt"></i> {_he(str(project_info.get('location', 'N/A')))}</span>
        <div class="divider"></div>
        <span><i class="fas fa-user"></i> {_he(str(project_info.get('client_name', 'N/A')))}</span>
        <div class="divider"></div>
        <span><i class="fas fa-briefcase"></i> {_he(str(project_info.get('project_type', 'N/A')))} | {_he(str(project_info.get('project_stage', 'N/A')))}</span>
        {badge_html}
        <div class="divider"></div>
        <span><i class="fas fa-ruler-combined"></i> {project_area:,.2f} m²</span>
        {ver_html}
        <div class="divider"></div>
        <span><i class="fas fa-calendar-alt"></i> {datetime.now().strftime('%Y-%m-%d')}</span>
    </div>
"""


def _get_sidebar_buttons():
    return """
    <div class="sidebar-actions">
        <button class="sidebar-btn" onclick="openPanel('detailedPanel')" title="Detailed Emissions">
            <i class="fas fa-table"></i> Emissions
        </button>
        <button class="sidebar-btn" onclick="openPanel('assumptionsPanel')" title="Assumptions & Methodology">
            <i class="fas fa-info-circle"></i> Assumptions
        </button>
        <button class="sidebar-btn" onclick="goToVETab()" title="Value Engineering">
            <i class="fas fa-lightbulb"></i> Value Eng.
        </button>
    </div>
    <div id="panelOverlay" class="panel-overlay" onclick="closeAllPanels()"></div>
"""


def _get_slide_panels(detailed_data, distances, metrics,
                      rebar_rates_used=None, concrete_specs_used=None,
                      emission_factors_used=None, stage=''):
    from html import escape as _he
    from catalogue import SEAI_TRANSPORT_TABLE
    from calculations import (SEAI_CF_OUTWARD_ROAD, SEAI_CF_RETURN_ROAD, SEAI_CF_SEA)
    # Per-material transport distances actually used (SEAI Table 2).
    _tr_rows = []
    for _fam, _meta in SEAI_TRANSPORT_TABLE.items():
        _s = distances.get(f'{_fam}_sea_distance', _meta['sea'])
        _r = distances.get(f'{_fam}_road_distance', _meta['road'])
        _tr_rows.append(
            f'<tr><td>{_he(_meta["label"])}</td>'
            f'<td style="text-align:right;">{_s:g}</td>'
            f'<td style="text-align:right;">{_r:g}</td></tr>')
    transport_rows_html = ''.join(_tr_rows)
    transport_factor_note = (
        f'SEAI Table 3 factors: road {SEAI_CF_OUTWARD_ROAD:g} outward (average laden) + '
        f'{SEAI_CF_RETURN_ROAD:g} return (0% laden); sea {SEAI_CF_SEA:g} kgCO&#8322;e/kg&middot;km single leg.')
    # A5a factor actually used (override or SEAI default).
    from calculations import A5A_EMISSION_FACTOR_KGCO2E_PER_SQM as _A5A_DEF
    _a5a = float((metrics or {}).get('a5a_factor', _A5A_DEF) or _A5A_DEF)
    _a5a_override = abs(_a5a - _A5A_DEF) > 1e-6
    _a5a_src = 'site data / experience override' if _a5a_override else 'SEAI, 70% structural scope'
    # When overridden, the SEAI 40 kgCO2e/m² whole-building figure is no longer the
    # basis of the number above — say what the basis actually is instead.
    _a5a_basis = (
        f'<strong>A5a Basis:</strong> user override &mdash; supersedes the SEAI default '
        f'of {_A5A_DEF:g} kgCO&#8322;e/m&sup2; GIA'
        if _a5a_override else
        '<strong>A5a Full Building:</strong> 40 kgCO&#8322;e/m&sup2; GIA (SEAI methodology)')

    rebar_rates_used     = rebar_rates_used or []
    concrete_specs_used  = concrete_specs_used or []
    emission_factors_used = emission_factors_used or []

    # Stage hint
    if 'Tender' in stage:
        stage_note = '<span style="color:#d97706;font-weight:600;">⚠ Tender Stage: Supplier-specific EPD values recommended for all factors.</span>'
    elif 'Detailed' in stage:
        stage_note = '<span style="color:#2563eb;">ℹ Detailed Design: Generic catalogue rates used. Override with EPDs if available.</span>'
    else:
        stage_note = '<span style="color:#64748b;">ℹ Concept Stage: Generic industry factors applied (±20–30% accuracy).</span>'

    # Rebar rates table
    if rebar_rates_used:
        rebar_rows = ''.join(
            f'<tr><td>{_he(r["element"])}</td>'
            f'<td style="text-align:right;">{r["rate_kg_m3"]:.0f}</td></tr>'
            for r in rebar_rates_used
        )
        rebar_section = f"""
        <div style="margin-bottom:16px;">
            <div style="font-weight:600;font-size:0.88rem;color:var(--text);margin-bottom:8px;border-bottom:2px solid var(--accent);padding-bottom:4px;">
                <i class="fas fa-bars" style="color:var(--accent);"></i> Rebar Rates Used
            </div>
            <table class="data-table" style="font-size:0.78rem;">
                <thead><tr><th>Element</th><th style="text-align:right;">Rate (kg/m³)</th></tr></thead>
                <tbody>{rebar_rows}</tbody>
            </table>
        </div>"""
    else:
        rebar_section = ''

    # Concrete grade + GGBS table
    if concrete_specs_used:
        conc_rows = ''.join(
            f'<tr><td>{_he(s["element"])}</td>'
            f'<td style="text-align:center;">{_he(str(s["grade"]))}</td>'
            f'<td style="text-align:center;">{s["ggbs_pct"]}%</td>'
            f'<td style="text-align:right;">{s["a1_a3"]:.3f}</td></tr>'
            for s in concrete_specs_used
        )
        conc_section = f"""
        <div style="margin-bottom:16px;">
            <div style="font-weight:600;font-size:0.88rem;color:var(--text);margin-bottom:8px;border-bottom:2px solid var(--accent);padding-bottom:4px;">
                <i class="fas fa-cube" style="color:var(--accent);"></i> Concrete Strength &amp; GGBS Content
            </div>
            <table class="data-table" style="font-size:0.78rem;">
                <thead><tr><th>Element</th><th>Grade</th><th>GGBS</th><th style="text-align:right;">A1-A3 (kgCO&#8322;e/kg)</th></tr></thead>
                <tbody>{conc_rows}</tbody>
            </table>
        </div>"""
    else:
        conc_section = ''

    # Emission factors table (no e_id)
    if emission_factors_used:
        ef_rows = ''.join(
            f'<tr><td>{_he(f["material"])}</td>'
            f'<td>{_he(f["name"])}</td>'
            f'<td style="text-align:right;">{f["a1_a3"]:.3f}</td>'
            f'<td style="text-align:right;">{f["density"]:.0f}</td>'
            f'<td>{"<b style=\'color:#d97706\'>Custom</b>" if f["is_custom"] else _he(f["data_type"])}</td></tr>'
            for f in emission_factors_used
        )
        ef_section = f"""
        <div style="margin-bottom:16px;">
            <div style="font-weight:600;font-size:0.88rem;color:var(--text);margin-bottom:8px;border-bottom:2px solid var(--accent);padding-bottom:4px;">
                <i class="fas fa-database" style="color:var(--accent);"></i> Emission Factors Used
            </div>
            <div style="font-size:0.75rem;color:var(--text-light);margin-bottom:6px;">{stage_note}</div>
            <table class="data-table" style="font-size:0.78rem;">
                <thead><tr><th>Material</th><th>Product Name</th><th style="text-align:right;">A1-A3 (kgCO&#8322;e/kg)</th><th style="text-align:right;">Density (kg/m³)</th><th>Data Type</th></tr></thead>
                <tbody>{ef_rows}</tbody>
            </table>
        </div>"""
    else:
        ef_section = f"""
        <div style="margin-bottom:16px;">
            <div style="font-weight:600;font-size:0.88rem;color:var(--text);margin-bottom:8px;border-bottom:2px solid var(--accent);padding-bottom:4px;">
                <i class="fas fa-database" style="color:var(--accent);"></i> Data Sources &amp; Emission Factors
            </div>
            <div style="font-size:0.8rem;color:var(--text);">
                <div style="padding:6px 0;border-bottom:1px dotted var(--border);"><strong>Emission Database:</strong> IGBC A1-A5 Catalogue</div>
                <div style="padding:6px 0;border-bottom:1px dotted var(--border);"><strong>Benchmark:</strong> SCORS (IStructE structural carbon)</div>
                <div style="padding:6px 0;border-bottom:1px dotted var(--border);"><strong>Reinforcement default:</strong> Ireland average factor (IStructE), unless a supplier EPD is entered</div>
                <div style="padding:6px 0;border-bottom:1px dotted var(--border);"><strong>Post-tensioning default:</strong> no generic PT factor exists in the IGBC database — defaults to the same Ireland rebar factor (IStructE), unless a named supplier EPD is selected</div>
                <div style="padding:6px 0;">{stage_note}</div>
            </div>
        </div>"""

    return f"""
    <!-- Detailed Emissions Panel -->
    <div id="detailedPanel" class="slide-panel">
        <div class="slide-panel-header">
            <h3><i class="fas fa-table"></i> Detailed Emissions Data</h3>
            <button class="slide-panel-close" onclick="closeAllPanels()"><i class="fas fa-times"></i></button>
        </div>
        <div class="slide-panel-body">
            <div style="font-size:0.78rem;color:var(--text-light);margin-bottom:10px;">
                {len(detailed_data)} elements &middot; Grouped by member (Concrete &rarr; Rebar &rarr; PT)
            </div>
            <div style="overflow-x:auto;">
                <table class="data-table" style="font-size:0.73rem;">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Material</th>
                            <th>Category</th>
                            <th>Family / Type</th>
                            <th style="text-align:right;">Mass (kg)</th>
                            <th style="text-align:right;">A1-A3</th>
                            <th style="text-align:right;">A4</th>
                            <th style="text-align:right;">A5</th>
                            <th style="text-align:right;">Total (t)</th>
                        </tr>
                    </thead>
                    <tbody id="detailedPanelBody"></tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Assumptions Panel -->
    <div id="assumptionsPanel" class="slide-panel">
        <div class="slide-panel-header">
            <h3><i class="fas fa-info-circle"></i> Assumptions &amp; Methodology</h3>
            <button class="slide-panel-close" onclick="closeAllPanels()"><i class="fas fa-times"></i></button>
        </div>
        <div class="slide-panel-body">

            <!-- Transport Distances -->
            <div style="margin-bottom:16px;">
                <div style="font-weight:600;font-size:0.88rem;color:var(--text);margin-bottom:8px;border-bottom:2px solid var(--accent);padding-bottom:4px;">
                    <i class="fas fa-truck" style="color:var(--accent);"></i> Transport Distances Used (A4, SEAI Table 2)
                </div>
                <table class="data-table" style="font-size:0.78rem;">
                    <thead><tr><th>Material</th><th style="text-align:right;">Sea (km)</th><th style="text-align:right;">Road (km)</th></tr></thead>
                    <tbody>
                        {transport_rows_html}
                    </tbody>
                </table>
                <div style="font-size:0.68rem;color:var(--muted);margin-top:5px;">{transport_factor_note}</div>
            </div>

            {rebar_section}
            {conc_section}
            {ef_section}

            <!-- Calculation Methodology -->
            <div style="margin-bottom:16px;">
                <div style="font-weight:600;font-size:0.88rem;color:var(--text);margin-bottom:8px;border-bottom:2px solid var(--accent);padding-bottom:4px;">
                    <i class="fas fa-cogs" style="color:var(--accent);"></i> Calculation Methodology
                </div>
                <div style="font-size:0.8rem;color:var(--text);">
                    <div style="padding:6px 0;border-bottom:1px dotted var(--border);"><strong>Method basis:</strong> EN 15978 framework (subset application)</div>
                    <div style="padding:6px 0;border-bottom:1px dotted var(--border);"><strong>Computed scope:</strong> A1-A5 upfront embodied carbon only</div>
                    <div style="padding:6px 0;border-bottom:1px dotted var(--border);"><strong>A5a Factor:</strong> {_a5a:g} kgCO&#8322;e/m&sup2; GIA ({_a5a_src})</div>
                    <div style="padding:6px 0;border-bottom:1px dotted var(--border);">{_a5a_basis}</div>
                    <div style="padding:6px 0;border-bottom:1px dotted var(--border);"><strong>A5a Distribution:</strong> By mass fraction across elements</div>
                    <div style="padding:6px 0;border-bottom:1px dotted var(--border);"><strong>A5w (Waste):</strong> Per-material waste factor &times; (A1-A3 + A4), per SEAI A5.3</div>
                    <div style="padding:6px 0;"><strong>Social Cost:</strong> &euro;70/tCO&#8322;e</div>
                </div>
            </div>

            <div style="padding:10px;background:var(--accent-bg);border-radius:var(--radius);border:1px solid #bbf7d0;font-size:0.78rem;">
                <i class="fas fa-lightbulb" style="color:var(--accent);"></i>
                <strong>Note:</strong> Using product-specific EPD values instead of generic data can reduce calculated emissions by 5–15% while improving data quality score.
            </div>
        </div>
    </div>
"""


def _get_tab_navigation():
    return """
    <div class="tab-navigation">
        <button class="tab-button active" onclick="openTab(event, 'analysis-tab')">
            <i class="fas fa-chart-pie"></i> Carbon Analysis
        </button>
        <button class="tab-button" onclick="openTab(event, 'decarbonisation-tab')">
            <i class="fas fa-leaf"></i> Decarbonization &amp; Value Engineering
        </button>
    </div>
"""


def _get_analysis_tab(model_type, model_base64, metrics, efficiency_rating, rating_row_index,
                       summary_df, total_emission, project_area, material_based, detailed_data, categories,
                       geometry_json=None, stage='Concept / Schematic Design', dashboard_insights=None):
    dashboard_insights = dashboard_insights or {}
    badge_class = efficiency_rating.lower().replace('+', '-plus')
    _raw_psm = metrics.get('total_emission_per_sqm', 0)
    try:
        current_per_sqm = float(_raw_psm)
        if math.isnan(current_per_sqm) or math.isinf(current_per_sqm):
            current_per_sqm = 0.0
    except (TypeError, ValueError):
        current_per_sqm = 0.0
    benchmark = 220
    target_b = 200
    budget_pct = min(100, max(0, (current_per_sqm / max(1, target_b)) * 100))
    distance_to_b = current_per_sqm - target_b
    budget_color = '#16a34a' if current_per_sqm <= 200 else '#e67e22' if current_per_sqm <= 350 else '#dc2626'
    benchmark_diff = ((current_per_sqm - benchmark) / benchmark) * 100 if benchmark > 0 else 0
    social_cost = total_emission * 1000 * 0.070
    stage_unc = int(dashboard_insights.get('stage_uncertainty_pct', 25))

    stage_focus = {
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
    }.get(stage, {})

    a1_a3_pct = (material_based.get('A1-A3', 0) / total_emission * 100) if total_emission > 0 else 0
    a4_pct = (material_based.get('A4', 0) / total_emission * 100) if total_emission > 0 else 0
    a5_pct = (material_based.get('A5', 0) / total_emission * 100) if total_emission > 0 else 0

    material_rows = ""
    for _, row in summary_df.iterrows():
        em_t = _to_float(row['Total Emission(tCO2e)'])
        if em_t <= 0:
            continue
        pct = (em_t / total_emission * 100) if total_emission > 0 else 0
        per_sqm = em_t * 1000 / project_area if project_area > 0 else 0
        material_rows += f"""
                    <tr>
                        <td>{row['Material Type']}</td>
                        <td style="text-align:right;">{em_t:.2f}</td>
                        <td style="text-align:right;">{pct:.1f}%</td>
                        <td style="text-align:right;">{per_sqm:.1f}</td>
                    </tr>"""

    sorted_data = sorted(detailed_data, key=lambda x: x.get('total', 0), reverse=True)[:5]
    top_rows = ""
    for item in sorted_data:
        item_pct = (item.get('total', 0) / total_emission * 100) if total_emission > 0 else 0
        # material/category are file-derived → escape.
        top_rows += f"""
                    <tr>
                        <td>{_he(str(item.get('material', 'N/A')))}</td>
                        <td>{_he(str(item.get('category', 'N/A')))}</td>
                        <td style="text-align:right;">{item.get('mass', 0):,.0f}</td>
                        <td style="text-align:right;"><strong>{item.get('total', 0):.3f}</strong></td>
                        <td style="text-align:right;">{item_pct:.1f}%</td>
                    </tr>"""

    # row['label'] is a file-derived level/category string → escape it.
    floor_rows = ""
    for row in dashboard_insights.get('floor_intensity', []):
        floor_rows += (
            f"<tr><td>{_he(str(row['label']))}</td><td style='text-align:right;'>{row['ton']:.2f}</td>"
            f"<td style='text-align:right;'>{row['kg_per_m2_gia']:.1f}</td><td style='text-align:right;'>{row['share_pct']:.1f}%</td></tr>"
        )
    if not floor_rows:
        floor_rows = "<tr><td colspan='4' style='text-align:center;color:var(--text-muted);'>No floor-level data available</td></tr>"

    zone_rows = ""
    for row in dashboard_insights.get('zone_intensity', []):
        zone_rows += (
            f"<tr><td>{_he(str(row['label']))}</td><td style='text-align:right;'>{row['ton']:.2f}</td>"
            f"<td style='text-align:right;'>{row['kg_per_m2_gia']:.1f}</td><td style='text-align:right;'>{row['share_pct']:.1f}%</td></tr>"
        )
    if not zone_rows:
        zone_rows = "<tr><td colspan='4' style='text-align:center;color:var(--text-muted);'>No zone-level data available</td></tr>"

    mem_int_rows = ""
    for row in dashboard_insights.get('member_intensity', []):
        mem_int_rows += (
            f"<tr><td>{_he(str(row['label']))}</td><td style='text-align:right;'>{row['kgco2e_per_m3']:.1f}</td>"
            f"<td style='text-align:right;'>{row['conc_vol']:.1f}</td></tr>"
        )
    if not mem_int_rows:
        mem_int_rows = "<tr><td colspan='3' style='text-align:center;color:var(--text-muted);'>No concrete volume data available</td></tr>"

    mem_eff_rows = ""
    for row in dashboard_insights.get('member_efficiency', []):
        mem_eff_rows += (
            f"<tr><td>{_he(str(row['label']))}</td><td style='text-align:right;'>{row['steel_kg_per_m3']:.1f}</td>"
            f"<td style='text-align:right;'>{row['steel_mass_t']:.2f}</td></tr>"
        )
    if not mem_eff_rows:
        mem_eff_rows = "<tr><td colspan='3' style='text-align:center;color:var(--text-muted);'>No steel proxy efficiency data available</td></tr>"

    sub_split = dashboard_insights.get('substructure_split', {})
    slab_metrics = dashboard_insights.get('slab_metrics', {})

    has_geometry = geometry_json and len(geometry_json) > 0
    geom_categories = sorted(set(g['cat'] for g in geometry_json)) if has_geometry else []
    cat_checkboxes_2 = ""
    for cat in geom_categories:
        _cat_esc = _he(str(cat))
        cat_checkboxes_2 += f'<div class="cat-box-2 active" data-cat="{_cat_esc}" onclick="this.classList.toggle(\'active\');update3DFilters2();" style="padding:4px 8px;border-radius:4px;font-size:0.75rem;cursor:pointer;user-select:none;border:1px solid rgba(0,0,0,0.15);text-align:center;">{_cat_esc}</div>'

    if has_geometry:
        model_html = f"""
            <div style="display:flex;gap:10px;height:620px;">
                <div id="three-container-2" style="flex:1;height:620px;position:relative;background:#dce6ef;border-radius:var(--radius);overflow:hidden;">
                    <div id="three-loading-2" style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:#4a6fa5;font-family:sans-serif;text-align:center;z-index:10;">
                        <div style="font-size:1.5rem;margin-bottom:8px;">&#9881;</div>
                        <div>Loading IFC 3D Model...</div>
                    </div>
                    <canvas id="three-canvas-2" style="display:block;"></canvas>
                    <div id="element-tooltip-2" style="display:none;position:absolute;background:rgba(0,0,0,0.82);color:#fff;padding:8px 12px;border-radius:6px;font-size:0.78rem;pointer-events:none;z-index:100;border:1px solid rgba(255,255,255,0.15);max-width:300px;"></div>
                </div>
                <div style="width:220px;display:flex;flex-direction:column;gap:8px;overflow-y:auto;font-size:0.78rem;">
                    <button onclick="fit3DView2()" title="Frame the whole model (or double-click the viewer)"
                        style="padding:6px 8px;background:var(--teal);color:#fff;border:none;border-radius:5px;font-size:0.78rem;font-weight:600;cursor:pointer;">
                        <i class="fas fa-expand"></i> Fit model to view
                    </button>
                    <div style="font-weight:600;color:var(--text);margin-bottom:2px;"><i class="fas fa-palette" style="color:var(--accent);margin-right:4px;"></i>Color Mode</div>
                    <select id="color-mode-2" onchange="update3DColors2()" style="padding:4px;border:1px solid var(--border);border-radius:4px;font-size:0.78rem;">
                        <option value="category">By Category</option>
                        <option value="per_m3">Emission Intensity (kgCO₂e/m³)</option>
                        <option value="total" selected>Total Emission</option>
                    </select>
                    <div style="font-weight:600;color:var(--text);margin-top:4px;"><i class="fas fa-filter" style="color:var(--accent);margin-right:4px;"></i>Lifecycle Filter</div>
                    <select id="ec-stage-filter-2" onchange="update3DColors2()" style="padding:4px;border:1px solid var(--border);border-radius:4px;font-size:0.78rem;">
                        <option value="a1a5" selected>A1-A5 (Total)</option>
                        <option value="a1a3">A1-A3 Only</option>
                    </select>
                    <div style="font-weight:600;color:var(--text);margin-top:4px;"><i class="fas fa-building" style="color:var(--accent);margin-right:4px;"></i>Filter by Level</div>
                    <select id="level-filter-3d" onchange="update3DLevelFilter()" style="padding:4px;border:1px solid var(--border);border-radius:4px;font-size:0.78rem;">
                        <option value="all">All Levels</option>
                    </select>
                    <div style="font-weight:600;color:var(--text);margin-top:4px;"><i class="fas fa-layer-group" style="color:var(--accent);margin-right:4px;"></i>Categories</div>
                    <div style="display:flex;gap:4px;margin-bottom:2px;">
                        <button onclick="toggleAllCats2(true)" style="flex:1;padding:2px 4px;font-size:0.7rem;background:var(--accent);color:#fff;border:none;border-radius:3px;cursor:pointer;">All</button>
                        <button onclick="toggleAllCats2(false)" style="flex:1;padding:2px 4px;font-size:0.7rem;background:var(--danger);color:#fff;border:none;border-radius:3px;cursor:pointer;">None</button>
                    </div>
                    <div style="display:flex;flex-direction:column;gap:2px;max-height:120px;overflow-y:auto;">{cat_checkboxes_2}</div>
                    <div id="element-info-2" style="margin-top:6px;padding:8px;background:var(--bg);border-radius:4px;font-size:0.75rem;color:var(--text-light);min-height:120px;">
                        <i>Click an element to see embodied carbon details</i>
                    </div>
                    <div id="ec-legend-2" style="margin-top:auto;padding:6px;background:var(--bg);border-radius:4px;">
                        <div style="font-weight:600;font-size:0.72rem;margin-bottom:4px;">Emission Intensity Legend</div>
                        <div style="display:flex;align-items:center;gap:4px;font-size:0.7rem;"><span style="display:inline-block;width:14px;height:10px;border-radius:2px;background:#22c55e;"></span>Low</div>
                        <div style="display:flex;align-items:center;gap:4px;font-size:0.7rem;"><span style="display:inline-block;width:14px;height:10px;border-radius:2px;background:#f59e0b;"></span>Medium</div>
                        <div style="display:flex;align-items:center;gap:4px;font-size:0.7rem;"><span style="display:inline-block;width:14px;height:10px;border-radius:2px;background:#ef4444;"></span>High</div>
                        <div style="display:flex;align-items:center;gap:4px;font-size:0.7rem;"><span style="display:inline-block;width:14px;height:10px;border-radius:2px;background:#94a3b8;"></span>No EC Data</div>
                    </div>
                </div>
            </div>"""
    elif model_type == "glb" and model_base64:
        data_url = f"data:model/gltf-binary;base64,{model_base64}"
        model_html = f"""
            <div class="model-area expanded">
                <model-viewer id="my-model" src="{data_url}" alt="3D Model" camera-controls auto-rotate
                    shadow-intensity="1" exposure="0.5" style="width:100%;height:100%;"></model-viewer>
            </div>"""
    else:
        model_html = """
            <div class="model-area">
                <div class="no-model-mini">
                    <i class="fas fa-cube"></i>
                    <span>No 3D model uploaded. Add a GLB/GLTF/IFC file to enable visualization.</span>
                </div>
            </div>"""

    cat_options = '<option value="all">All Categories</option>'
    for cat in sorted(categories):
        _c = _he(str(cat))   # file-derived category → escape for value + text
        cat_options += f'<option value="{_c}">{_c}</option>'

    raw_levels = set()
    for item in detailed_data:
        lv = item.get('level', '')
        if lv and str(lv).strip() and str(lv).strip().lower() not in ('', 'none', 'nan', 'n/a'):
            raw_levels.add(str(lv).strip())
    level_options = '<option value="all">All Levels</option>'
    for lv in sorted(raw_levels):
        _lv = _he(str(lv))   # file-derived level → escape for value + text
        level_options += f'<option value="{_lv}">{_lv}</option>'

    leti_rows = ""
    leti_data = [
        (0, '0-50', 'Aplusplus', 'A++', ''),
        (1, '50-100', 'Aplus', 'A+', ''),
        (2, '100-150', 'A', 'A', ''),
        (3, '150-200', 'B', 'B', 'Target'),
        (4, '200-250', 'C', 'C', ''),
        (5, '250-300', 'D', 'D', ''),
        (6, '300-350', 'E', 'E', 'Avg'),
        (7, '350-400', 'F', 'F', ''),
        (8, '>400', 'G', 'G', ''),
    ]
    for idx, label, cls, rating, desc in leti_data:
        marker = ""
        if idx == rating_row_index:
            marker = f"""<div class="leti-marker"><div class="leti-marker-arrow"></div><div class="leti-marker-label">{current_per_sqm:.0f} kgCO2e/m²</div></div>"""
        desc_html = f'<span class="desc">{desc}</span>' if desc else ''
        leti_rows += f"""
                <div class="leti-row">
                    <div class="leti-label">{label}</div>
                    <div class="leti-bar leti-{cls}">{rating}{desc_html}</div>
                    {marker}
                </div>"""

    phase_show = ''.join([f"<li><span class='bullet' style='background:var(--accent);'></span>{txt}</li>" for txt in stage_focus.get('show', [])])
    phase_hide = ''.join([f"<li><span class='bullet' style='background:var(--warning);'></span>{txt}</li>" for txt in stage_focus.get('hide', [])])

    # Stage-aware uncertainty band shown under the headline numbers (display
    # only — the calculated value is unchanged and remains the number shown).
    _unc = _to_float(metrics.get('uncertainty_pct', 0))
    _tot = _to_float(metrics.get('total_emission_ton', 0))
    if _unc > 0:
        tot_band = (f"tCO2e &middot; expected {_tot * (1 - _unc / 100):,.1f}"
                    f"&ndash;{_tot * (1 + _unc / 100):,.1f} (&plusmn;{_unc:g}%)")
        psqm_band = (f"kgCO2e/m² &middot; expected {current_per_sqm * (1 - _unc / 100):,.0f}"
                     f"&ndash;{current_per_sqm * (1 + _unc / 100):,.0f} (&plusmn;{_unc:g}%)")
    else:
        tot_band = "tCO2e"
        psqm_band = "kgCO2e/m²"

    return f"""
    <div id="analysis-tab" class="tab-content active">
        <div class="grid grid-4">
            <div class="kpi-card green">
                <div class="kpi-label">Total Emissions</div>
                <div class="kpi-value">{metrics['total_emission_ton']:.2f}</div>
                <div class="kpi-unit">{tot_band}</div>
            </div>
            <div class="kpi-card {'green' if current_per_sqm <= 200 else 'orange' if current_per_sqm <= 350 else 'red'}">
                <div class="kpi-label">Carbon Intensity</div>
                <div class="kpi-value">{current_per_sqm:.1f}</div>
                <div class="kpi-unit">{psqm_band}</div>
            </div>
            <div class="kpi-card blue">
                <div class="kpi-label">Carbon Budget vs SCORS B</div>
                <div class="kpi-value">{budget_pct:.0f}%</div>
                <div class="kpi-unit">{('On target' if distance_to_b <= 0 else f'{distance_to_b:.0f} above target')} kgCO2e/m²</div>
            </div>
            <div class="kpi-card {'green' if current_per_sqm <= 200 else 'orange' if current_per_sqm <= 350 else 'red'}">
                <div class="kpi-label">Social Cost of Carbon</div>
                <div class="kpi-value">&euro;{social_cost:,.0f}</div>
                <div class="kpi-unit">EUR</div>
            </div>
        </div>

        <div class="grid grid-2">
            <div class="card" style="display:flex;align-items:center;gap:16px;padding:10px 14px;">
                <div class="rating-badge {badge_class}">{efficiency_rating}</div>
                <div style="flex:1;">
                    <div style="font-size:0.82rem;font-weight:600;color:var(--text);margin-bottom:2px;">
                        SCORS Structural Carbon Rating: {efficiency_rating}
                    </div>
                    <div class="budget-bar"><div class="budget-fill" style="width:{min(100, budget_pct)}%;background:{budget_color};"></div></div>
                    <div style="display:flex;justify-content:space-between;font-size:0.7rem;color:var(--text-muted);">
                        <span>0 kgCO2e/m² (A++)</span>
                        <span style="color:{budget_color};font-weight:600;">{'On target' if distance_to_b <= 0 else f'{distance_to_b:.0f} kgCO2e/m² above SCORS target (B)'}</span>
                        <span>500+</span>
                    </div>
                </div>
                <div style="text-align:center;padding:6px 12px;background:var(--bg);border-radius:var(--radius-sm);border:1px solid var(--border);min-width:90px;">
                    <div style="font-size:0.65rem;color:var(--text-muted);text-transform:uppercase;">Benchmark</div>
                    <div style="font-size:1.1rem;font-weight:700;color:var(--dark);">220</div>
                    <div style="font-size:0.65rem;color:{budget_color};font-weight:600;">{benchmark_diff:+.0f}%</div>
                </div>
            </div>
            <div class="card" style="padding:10px 14px;">
                <div style="font-size:0.82rem;font-weight:600;color:var(--text);margin-bottom:6px;">
                    <i class="fas fa-map-signs" style="color:var(--accent);"></i> Stage Focus: {stage}
                </div>
                <div style="font-size:0.78rem;color:var(--text-light);margin-bottom:8px;">Reporting uncertainty currently ±{stage_unc}%</div>
                <ul class="findings-list">{phase_show}</ul>
                <div style="font-size:0.78rem;font-weight:600;color:var(--warning);margin:8px 0 4px;">Collapse / lock at this stage</div>
                <ul class="findings-list">{phase_hide}</ul>
            </div>
        </div>

        <div style="display:grid;grid-template-columns:290px 1fr 1fr;gap:12px;margin-bottom:12px;">
            <!-- SCORS Scale — fixed narrow column -->
            <div class="card" style="padding:10px 12px;min-width:0;">
                <div class="card-title"><i class="fas fa-award"></i> SCORS Rating Scale</div>
                <div class="leti-scale">{leti_rows}</div>
            </div>
            <!-- Materials contribution histogram — stacked by A1-A3 / A4 / A5 -->
            <div class="card" style="padding:10px 12px;min-width:0;">
                <div class="card-title"><i class="fas fa-chart-bar"></i> Materials Contribution (tCO₂e)</div>
                <div id="materialsHistogramChart" style="width:100%;height:300px;"></div>
            </div>
            <!-- Stage breakdown — bar on top, pie below, side by side -->
            <div class="card" style="padding:10px 12px;min-width:0;">
                <div class="card-title"><i class="fas fa-chart-bar"></i> A1-A5 Stage Breakdown</div>
                <div id="lifecycleBarChart" style="width:100%;height:140px;"></div>
                <div id="stagePieChart" style="width:100%;height:170px;margin-top:4px;"></div>
            </div>
        </div>

        <div class="grid grid-12">
            <div class="card col-4" style="padding:10px 12px;">
                <div class="card-title"><i class="fas fa-cubes"></i> Material Summary</div>
                <table class="data-table">
                    <thead><tr><th>Material</th><th style="text-align:right;">tCO2e</th><th style="text-align:right;">%</th><th style="text-align:right;">kg/m² GIA</th></tr></thead>
                    <tbody>{material_rows}
                        <tr class="total-row"><td><strong>Total</strong></td><td style="text-align:right;"><strong>{total_emission:.2f}</strong></td><td style="text-align:right;"><strong>100%</strong></td><td style="text-align:right;"><strong>{(total_emission * 1000 / max(1, project_area)):.1f}</strong></td></tr>
                    </tbody>
                </table>
            </div>
            <div class="card col-4" style="padding:10px 12px;">
                <div class="card-title"><i class="fas fa-fire"></i> Top 5 Carbon Hotspots</div>
                <table class="data-table">
                    <thead><tr><th>Material</th><th>Category</th><th style="text-align:right;">Mass (kg)</th><th style="text-align:right;">tCO2e</th><th style="text-align:right;">%</th></tr></thead>
                    <tbody>{top_rows}</tbody>
                </table>
            </div>
            <div class="card col-4" style="padding:10px 12px;">
                <div class="card-title"><i class="fas fa-building"></i> Per-floor and Zone Intensity</div>
                <div style="font-size:0.76rem;color:var(--text-muted);margin-bottom:4px;">Offices vs core vs basement and level ranking.</div>
                <table class="data-table" style="margin-bottom:6px;">
                    <thead><tr><th>Level</th><th style="text-align:right;">tCO2e</th><th style="text-align:right;">kg/m²</th><th style="text-align:right;">%</th></tr></thead>
                    <tbody>{floor_rows}</tbody>
                </table>
                <table class="data-table">
                    <thead><tr><th>Zone</th><th style="text-align:right;">tCO2e</th><th style="text-align:right;">kg/m²</th><th style="text-align:right;">%</th></tr></thead>
                    <tbody>{zone_rows}</tbody>
                </table>
            </div>
        </div>

        <div class="grid grid-12">
            <div class="card col-4" style="padding:10px 12px;">
                <div class="card-title"><i class="fas fa-project-diagram"></i> Substructure vs Superstructure</div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
                    <div style="padding:10px;background:#eef2ff;border:1px solid #c7d2fe;border-radius:6px;">
                        <div style="font-size:0.72rem;color:#4c1d95;">Substructure</div>
                        <div style="font-size:1.2rem;font-weight:700;color:#4338ca;">{sub_split.get('sub_ton', 0.0):.1f} t</div>
                        <div style="font-size:0.72rem;color:#4c1d95;">{sub_split.get('sub_pct', 0.0):.1f}% of total</div>
                    </div>
                    <div style="padding:10px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;">
                        <div style="font-size:0.72rem;color:#166534;">Superstructure</div>
                        <div style="font-size:1.2rem;font-weight:700;color:#15803d;">{sub_split.get('super_ton', 0.0):.1f} t</div>
                        <div style="font-size:0.72rem;color:#166534;">{100 - sub_split.get('sub_pct', 0.0):.1f}% of total</div>
                    </div>
                </div>
                <div style="margin-top:8px;padding:8px;background:var(--bg);border-radius:6px;font-size:0.76rem;color:var(--text-light);">
                    Slab-system EC: <strong>{slab_metrics.get('kg_per_m2_slab', 0.0):.1f} kgCO2e/m² slab</strong>
                    ({slab_metrics.get('kg_per_m2_gia', 0.0):.1f} kgCO2e/m² GIA; slab area estimate {slab_metrics.get('area_m2_est', 0.0):.0f} m²).
                </div>
            </div>
            <div class="card col-4" style="padding:10px 12px;">
                <div class="card-title"><i class="fas fa-ruler-combined"></i> Member Efficiency Proxy</div>
                <table class="data-table">
                    <thead><tr><th>Member Type</th><th style="text-align:right;">Steel kg/m³ concrete</th><th style="text-align:right;">Steel mass (t)</th></tr></thead>
                    <tbody>{mem_eff_rows}</tbody>
                </table>
            </div>
            <div class="card col-4" style="padding:10px 12px;">
                <div class="card-title"><i class="fas fa-vial"></i> Member EC Intensity Sensitivity</div>
                <table class="data-table">
                    <thead><tr><th>Member Type</th><th style="text-align:right;">kgCO2e/m³ concrete</th><th style="text-align:right;">Concrete m³</th></tr></thead>
                    <tbody>{mem_int_rows}</tbody>
                </table>
            </div>
        </div>

        <div class="card" style="padding:10px 14px;">
            <div class="collapse-toggle {'open' if model_type == 'glb' and model_base64 else ''}" onclick="toggleCollapse(this)">
                <i class="fas fa-chevron-right"></i>
                <i class="fas fa-cube" style="color:var(--accent);"></i> IFC 3D Viewer
            </div>
            <div class="collapse-body {'open' if model_type == 'glb' and model_base64 else ''}">{model_html}</div>
        </div>

        <div class="card" style="padding:10px 14px;">
            <div class="collapse-toggle" onclick="toggleCollapse(this)">
                <i class="fas fa-chevron-right"></i>
                <i class="fas fa-table" style="color:var(--accent);"></i> Detailed Element Data ({len(detailed_data)} elements)
            </div>
            <div class="collapse-body">
                <div class="filter-inline">
                    <select id="filter-material">
                        <option value="all">All Materials</option>
                        <option value="Concrete">Concrete</option>
                        <option value="Rebar">Rebar</option>
                        <option value="Steel Section">Steel Section</option>
                        <option value="Post Tensioning">Post Tensioning</option>
                        <option value="Timber">Timber</option>
                    </select>
                    <select id="filter-category">{cat_options}</select>
                    <select id="filter-level">{level_options}</select>
                    <select id="filter-emission">
                        <option value="all">All EC Levels</option>
                        <option value="high">High (top third)</option>
                        <option value="medium">Medium (mid third)</option>
                        <option value="low">Low (bottom third)</option>
                        <option value="zero">No EC Data</option>
                    </select>
                    <button class="filter-btn primary" onclick="applyFilters()"><i class="fas fa-filter"></i> Apply</button>
                    <button class="filter-btn secondary" onclick="resetFilters()"><i class="fas fa-undo"></i> Reset</button>
                    <span id="filterSummary" style="font-size:0.78rem;color:var(--text-light);margin-left:auto;"></span>
                </div>
                <div style="max-height:300px;overflow-y:auto;">
                    <table class="data-table" id="filteredDataTable">
                        <thead>
                            <tr>
                                <th>Material</th>
                                <th>Category</th>
                                <th>Family / Type</th>
                                <th style="text-align:right;">Mass (kg)</th>
                                <th style="text-align:right;">A1-A5 (kgCO2e)</th>
                                <th style="text-align:right;">Total (tCO2e)</th>
                            </tr>
                        </thead>
                        <tbody id="filteredDataBody"></tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
"""


def _get_decarbonisation_tab(metrics, total_emission, project_area, stage, structural_system,
                              sensitivity_results, comparison_runs, member_hotspot, _scenario_params):
    def _c(v, d=0.0):
        try:
            f = float(v)
            return d if (math.isnan(f) or math.isinf(f)) else f
        except (TypeError, ValueError):
            return d
    current_per_sqm = _c(metrics.get('total_emission_per_sqm', 0))
    current_total   = _c(total_emission) or 0

    # ── Ireland office case study benchmarks (5,062 m², validated) ───────────
    BENCHMARKS = [
        ("RC Flat Slab",         278),
        ("Steel + Hollowcore",   258),
        ("In-Situ + Hollowcore", 236),
        ("PT Slab with Caps",    205),
        ("PT Flat Slab",         198),
    ]
    best_benchmark = min(b[1] for b in BENCHMARKS)
    max_bench      = max(b[1] for b in BENCHMARKS)
    bar_ref        = max(current_per_sqm, max_bench, 1)

    # ── Stage config ─────────────────────────────────────────────────────────
    STAGE_CFG = {
        "Concept / Schematic Design": {
            "color": "#16a34a", "range": "±20–30%", "unc": 25, "icon": "🔓",
            "msg": "Design choices are still open — this is where you save the most carbon. "
                   "Locked-in decisions at this stage account for <strong>70–90% of your final footprint</strong>.",
            "lock_msg": ""},
        "Detailed Design": {
            "color": "#d97706", "range": "±15–20%", "unc": 17, "icon": "🔒",
            "msg": "Structural system is locked. Remaining levers: material specification, "
                   "SCM/GGBS content, rebar optimisation. <strong>Max remaining saving: ~10–20%.</strong>",
            "lock_msg": "System switch interventions shown below are locked — they required a Concept stage decision."},
        "Tender": {
            "color": "#dc2626", "range": "±5–10%", "unc": 7, "icon": "🔒",
            "msg": "Structural and material decisions are fixed. Focus on supplier EPDs and actual transport distances. "
                   "<strong>Max remaining saving: ~5–10%.</strong>",
            "lock_msg": "Only EPD substitution and transport optimisation remain viable at this stage."},
    }
    cfg          = STAGE_CFG.get(stage, STAGE_CFG["Concept / Schematic Design"])
    unc_val      = cfg["unc"]
    unc_color    = cfg["color"]
    unc_label    = cfg["range"]
    unc_lo       = round(current_total * (1 - unc_val / 100), 1)
    unc_hi       = round(current_total * (1 + unc_val / 100), 1)
    STAGE_RANK   = {"Concept / Schematic Design": 0, "Detailed Design": 2, "Tender": 3}
    current_rank = STAGE_RANK.get(stage, 0)

    # ── Helper: render a computed sensitivity row ─────────────────────────────
    def _val_cells(result_dict, stage_rank_needed, locked):
        if locked or result_dict is None:
            return '<td colspan="3" style="text-align:center;color:#94a3b8;font-size:0.8rem;">locked — not applicable at this stage</td>'
        delta = result_dict.get('delta', 0)
        if delta is None or delta <= 0:
            return '<td colspan="3" style="text-align:center;color:#94a3b8;font-size:0.8rem;">calculating… (run sensitivity analysis)</td>'
        sqm = round(delta * 1000 / project_area, 1) if project_area > 0 else 0
        pct = round(delta / current_total * 100, 1) if current_total > 0 else 0
        bar = min(100, round(pct * 4))
        return (f'<td style="text-align:right;color:#16a34a;font-weight:700;padding:8px 10px;">{delta:.1f} tCO₂e</td>'
                f'<td style="text-align:right;color:#16a34a;padding:8px 10px;">{sqm} kg/m²</td>'
                f'<td style="text-align:right;padding:8px 10px;">'
                f'<div style="display:flex;align-items:center;gap:6px;justify-content:flex-end;">'
                f'<div style="background:#e2e8f0;height:10px;border-radius:3px;width:60px;">'
                f'<div style="background:#16a34a;height:10px;border-radius:3px;width:{bar}%;"></div></div>'
                f'<span style="color:#16a34a;font-weight:600;">{pct}%</span></div></td>')

    def _badge(locked, applicable_stage):
        if locked:
            return (f'<span style="background:#fee2e2;color:#dc2626;border-radius:4px;'
                    f'padding:1px 7px;font-size:0.72rem;margin-left:8px;">🔒 Requires {applicable_stage}</span>')
        return ('<span style="background:#dcfce7;color:#16a34a;border-radius:4px;'
                f'padding:1px 7px;font-size:0.72rem;margin-left:8px;">✓ Applicable now</span>')

    def _sens_row(key, label, note, applicable_stage_rank, applicable_stage_label, extra_note=""):
        locked = applicable_stage_rank > current_rank
        res    = sensitivity_results.get(key) if sensitivity_results else None
        n_rows = res.get('n_rows', 0) if isinstance(res, dict) else 0
        row_note = res.get('note', note) if isinstance(res, dict) else note
        row_style = "opacity:0.4;" if locked else ""
        n_badge = (f' <small style="color:#64748b;">({n_rows} rows affected)</small>'
                   if n_rows > 0 and not locked else "")
        return f"""
        <tr style="{row_style}border-bottom:1px solid #f1f5f9;">
          <td style="padding:10px 10px;">
            <strong>{label}</strong>{_badge(locked, applicable_stage_label)}{n_badge}
            <br><small style="color:#64748b;line-height:1.4;">{row_note}{(' — ' + extra_note) if extra_note else ''}</small>
          </td>
          {_val_cells(res if isinstance(res, dict) else None, applicable_stage_rank, locked)}
        </tr>"""

    # ── Pre-compute sensitivity values ──────────────────────────────────────────
    slab_res    = sensitivity_results.get('slab_thickness_30pct') if sensitivity_results else None
    ggbs50_res  = sensitivity_results.get('ggbs_50pct')           if sensitivity_results else None
    ggbs70_res  = sensitivity_results.get('ggbs_70pct')           if sensitivity_results else None
    is_pt_or_hc = any(kw in (structural_system or '').lower()
                      for kw in ['pt', 'post-tension', 'hollowcore', 'hollow core', 'precast'])
    pt_already  = any(kw in (structural_system or '').lower()
                      for kw in ['pt flat', 'pt slab', 'post-tension'])
    pt_delta    = round((current_per_sqm - 198) * project_area / 1000, 1) \
                  if project_area > 0 and not pt_already else 0
    epd_est     = round(current_total * 0.055, 1)
    epd_sqm     = round(epd_est * 1000 / project_area, 1) if project_area > 0 else 0

    REBAR_DEFS = [
        ('rebar_slab_15pct',  'Rebar reduction — Slabs (15%)',        'Layout optimisation, reduced grid, yield-line analysis.'),
        ('rebar_beam_15pct',  'Rebar reduction — Beams (15%)',         'Right-sizing, composite action, eliminating over-design.'),
        ('rebar_col_10pct',   'Rebar reduction — Columns (10%)',       'Higher-strength concrete, smaller section, shorter laps.'),
        ('rebar_found_10pct', 'Rebar reduction — Foundations (10%)',   'Optimised pile/cap layout, yield-line design for pad footings.'),
        ('rebar_wall_10pct',  'Rebar reduction — Walls (10%)',         'Rationalised shear wall layout, min. reinforcement in low-demand zones.'),
    ]

    # ── Row renderers ─────────────────────────────────────────────────────────
    def _res_cells(res):
        if res is None:
            return '<td colspan="3" style="text-align:center;color:#94a3b8;font-size:0.8rem;">calculating…</td>'
        delta = res.get('delta', 0) or 0
        if delta <= 0:
            return '<td colspan="3" style="text-align:center;color:#94a3b8;font-size:0.8rem;">no saving detected</td>'
        sqm = round(delta * 1000 / project_area, 1) if project_area > 0 else 0
        pct = round(delta / current_total * 100, 1) if current_total > 0 else 0
        bar = min(100, round(pct * 4))
        return (f'<td style="text-align:right;color:#16a34a;font-weight:700;padding:8px 10px;">{delta:.1f} tCO₂e</td>'
                f'<td style="text-align:right;color:#16a34a;padding:8px 10px;">{sqm} kg/m²</td>'
                f'<td style="padding:8px 10px;"><div style="display:flex;align-items:center;gap:6px;justify-content:flex-end;">'
                f'<div style="background:#e2e8f0;height:10px;border-radius:3px;width:60px;">'
                f'<div style="background:#16a34a;height:10px;border-radius:3px;width:{bar}%;"></div></div>'
                f'<span style="color:#16a34a;font-weight:600;">{pct}%</span></div></td>')

    def _active_row(label, note, res, n_rows=0):
        nb = f' <small style="color:#64748b;">({n_rows} rows)</small>' if n_rows > 0 else ''
        return (f'<tr style="border-bottom:1px solid #f1f5f9;">'
                f'<td style="padding:10px;"><strong>{label}</strong>'
                f'<span style="background:#dcfce7;color:#16a34a;border-radius:4px;padding:1px 7px;font-size:0.72rem;margin-left:8px;">✓ Active now</span>{nb}'
                f'<br><small style="color:#64748b;line-height:1.4;">{note}</small></td>'
                f'{_res_cells(res)}</tr>')

    def _locked_row(label, note, res):
        delta = (res or {}).get('delta', 0) or 0
        sqm_v = round(delta * 1000 / project_area, 1) if project_area > 0 and delta > 0 else 0
        pct_v = round(delta / current_total * 100, 1) if current_total > 0 and delta > 0 else 0
        val   = f'{delta:.1f} tCO₂e / {sqm_v} kg/m² / {pct_v}%' if delta > 0 else 'not computed'
        return (f'<div style="display:flex;justify-content:space-between;align-items:center;'
                f'padding:7px 0;border-bottom:1px solid #f1f5f9;font-size:0.83rem;color:#94a3b8;">'
                f'<div><span style="text-decoration:line-through;">{label}</span>'
                f'<br><small style="color:#cbd5e1;">{note}</small></div>'
                f'<span style="font-size:0.78rem;color:#94a3b8;white-space:nowrap;margin-left:12px;">{val}</span></div>')

    def _future_pill(label, note, detail):
        return (f'<div style="margin-top:12px;padding:10px 14px;background:#fef9ec;'
                f'border-left:3px solid #d97706;border-radius:6px;font-size:0.83rem;">'
                f'<strong style="color:#92400e;">⏳ Future — {label}</strong>: {note}'
                f'<br><small style="color:#b45309;">{detail}</small></div>')

    TH = ('<table style="width:100%;border-collapse:collapse;font-size:0.85rem;">'
          '<thead><tr style="background:#f1f5f9;font-size:0.8rem;">'
          '<th style="text-align:left;padding:8px 10px;">Intervention</th>'
          '<th style="text-align:right;padding:8px 10px;">Saved tCO₂e</th>'
          '<th style="text-align:right;padding:8px 10px;">Saved kg/m²</th>'
          '<th style="text-align:right;padding:8px 10px;">% Reduction</th>'
          '</tr></thead><tbody>')

    # =========================================================================
    if stage == "Concept / Schematic Design":
        # slab row
        if is_pt_or_hc:
            slab_html = (f'<tr style="border-bottom:1px solid #f1f5f9;">'
                         f'<td style="padding:10px;"><strong>Slab thickness optimisation</strong>'
                         f'<span style="background:#fef3c7;color:#b45309;border-radius:4px;padding:1px 7px;font-size:0.72rem;margin-left:8px;">'
                         f'⚠ N/A — {structural_system}</span>'
                         f'<br><small style="color:#64748b;">PT/hollowcore slabs are precast — depth is structurally fixed.</small>'
                         f'</td><td colspan="3" style="text-align:center;color:#94a3b8;font-size:0.8rem;">N/A for this system</td></tr>')
        else:
            slab_html = _active_row('Slab thickness optimisation (up to 30%)',
                'RC flat slabs only — e.g. 300mm → 210mm with optimised grid or post-tensioning.',
                slab_res, (slab_res or {}).get('n_rows', 0))

        g50 = _active_row('Concrete mix: 50% GGBS substitution',
            'Applied per concrete grade using BOQ row e_id swaps to 50% GGBS catalogue entries.',
            ggbs50_res, (ggbs50_res or {}).get('n_rows', 0))
        g70 = _active_row('Concrete mix: 70% GGBS (CEM III equivalent)',
            'Applied to each grade at maximum permitted GGBS; capped where grade/spec limits apply.',
            ggbs70_res, (ggbs70_res or {}).get('n_rows', 0))

        rs  = (sensitivity_results or {}).get('rebar_slab_15pct')
        rb  = _active_row('Rebar reduction — Slabs (15%)',
            'Layout optimisation, reduced grid spacing, yield-line analysis.',
            rs, (rs or {}).get('n_rows', 0)) if rs else ''

        if pt_already:
            pt_html = ('<tr style="border-bottom:1px solid #f1f5f9;">'
                       '<td style="padding:10px;"><strong>Switch structural system to PT Flat Slab</strong>'
                       '<span style="background:#dcfce7;color:#16a34a;border-radius:4px;padding:1px 7px;font-size:0.72rem;margin-left:8px;">✓ Already PT system</span>'
                       '<br><small style="color:#64748b;">Already using PT slab — benchmark comparison not applicable.</small>'
                       '</td><td colspan="3" style="text-align:center;color:#16a34a;font-size:0.8rem;">✓ Already using PT</td></tr>')
        else:
            sqm_d = round(current_per_sqm - 198, 1)
            pct_d = round(pt_delta / current_total * 100, 1) if current_total > 0 else 0
            pt_html = (f'<tr style="border-bottom:1px solid #f1f5f9;">'
                       f'<td style="padding:10px;"><strong>Switch structural system to PT Flat Slab</strong>'
                       f'<span style="background:#dcfce7;color:#16a34a;border-radius:4px;padding:1px 7px;font-size:0.72rem;margin-left:8px;">✓ Active now</span>'
                       f'<br><small style="color:#64748b;">Ireland office case study: RC Flat Slab 278 vs PT Flat Slab 198 kgCO₂e/m². Concept-stage decision only.</small>'
                       f'</td>'
                       f'<td style="text-align:right;color:#16a34a;font-weight:700;padding:8px 10px;">{pt_delta:.1f} tCO₂e</td>'
                       f'<td style="text-align:right;color:#16a34a;padding:8px 10px;">{sqm_d} kg/m²</td>'
                       f'<td style="text-align:right;color:#16a34a;font-weight:600;padding:8px 10px;">{pct_d}%</td></tr>')

        fp1 = _future_pill('Detailed Design', 'Rebar in beams, columns, walls and foundations remain open.',
                           'GGBS % can still be increased; rebar layouts refined at Detailed Design stage.')
        fp2 = _future_pill('Tender', f'Supplier EPDs can reduce EC by ~{epd_est:.1f} tCO₂e (~{epd_sqm} kg/m²).',
                           'At Tender, confirmed suppliers allow substitution of generic catalogue rates with verified EPD values.')
        stage_matrix_html = (
            '      <!-- ④ Scenario Matrix: Concept / Schematic Design -->'
            '      <div class="card" style="margin-bottom:18px;">'
            '        <h3 style="font-size:1rem;font-weight:700;margin:0 0 4px;">'
            '<i class="fas fa-sliders-h" style="color:#16a34a;"></i>&nbsp; Decarbonisation Matrix &mdash; '
            '<span style="color:#16a34a;">Concept / Schematic Design</span></h3>'
            '        <p style="font-size:0.82rem;color:#64748b;margin:0 0 4px;">'
            '<strong>This is where 70–90% of embodied carbon is locked in.</strong> '
            'All rows below are active — design choices are still open. Act now for maximum impact.</p>'
            '        <p style="font-size:0.78rem;color:#94a3b8;margin:0 0 12px;">'
            'Savings calculated on actual BOQ quantities. GGBS values use real IGBC catalogue rates.</p>'
            f'        {TH}{slab_html}{g50}{g70}{rb}{pt_html}</tbody></table>'
            f'        {fp1}{fp2}'
            '      </div>')

    # =========================================================================
    elif stage == "Detailed Design":
        g50 = _active_row('Concrete mix: 50% GGBS substitution',
            'Applied per grade using BOQ-driven catalogue substitution to 50% GGBS variants.',
            ggbs50_res, (ggbs50_res or {}).get('n_rows', 0))
        g70 = _active_row('Concrete mix: 70% GGBS (CEM III equivalent)',
            'Applied to highest allowed GGBS level by grade; confirm supplier and strength constraints.',
            ggbs70_res, (ggbs70_res or {}).get('n_rows', 0))
        rebar_html = ''
        for key, label, note in REBAR_DEFS:
            res = (sensitivity_results or {}).get(key)
            rebar_html += _active_row(label, note, res, (res or {}).get('n_rows', 0))

        lk_c  = _locked_row('Structural system switch to PT Flat Slab', 'Required Concept stage decision — now fixed.', {'delta': pt_delta} if pt_delta > 0 else None)
        lk_c += _locked_row('Slab thickness optimisation (30%)', 'Thickness decisions finalised at Concept/Schematic stage.', slab_res)

        fp = _future_pill('Tender', f'Supplier EPDs can reduce EC by ~{epd_est:.1f} tCO₂e (~{epd_sqm} kg/m²).',
                          'At Tender, confirmed suppliers allow verified EPD substitution (3–8% typical saving).')
        stage_matrix_html = (
            '      <!-- ④ Scenario Matrix: Detailed Design -->'
            '      <div class="card" style="margin-bottom:18px;">'
            '        <h3 style="font-size:1rem;font-weight:700;margin:0 0 4px;">'
            '<i class="fas fa-sliders-h" style="color:#d97706;"></i>&nbsp; Decarbonisation Matrix &mdash; '
            '<span style="color:#d97706;">Detailed Design</span></h3>'
            '        <p style="font-size:0.82rem;color:#64748b;margin:0 0 4px;">'
            'Structural system is locked. Focus on <strong>material specification, GGBS content and rebar optimisation</strong>. '
            'Max remaining saving: ~10–20%.</p>'
            '        <p style="font-size:0.78rem;color:#94a3b8;margin:0 0 8px;">'
            'Savings calculated on actual BOQ quantities using IGBC catalogue rates.</p>'
            '        <div style="font-size:0.82rem;font-weight:700;color:#16a34a;margin-bottom:6px;">✓ Active Interventions</div>'
            f'        {TH}{g50}{g70}{rebar_html}</tbody></table>'
            '        <div style="margin-top:14px;padding:12px 14px;background:#f8fafc;border-left:4px solid #94a3b8;border-radius:6px;">'
            '          <div style="font-size:0.82rem;font-weight:700;color:#64748b;margin-bottom:8px;">'
            '\U0001f512 Locked — Concept Stage Decisions (for reference)</div>'
            '          <p style="font-size:0.78rem;color:#94a3b8;margin:0 0 8px;">'
            'These interventions required a Concept stage decision and are no longer available. '
            'Deltas shown are the savings that were achievable.</p>'
            f'          {lk_c}'
            '        </div>'
            f'        {fp}'
            '      </div>')

    # =========================================================================
    else:  # Tender
        epd_pct_str = f"{round(epd_est / current_total * 100, 1)}%" if current_total > 0 else "3–8%"
        epd_block = (
            '<div style="background:#f0fdf4;border:2px solid #16a34a;border-radius:10px;padding:16px 20px;margin-bottom:14px;">'
            '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;"><div>'
            '<div style="font-size:1rem;font-weight:700;color:#15803d;">'
            'Replace generic catalogue rates with verified supplier EPDs'
            '<span style="background:#dcfce7;color:#16a34a;border-radius:4px;padding:1px 8px;font-size:0.72rem;margin-left:8px;">'
            '✓ Action now — supplier confirmed</span></div>'
            '<p style="font-size:0.84rem;color:#166534;margin:6px 0 4px;">'
            'Supplier EPDs (e.g. Celsa rebar: 0.49 vs generic 1.72 kgCO₂e/kg) significantly reduce reported embodied carbon. '
            'Your supply chain is now confirmed — this is the correct point to substitute generic rates.</p>'
            '<small style="color:#15803d;">How to: Step 5 (Factors &amp; Transport) → double-click any e_id → enter supplier EPD factor.</small>'
            f'</div><div style="text-align:right;min-width:130px;background:white;border-radius:8px;padding:10px 14px;">'
            f'<div style="font-size:1.3rem;font-weight:800;color:#16a34a;">~{epd_est:.1f} tCO₂e</div>'
            f'<div style="font-size:0.8rem;color:#64748b;">{epd_sqm} kg/m²</div>'
            f'<div style="font-size:0.75rem;color:#94a3b8;">{epd_pct_str} potential saving</div>'
            '</div></div></div>')

        transport_block = (
            '<div style="margin-bottom:14px;padding:12px 16px;background:#f0f9ff;border-left:4px solid #0ea5e9;border-radius:6px;font-size:0.84rem;">'
            '<strong style="color:#0369a1;">Transport distance optimisation</strong>'
            ' — Confirm actual supplier/contractor locations and update distances in Step 5.'
            '<br><small style="color:#0284c7;">Actual haul distances vs assumed averages can change A4 emissions by ±20–40%. '
            'Contact your supply chain for confirmed distances.</small></div>')

        lk_d = ''
        lk_d += _locked_row('GGBS 50% substitution', 'Detailed Design spec — fixed.', ggbs50_res)
        lk_d += _locked_row('GGBS 70% (CEM III)', 'Detailed Design spec — fixed.', ggbs70_res)
        for key, label, note in REBAR_DEFS:
            res = (sensitivity_results or {}).get(key)
            lk_d += _locked_row(label, 'Detailed Design decision — fixed.', res)

        lk_c  = _locked_row('Structural system switch to PT Flat Slab', 'Concept stage decision — fixed.', {'delta': pt_delta} if pt_delta > 0 else None)
        lk_c += _locked_row('Slab thickness optimisation (30%)', 'Concept/Schematic design decision — fixed.', slab_res)

        stage_matrix_html = (
            '      <!-- ④ Scenario Matrix: Tender -->'
            '      <div class="card" style="margin-bottom:18px;">'
            '        <h3 style="font-size:1rem;font-weight:700;margin:0 0 4px;">'
            '<i class="fas fa-sliders-h" style="color:#dc2626;"></i>&nbsp; Decarbonisation Matrix &mdash; '
            '<span style="color:#dc2626;">Tender</span></h3>'
            '        <p style="font-size:0.82rem;color:#64748b;margin:0 0 12px;">'
            'Structural and material decisions are fixed. Only <strong>procurement, EPD substitution and transport</strong> '
            'remain as active levers. Max remaining saving: ~5–10%.</p>'
            f'        {epd_block}'
            f'        {transport_block}'
            '        <div style="margin-top:14px;padding:12px 14px;background:#f8fafc;border-left:4px solid #94a3b8;border-radius:6px;">'
            '          <div style="font-size:0.82rem;font-weight:700;color:#64748b;margin-bottom:8px;">'
            '\U0001f512 Locked — Detailed Design Decisions</div>'
            '          <p style="font-size:0.78rem;color:#94a3b8;margin:0 0 8px;">'
            'Material specification was finalised at Detailed Design.</p>'
            f'          {lk_d}'
            '        </div>'
            '        <div style="margin-top:10px;padding:12px 14px;background:#f8fafc;border-left:4px solid #cbd5e1;border-radius:6px;">'
            '          <div style="font-size:0.82rem;font-weight:700;color:#94a3b8;margin-bottom:8px;">'
            '\U0001f512 Locked — Concept Stage Decisions</div>'
            f'          {lk_c}'
            '        </div>'
            '      </div>')


    # ── Assemble return HTML ──────────────────────────────────────────────────
    lock_notice = (f'<div style="background:#fef3c7;border-left:4px solid #d97706;border-radius:6px;'
                   f'padding:10px 14px;margin-top:10px;font-size:0.85rem;color:#92400e;">'
                   f'<strong>⚠ Note:</strong> {cfg["lock_msg"]}</div>'
                   if cfg.get("lock_msg") else "")

    # ── Top Decarbonisation Actions (quick bullet summary) ────────────────────
    _top_actions = []
    if stage == "Concept / Schematic Design":
        _top_actions = [
            ("Switch to PT Flat Slab", "Largest single lever — saves ~20% vs RC flat slab (Ireland benchmark)."),
            ("50–70% GGBS substitution", "Saves 35–54% of concrete A1-A3 emissions; confirm with supplier early."),
            ("Slab thickness optimisation", "Every 10mm reduction ≈ 3% concrete saving across all slabs."),
        ]
    elif stage == "Detailed Design":
        _top_actions = [
            ("Specify 50–70% GGBS in concrete", "Concrete is typically 60–70% of structural EC. GGBS spec must be locked now."),
            ("Rebar layout optimisation", "Yield-line / layout review can save 10–15% of rebar EC across all members."),
            ("Right-size structural members", "Eliminate over-design — check utilisation ratios on all key members."),
        ]
    else:  # Tender
        _top_actions = [
            ("Obtain verified supplier EPDs", "Celsa rebar EPD: 0.49 vs generic 1.72 kgCO₂e/kg — up to 70% lower."),
            ("Confirm actual transport distances", "A4 transport can be ±20–40% vs assumed average distances."),
            ("Request low-carbon concrete mix designs", "Ask concrete suppliers for mix designs with maximised SCM content."),
        ]
    suggestions_html = '<ul style="margin:0;padding-left:1.2em;font-size:0.88rem;color:#374151;">' + ''.join(
        f'<li style="margin-bottom:6px;"><strong>{a}</strong> — {d}</li>'
        for a, d in _top_actions
    ) + '</ul>'

    # ── System Comparison rows ────────────────────────────────────────────────
    BENCHMARKS = [
        ("RC Flat Slab",         278),
        ("Steel + Hollowcore",   258),
        ("In-Situ + Hollowcore", 236),
        ("PT Slab with Caps",    205),
        ("PT Flat Slab",         198),
    ]
    best_benchmark = min(b[1] for b in BENCHMARKS)
    comparison_rows = ''
    for run_name, run_data in (comparison_runs or {}).items():
        try:
            rv = float(run_data.get('carbon_per_sqm', 0) or 0)
            rt = float(run_data.get('total_carbon', 0) or 0)
        except (TypeError, ValueError):
            rv, rt = 0.0, 0.0
        rs = run_data.get('stage', '')
        bar_w = min(100, round(rv / bar_ref * 100)) if bar_ref > 0 else 0
        comparison_rows += (
            f'<tr style="border-bottom:1px solid #f1f5f9;">'
            f'<td style="padding:8px 10px;">{_he(str(run_name))}</td>'
            f'<td style="text-align:right;padding:8px 10px;">{rv:.0f}</td>'
            f'<td style="text-align:right;padding:8px 10px;">{rt:.1f}</td>'
            f'<td style="text-align:right;padding:8px 10px;">{_he(str(rs))}</td>'
            f'<td style="padding:8px 10px;"><div style="background:#e2e8f0;height:12px;border-radius:3px;">'
            f'<div style="background:#64748b;height:12px;border-radius:3px;width:{bar_w}%;"></div></div></td></tr>')
    bench_rows = ''
    for bname, bval in BENCHMARKS:
        bar_w = min(100, round(bval / bar_ref * 100)) if bar_ref > 0 else 0
        is_best = bval == best_benchmark
        style = 'font-weight:600;color:#16a34a;' if is_best else ''
        bench_rows += (
            f'<tr style="border-bottom:1px solid #f1f5f9;{style}">'
            f'<td style="padding:7px 10px;">{bname}{"  ★" if is_best else ""}</td>'
            f'<td style="text-align:right;padding:7px 10px;">{bval}</td>'
            f'<td style="text-align:right;padding:7px 10px;">—</td>'
            f'<td style="text-align:right;padding:7px 10px;">Benchmark</td>'
            f'<td style="padding:7px 10px;"><div style="background:#e2e8f0;height:12px;border-radius:3px;">'
            f'<div style="background:#94a3b8;height:12px;border-radius:3px;width:{bar_w}%;"></div></div></td></tr>')

    # ── Hotspot rows (top 3 member categories by emission) ───────────────────
    hotspot_rows = ''
    if member_hotspot:
        sorted_hs = sorted(member_hotspot.items(), key=lambda x: x[1], reverse=True)[:3]
        hs_max = sorted_hs[0][1] if sorted_hs else 1
        for rank, (cat, em) in enumerate(sorted_hs, 1):
            bar_w = min(100, round(em / hs_max * 100)) if hs_max > 0 else 0
            pct = round(em / current_total * 100, 1) if current_total > 0 else 0
            colors = ['#dc2626', '#d97706', '#2563eb']
            col = colors[rank - 1]
            hotspot_rows += (
                f'<tr style="border-bottom:1px solid #f1f5f9;">'
                f'<td style="padding:8px 10px;font-weight:600;">#{rank} {_he(str(cat))}</td>'
                f'<td style="text-align:right;padding:8px 10px;">{em:.1f}</td>'
                f'<td style="text-align:right;padding:8px 10px;">{pct}%</td>'
                f'<td style="padding:8px 10px;min-width:120px;">'
                f'<div style="background:#e2e8f0;height:12px;border-radius:3px;">'
                f'<div style="background:{col};height:12px;border-radius:3px;width:{bar_w}%;"></div></div></td></tr>')
    if not hotspot_rows:
        hotspot_rows = '<tr><td colspan="4" style="padding:12px;text-align:center;color:#94a3b8;font-size:0.85rem;">Run calculations to see hotspot breakdown.</td></tr>'

    return f"""
    <div class="tab-content" id="decarbonisation-tab">

      <!-- ⓿ Value Engineering — A1-A5 embodied carbon (Y) vs cost (X) by structural system -->
      <div class="card" style="margin-bottom:18px;">
        <h3 style="font-size:1rem;font-weight:700;margin:0 0 4px;color:#1e293b;">
          <i class="fas fa-lightbulb" style="color:#f59e0b;"></i>&nbsp; A1&ndash;A5 Embodied Carbon vs Cost &mdash; Like-for-Like Case Study
        </h3>
        <p style="font-size:0.82rem;color:#64748b;margin:0 0 12px;">
          <strong>Like-for-like comparison:</strong> the same building (5,062&nbsp;m² GIA) modelled with different structural systems.
          Transport distances, the A5a site-activity factor (28&nbsp;kgCO₂e/m²) and waste allowances are held <strong>identical</strong> across
          every option &mdash; concrete 100&nbsp;km road; steel &amp; post-tensioning 1,000&nbsp;km sea + 150&nbsp;km road &mdash; so the only
          variable is the structure itself. Each point is a system's computed <strong>A1&ndash;A5</strong> embodied carbon (vertical)
          against its indicative cost (horizontal); the thin line above a point is a small modelling allowance.
        </p>
        <div id="costVsEcChart" style="width:100%;height:420px;margin-top:4px;"></div>
        <p style="font-size:0.74rem;color:#94a3b8;margin:8px 0 0;">
          Vertical = A1&ndash;A5 carbon (kgCO₂e/m²); horizontal = cost (€/m²). Lower-left = cheaper and lower-carbon.
        </p>
      </div>

      <!-- ① Stage Awareness Banner -->
      <div style="background:linear-gradient(135deg,{cfg['color']}12,{cfg['color']}04);
                  border-left:5px solid {cfg['color']};border-radius:10px;
                  padding:18px 22px;margin-bottom:18px;">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;">
          <span style="font-size:1.9rem;">{cfg['icon']}</span>
          <div>
            <div style="font-size:1.05rem;font-weight:700;color:{cfg['color']};">
              {stage} &nbsp;·&nbsp; Structural System: {structural_system or 'Not specified'}
            </div>
            <div style="font-size:0.8rem;color:#64748b;">Estimate accuracy: {unc_label}</div>
          </div>
        </div>
        <p style="margin:0 0 6px;font-size:0.9rem;color:#1e293b;">{cfg['msg']}</p>
        <p style="margin:0;font-size:0.82rem;color:#64748b;">
          Once the structural system is chosen at Concept, <strong>70–90%</strong> of the embodied carbon footprint is locked in.
          Tender and Construction can only influence the remaining <strong>5–10%</strong> (EPD substitution, transport, waste).
        </p>
        {lock_notice}
        <div style="margin-top:14px;padding:12px 14px;background:white;border-radius:8px;">
          <div style="font-size:0.8rem;color:#64748b;margin-bottom:6px;">
            Estimate range at <strong>{stage}</strong>:
            <strong style="color:{unc_color};">{unc_lo:.0f} – {unc_hi:.0f} tCO₂e</strong>
            &nbsp;(central value: {current_total:.1f} tCO₂e &nbsp;|&nbsp; {current_per_sqm:.0f} kgCO₂e/m²)
          </div>
          <div style="position:relative;background:#e2e8f0;height:20px;border-radius:6px;">
            <div style="position:absolute;left:{max(0, 50 - unc_val//2)}%;width:{min(100, unc_val)}%;background:{unc_color}40;height:20px;border-radius:6px;"></div>
            <div style="position:absolute;left:50%;transform:translateX(-50%);width:3px;height:20px;background:{unc_color};border-radius:2px;"></div>
            <div style="position:absolute;left:{max(0, 50 - unc_val//2)}%;top:50%;transform:translateY(-50%);font-size:0.7rem;color:{unc_color};padding-left:4px;white-space:nowrap;">{unc_lo:.0f}</div>
            <div style="position:absolute;right:{max(0, 50 - unc_val//2)}%;top:50%;transform:translateY(-50%);font-size:0.7rem;color:{unc_color};padding-right:4px;white-space:nowrap;">{unc_hi:.0f}</div>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:0.72rem;color:#94a3b8;margin-top:3px;">
            <span>Low estimate</span>
            <span style="color:{unc_color};font-weight:600;">{unc_label} accuracy at {stage}</span>
            <span>High estimate</span>
          </div>
        </div>
      </div>

      <!-- ② Top Decarbonisation Actions -->
      <div class="card" style="margin-bottom:18px;">
        <h3 style="font-size:1rem;font-weight:700;margin:0 0 10px;color:#1e293b;">
          <i class="fas fa-star" style="color:#f59e0b;"></i>&nbsp; Top Decarbonisation Actions
        </h3>
        {suggestions_html}
      </div>

      <!-- ③ System Comparison -->
      <div class="card" style="margin-bottom:18px;">
        <h3 style="font-size:1rem;font-weight:700;margin:0 0 4px;">
          <i class="fas fa-balance-scale" style="color:#2563eb;"></i>&nbsp; System Comparison
        </h3>
        <p style="font-size:0.82rem;color:#64748b;margin:0 0 12px;">
          Ireland office case study benchmarks (5,062 m² GIA) with your saved runs.
          Use <strong>Save Run for Comparison</strong> after each calculation to build this table.
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:0.85rem;">
          <thead>
            <tr style="background:#f1f5f9;font-size:0.8rem;">
              <th style="text-align:left;padding:8px 10px;">System / Run</th>
              <th style="text-align:right;padding:8px 10px;">kgCO₂e/m²</th>
              <th style="text-align:right;padding:8px 10px;">Total tCO₂e</th>
              <th style="text-align:right;padding:8px 10px;">Stage</th>
              <th style="padding:8px 10px;min-width:100px;">Relative</th>
            </tr>
          </thead>
          <tbody>
            <tr style="background:#eff6ff;font-weight:600;border-bottom:2px solid #2563eb;">
              <td style="padding:9px 10px;">&#9658; {structural_system or 'This Run'} <small style="color:#64748b;font-weight:400;">(current)</small></td>
              <td style="text-align:right;padding:9px 10px;">{current_per_sqm:.0f}</td>
              <td style="text-align:right;padding:9px 10px;">{current_total:.1f}</td>
              <td style="text-align:right;padding:9px 10px;">{stage}</td>
              <td style="padding:9px 10px;">
                <div style="background:#e2e8f0;height:12px;border-radius:3px;">
                  <div style="background:#2563eb;height:12px;border-radius:3px;width:{min(100, round(current_per_sqm/bar_ref*100))}%;"></div>
                </div>
              </td>
            </tr>
            {comparison_rows}
            <tr><td colspan="5" style="padding:6px 10px 3px;font-size:0.72rem;color:#94a3b8;font-weight:700;letter-spacing:0.04em;background:#f8fafc;">
                IRELAND OFFICE CASE STUDY BENCHMARKS
            </td></tr>
            {bench_rows}
          </tbody>
        </table>
      </div>

      <!-- ④ Stage-Classified Decarbonisation Matrix -->
      {stage_matrix_html}

      <!-- ⑤ Carbon Hotspot — Top 3 -->
      <div class="card" style="margin-bottom:18px;">
        <h3 style="font-size:1rem;font-weight:700;margin:0 0 4px;">
          <i class="fas fa-fire" style="color:#dc2626;"></i>&nbsp; Carbon Hotspot — Top 3 Industry Targets
        </h3>
        <p style="font-size:0.82rem;color:#64748b;margin:0 0 12px;">
          Ranked by combined total emission (A1-A3 + A4 + A5). The top 3 members account for the majority
          of structural embodied carbon and are the standard industry optimisation targets.
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:0.85rem;">
          <thead>
            <tr style="background:#f1f5f9;font-size:0.8rem;">
              <th style="text-align:left;padding:8px 10px;">Member Type</th>
              <th style="text-align:right;padding:8px 10px;">Total tCO₂e</th>
              <th style="text-align:right;padding:8px 10px;">% of Total</th>
              <th style="text-align:right;padding:8px 10px;">kgCO₂e/m²</th>
              <th style="padding:8px 10px;">Risk</th>
              <th style="text-align:left;padding:8px 10px;">Recommended Action</th>
            </tr>
          </thead>
          <tbody>{hotspot_rows}</tbody>
        </table>
      </div>

      <!-- ⑥ Data Intelligence -->
      <div class="card" style="margin-bottom:18px;background:#fffbeb;border-left:4px solid #d97706;">
        <h3 style="font-size:0.95rem;font-weight:700;margin:0 0 10px;color:#92400e;">
          <i class="fas fa-database"></i>&nbsp; Data Intelligence &amp; Audit Trail
        </h3>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;font-size:0.83rem;">
          <div>
            <div style="font-weight:700;margin-bottom:6px;color:#1e293b;">Core Results</div>
            <ul style="margin:0;padding-left:16px;color:#475569;line-height:1.7;">
              <li>Total: <strong>{current_total:.1f} tCO₂e</strong></li>
              <li>Intensity: <strong>{current_per_sqm:.0f} kgCO₂e/m²</strong></li>
              <li>{len(member_hotspot)} member types computed</li>
              <li>{len(sensitivity_results or {})} sensitivity runs completed</li>
            </ul>
          </div>
          <div>
            <div style="font-weight:700;margin-bottom:6px;color:#1e293b;">Accuracy &amp; Stage</div>
            <ul style="margin:0;padding-left:16px;color:#475569;line-height:1.7;">
              <li>Stage: <strong>{stage}</strong></li>
              <li>Accuracy band: <strong>{unc_label}</strong></li>
              <li>System: {structural_system or 'Not specified'}</li>
              <li>Saved comparisons: {len(comparison_runs)}</li>
            </ul>
          </div>
          <div>
            <div style="font-weight:700;margin-bottom:6px;color:#1e293b;">EPD Readiness</div>
            <ul style="margin:0;padding-left:16px;color:#475569;line-height:1.7;">
              <li>Current: generic IGBC catalogue</li>
              <li>At Tender: replace with EPD e_ids</li>
              <li>Potential saving: 3–8%</li>
              <li style="color:#d97706;font-weight:600;">⚠ EPD critical at Tender stage</li>
            </ul>
          </div>
        </div>
      </div>

    </div>
"""


def _get_alert_banners(project_info, dashboard_insights):
    """Always-visible warnings above the tabs — zero-blackboxing: the report
    says loudly when totals are understated or below stage expectations."""
    banners = ''

    # 1. Elements with no emission factor (contribute 0 — totals understated)
    nf = project_info.get('no_factor_summary') or {}
    if nf.get('count'):
        cats = ', '.join(_he(str(c)) for c in (nf.get('categories') or [])[:8])
        vol = nf.get('volume_m3', 0)
        banners += f"""
        <div class="zero-factor-banner" style="display:flex;gap:10px;align-items:flex-start;
             background:#fef2f2;border:1px solid #fecaca;border-left:4px solid #dc2626;
             border-radius:8px;padding:10px 14px;margin-bottom:8px;font-size:0.85rem;color:#7f1d1d;">
            <i class="fas fa-triangle-exclamation" style="color:#dc2626;margin-top:2px;"></i>
            <div><strong>{nf['count']} element(s) are not included in the carbon totals</strong>
            (&asymp;{vol:,.1f} m³{'; ' + cats if cats else ''}).
            The carbon database does not yet cover these materials (for example timber, CLT or
            connections), so they are shown in the model but counted as zero — the true total
            for this project is <strong>higher than reported</strong>. Speak to your assessor
            about adding supplier data for these materials.</div>
        </div>"""

    # 2. Stage expectation: Tender runs should be EPD-backed
    if project_info.get('epd_expected'):
        cov = _to_float((dashboard_insights or {}).get('epd_coverage_pct', 0))
        if cov < 50:
            banners += f"""
        <div class="epd-stage-banner" style="display:flex;gap:10px;align-items:flex-start;
             background:#fffbeb;border:1px solid #fde68a;border-left:4px solid #d97706;
             border-radius:8px;padding:10px 14px;margin-bottom:8px;font-size:0.85rem;color:#78350f;">
            <i class="fas fa-file-signature" style="color:#d97706;margin-top:2px;"></i>
            <div><strong>This is a tender-stage assessment, but most factors are still industry
            averages:</strong> only {cov:g}% of the material mass is backed by supplier
            (EPD) data. At tender stage suppliers are usually confirmed — adding their EPD
            values in the Emission Factors step will make this assessment tender-grade.</div>
        </div>"""

    # 3. Biogenic carbon stored in timber (positive news — a separate indicator)
    seq = _to_float((dashboard_insights or {}).get('sequestration_ton', 0))
    if seq < 0:
        banners += f"""
        <div class="biogenic-banner" style="display:flex;gap:10px;align-items:flex-start;
             background:#f0fdf4;border:1px solid #bbf7d0;border-left:4px solid #16a34a;
             border-radius:8px;padding:10px 14px;margin-bottom:8px;font-size:0.85rem;color:#14532d;">
            <i class="fas fa-leaf" style="color:#16a34a;margin-top:2px;"></i>
            <div><strong>Biogenic carbon stored: {seq:,.1f} tCO₂e</strong> in the timber
            elements. This is the carbon sequestered in the wood while in use — reported
            <strong>separately</strong> and <strong>not</strong> subtracted from the A1–A5
            total above (it is released at end of life, module C). Shown per EN 16485.</div>
        </div>"""

    if not banners:
        return ''
    return f'<div style="margin:10px 0 4px 0;">{banners}</div>'


def _get_methodology_appendix(data):
    """Full methodology & assumptions appendix rendered with THIS run's actual
    numbers — every headline figure on the dashboard is reproducible from it."""
    from calculations import (A5A_EMISSION_FACTOR_KGCO2E_PER_SQM as _A5A_DEFAULT,
                              SEAI_CF_OUTWARD_ROAD, SEAI_CF_RETURN_ROAD, SEAI_CF_SEA)
    from catalogue import SEAI_TRANSPORT_TABLE as _SEAI_TT
    pi = data.project_info or {}
    dist = data.distances or {}
    metrics = data.metrics or {}
    area = data.project_area or 0
    # A5a factor actually used this run (user override or the SEAI default).
    _A5A = float(metrics.get('a5a_factor', _A5A_DEFAULT) or _A5A_DEFAULT)
    _a5a_is_override = abs(_A5A - _A5A_DEFAULT) > 1e-6
    a5w = getattr(data, 'a5w_waste_pcts', None) or {
        'Concrete': 0.05, 'Rebar': 0.05, 'Steel Section': 0.01, 'Post Tensioning': 0.015}
    cat_ver = getattr(data, 'catalogue_version', '') or pi.get('catalogue_version', '')
    stage_badge = pi.get('stage_badge', '')
    stage_desc = pi.get('stage_description', '')
    unc = pi.get('uncertainty_pct', 0) or metrics.get('uncertainty_pct', 0)
    expectations = pi.get('stage_expectations') or []
    exp_html = ''.join(f'<li>{_he(str(e))}</li>' for e in expectations)
    waste_rows = ''.join(
        f'<tr><td style="padding:3px 10px;">{_he(k)}</td>'
        f'<td style="padding:3px 10px;text-align:right;">{v * 100:g}%</td></tr>'
        for k, v in a5w.items())
    a5a_total = _A5A * area / 1000.0
    n_sens = len(getattr(data, 'sensitivity_results', {}) or {})

    return f"""
    <details id="methodology-appendix" class="card" style="margin-top:14px;padding:14px 18px;">
      <summary style="cursor:pointer;font-weight:700;color:var(--primary);font-size:0.95rem;">
        <i class="fas fa-book-open"></i> Methodology &amp; Assumptions — how every number on this
        report is calculated (click to expand)
      </summary>
      <div style="font-size:0.84rem;color:#334155;line-height:1.55;margin-top:12px;">

        <p><strong>Scope.</strong> Upfront embodied carbon <strong>A1&ndash;A5</strong>
        (EN&nbsp;15978 subset), structural elements only. Life-cycle modules B/C/D are not
        included. Emission factors are compiled from Irish industry EPDs (IGBC), the ICE
        database and manufacturer EPDs (CARES scheme); a versioned change log of the dataset
        is maintained with the tool{f' (dataset {_he(str(cat_ver))})' if cat_ver else ''}.</p>

        <p><strong>Design stage &amp; confidence.</strong>
        <strong>{_he(str(pi.get('project_stage', '')))} &rarr; {_he(str(stage_badge))}</strong>,
        display band &plusmn;{unc:g}%. {_he(str(stage_desc))}</p>
        <ul style="margin:4px 0 10px 18px;">{exp_html}</ul>

        <p><strong>A1&ndash;A3 (product).</strong> emission = factor (kgCO2e/kg, from the
        catalogue or your Step-5 EPD override) &times; mass (kg). Concrete mass = volume &times;
        2400 kg/m³; steel/PT mass = supplied mass or volume &times; 7850 kg/m³.</p>

        <p><strong>A4 (transport, SEAI methodology).</strong> emission = mass &times; sea_km &times;
        {SEAI_CF_SEA:g} &nbsp;+&nbsp; mass &times; road_km &times; ({SEAI_CF_OUTWARD_ROAD:g} outward
        <strong>+</strong> {SEAI_CF_RETURN_ROAD:g} return) &mdash; road is outward (average laden)
        <em>plus</em> a separate empty-return leg (0% laden, SEAI-derived); sea is single-leg (no return).
        Distances used in this run (km): {'; '.join(
            f"{_meta['label']} {dist.get(f'{_fam}_sea_distance', _meta['sea']):g} sea / "
            f"{dist.get(f'{_fam}_road_distance', _meta['road']):g} road"
            for _fam, _meta in _SEAI_TT.items())}.</p>

        <p><strong>A5w (material waste, SEAI A5.3).</strong> emission = waste% &times; (A1&ndash;A3 + A4)
        &mdash; the wasted material still carried its production <em>and</em> its transport to site before being
        discarded (end-of-life C2/C4 excluded, as this tool is A1&ndash;A5 only).
        Waste percentages used in this run:</p>
        <table style="border-collapse:collapse;background:#f8fafc;border-radius:6px;margin:4px 0 10px 0;">
          <thead><tr><th style="padding:3px 10px;text-align:left;">Material</th>
          <th style="padding:3px 10px;text-align:right;">A5w %</th></tr></thead>
          <tbody>{waste_rows}</tbody>
        </table>

        <p><strong>A5a (construction / site activities).</strong>
        {_A5A:g} kgCO2e/m² &times; {area:,.0f} m² GIA = <strong>{a5a_total:,.1f} tCO2e</strong>,
        allocated across element rows in proportion to mass (so per-element A5 =
        A5w + A5a share; material totals include their A5a share).
        {'<b style="color:#b45309;">Site-activity override applied</b> (from site data / experience) &mdash; the SEAI default is 28 kgCO2e/m².' if _a5a_is_override else 'SEAI default (28 kgCO2e/m², 70% of the RICS 40 whole-building figure).'}</p>

        <p><strong>Rating.</strong> SCORS structural bands (IStructE): A++ &le;50 &hellip;
        G &gt;400 kgCO2e/m², applied to the A1&ndash;A5 per-m² intensity.</p>

        <p><strong>Decarbonisation scenarios.</strong> {n_sens} sensitivity run(s) were computed
        by re-running this exact BOQ through the same engine with targeted changes (e.g. GGBS
        e_id swaps per concrete grade within IS&nbsp;EN&nbsp;206 limits, rebar mass reductions
        by member type) — deltas are calculated, never fixed multipliers.</p>

      </div>
    </details>
"""


def _get_methodology_bar(metrics=None):
    """Footer strip. The A5a entry must quote the factor THIS run used — quoting
    the SEAI default here would contradict the appendix directly above it."""
    from calculations import A5A_EMISSION_FACTOR_KGCO2E_PER_SQM as _A5A_DEFAULT
    _a5a = float((metrics or {}).get('a5a_factor', _A5A_DEFAULT) or _A5A_DEFAULT)
    _src = ('70% structural scope' if abs(_a5a - _A5A_DEFAULT) <= 1e-6
            else 'user override')
    _lbl = 'SEAI ' if abs(_a5a - _A5A_DEFAULT) <= 1e-6 else ''
    return f"""
    <div class="methodology-bar">
        <span><i class="fas fa-database"></i> Data: IGBC</span>
        <span><i class="fas fa-cogs"></i> Scope: A1-A5 upfront embodied carbon (EN 15978 subset)</span>
        <span><i class="fas fa-building"></i> A5a: {_lbl}{_a5a:g} kgCO2e/m² GIA ({_src})</span>
        <span><i class="fas fa-calendar"></i> Generated: {datetime.now().strftime('%Y-%m-%d')}</span>
    </div>
"""


def _get_javascript(model_type, detailed_data, project_area, metrics,
                    material_types, material_emissions, stage_emissions,
                    concrete_emissions, rebar_emissions, structural_steel_emissions, steel_emissions, pt_emissions,
                    summary_df, total_emission, material_based, categories, distances,
                    geometry_json=None, is_eaf_steel=False,
                    timber_emissions=None,
                    stage='Concept / Schematic Design', sensitivity_results=None,
                    dashboard_insights=None):
    timber_emissions = timber_emissions or [0, 0, 0]

    geom_json_str = _safe_json_geometry(geometry_json or [])

    return f"""
<script>
const detailedData = {_safe_json(detailed_data)};
const projectArea = {project_area};
const totalEmission = {round(total_emission, 3)};
const materialTypes = {_safe_json(material_types)};
const materialEmissions = {_safe_json(material_emissions)};
const stageEmissions = {_safe_json(stage_emissions)};
const concreteEmissions = {_safe_json(concrete_emissions)};
const rebarEmissions = {_safe_json(rebar_emissions)};
const structuralSteelEmissions = {_safe_json(structural_steel_emissions)};
const steelEmissions = {_safe_json(steel_emissions)};
const ptEmissions = {_safe_json(pt_emissions)};
const timberEmissions = {_safe_json(timber_emissions)};
const metricsData = {_safe_json(metrics)};
const categories = {_safe_json(categories)};
const materialBased = {_safe_json(material_based)};
const distances = {_safe_json(distances)};
const isEafSteel = {'true' if is_eaf_steel else 'false'};
const geometryData2 = {geom_json_str};

// SECURITY: every string that originated in the uploaded model/BOQ (names,
// descriptions, categories, families, levels) must pass through escH() before
// being interpolated into innerHTML — a crafted element name must render as
// text, never as markup.
function escH(s) {{ return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }}
const currentStage = {_safe_json(stage)};
const sensitivityResults = {_safe_json(sensitivity_results or {})};
const dashboardInsights = {_safe_json(dashboard_insights or {})};

// ── Data-driven emission intensity thresholds ──────────────────────────────
// Computed from actual model data using 33rd / 67th percentile of non-zero totals
const _ecVals = detailedData.map(function(d) {{ return d.total||0; }}).filter(function(v) {{ return v > 0; }}).sort(function(a,b) {{ return a-b; }});
function _pct(arr, p) {{ if (!arr.length) return 0; var i = Math.max(0, Math.floor(arr.length * p / 100) - 1); return arr[i]; }}
const EC_LOW  = _pct(_ecVals, 33);   // below = Low
const EC_HIGH = _pct(_ecVals, 67);   // above = High
// Fallback if model has very few elements
const EC_LOW_LABEL  = EC_LOW  > 0 ? EC_LOW.toFixed(3)  : '0.1';
const EC_HIGH_LABEL = EC_HIGH > 0 ? EC_HIGH.toFixed(3) : '0.5';

const colors = {{
    primary: '#1e3a5f',
    accent: '#16a34a',
    blue: '#3498db',
    warning: '#e67e22',
    danger: '#dc2626',
    teal: '#0f4c5c',
    tealLight: '#1a7a8a',
    concrete: '#3498db',
    steel: '#ef4444',
    pt: '#22c55e',
    a1a3: '#1e3a5f',
    a4: '#3498db',
    a5: '#16a34a'
}};

const materialColorMap = {{
    'Concrete': colors.concrete,
    'Rebar': colors.steel,
    'Steel Section': '#b91c1c',
    'Post Tensioning': colors.pt,
    'Timber': '#16a34a',
}};

// ==========================================
// SIDEBAR PANEL FUNCTIONS
// ==========================================
function openPanel(panelId) {{
    closeAllPanels();
    document.getElementById(panelId).classList.add('open');
    document.getElementById('panelOverlay').classList.add('open');
    if (panelId === 'detailedPanel') populateDetailedPanel();
}}

function closeAllPanels() {{
    document.querySelectorAll('.slide-panel').forEach(function(p) {{ p.classList.remove('open'); }});
    document.getElementById('panelOverlay').classList.remove('open');
}}

function goToVETab() {{
    var btns = document.querySelectorAll('.tab-button');
    if (btns[1]) btns[1].click();
}}

function getDisplayCategory(d) {{
    // Infer likely non-structural type from element name/description keywords
    var name = ((d.description||'') + ' ' + (d.category||'')).toLowerCase();
    if (/glaz|curtain|cladding|facade|panel|window|glazed/.test(name)) return 'Glazing/Curtain Wall';
    if (/door/.test(name)) return 'Door';
    if (/railing|balustrade|handrail/.test(name)) return 'Railing';
    if (/ceiling|soffit/.test(name)) return 'Ceiling';
    if (/floor.finish|screed|topping/.test(name)) return 'Floor Finish';
    if (/insul/.test(name)) return 'Insulation';
    if (/parapet/.test(name)) return 'Parapet';
    return d.category || 'Other';
}}

function populateDetailedPanel() {{
    var tbody = document.getElementById('detailedPanelBody');
    if (tbody.innerHTML.length > 10) return;

    function baseMemberName(d) {{
        var desc = String(d.description || '').trim();
        if (desc.indexOf(' - ') >= 0) return desc.split(' - ')[0].trim();
        if (desc) return desc;
        return String(d.category || d.material || 'Unknown');
    }}

    function materialRank(d) {{
        var m = String(d.material || '').toLowerCase();
        if (m === 'concrete') return 0;
        if (m === 'rebar') return 1;
        if (m === 'steel section') return 2;
        if (m === 'post tensioning') return 3;
        return 4;
    }}

    function sortedByMemberFlow(data) {{
        var groups = {{}};
        data.forEach(function(d) {{
            var key = baseMemberName(d);
            if (!groups[key]) groups[key] = [];
            groups[key].push(d);
        }});

        var groupList = Object.keys(groups).map(function(k) {{
            var rows = groups[k];
            var concrete = rows.find(function(r) {{ return String(r.material || '') === 'Concrete'; }});
            var keyTotal = concrete ? Number(concrete.total || 0) : Math.max.apply(null, rows.map(function(r) {{ return Number(r.total || 0); }}));
            return {{ key: k, rows: rows, keyTotal: keyTotal }};
        }});

        groupList.sort(function(a, b) {{
            if (b.keyTotal !== a.keyTotal) return b.keyTotal - a.keyTotal;
            return a.key.localeCompare(b.key);
        }});

        var ordered = [];
        groupList.forEach(function(g) {{
            g.rows.sort(function(a, b) {{
                var ra = materialRank(a), rb = materialRank(b);
                if (ra !== rb) return ra - rb;
                return (Number(b.total || 0) - Number(a.total || 0));
            }});
            ordered = ordered.concat(g.rows);
        }});
        return ordered;
    }}

    function familyTypeLabel(d) {{
        var fam = String(d.family || '').trim();
        var typ = String(d.ifc_type || '').trim();
        if (!fam && !typ) return '-';
        return (fam || '-') + ' / ' + (typ || '-');
    }}

    var sorted = sortedByMemberFlow(detailedData);
    tbody.innerHTML = sorted.map(function(d) {{
        var totalVal = d.total||0;
        var zeroStyle = totalVal === 0 ? 'color:#94a3b8;font-style:italic;' : 'font-weight:600;';
        var totalDisp = totalVal === 0 ? 'No EC' : totalVal.toFixed(3);
        var dispCat = getDisplayCategory(d);
        var matCell = escH(d.material||'') + ((d.description||'') ? ('<div style="font-size:0.68rem;color:#64748b;">' + escH(d.description) + '</div>') : '');
        return '<tr><td>'+escH(d.e_id||'')+'</td><td>'+matCell+'</td><td>'+escH(dispCat)+'</td><td>'+escH(familyTypeLabel(d))+'</td><td style="text-align:right;">'+(d.mass||0).toLocaleString()+'</td><td style="text-align:right;">'+(d.a1_a3||0).toFixed(3)+'</td><td style="text-align:right;">'+(d.a4||0).toFixed(3)+'</td><td style="text-align:right;">'+(d.a5||0).toFixed(3)+'</td><td style="text-align:right;'+zeroStyle+'">'+totalDisp+'</td></tr>';
    }}).join('');
}}

// ==========================================
// TAB SWITCHING
// ==========================================
function openTab(evt, tabName) {{
    var tabs = document.getElementsByClassName("tab-content");
    for (var i = 0; i < tabs.length; i++) {{ tabs[i].style.display = "none"; tabs[i].classList.remove("active"); }}
    var btns = document.getElementsByClassName("tab-button");
    for (var i = 0; i < btns.length; i++) {{ btns[i].classList.remove("active"); }}
    var activeTab = document.getElementById(tabName);
    if (activeTab) {{ activeTab.style.display = "block"; activeTab.classList.add("active"); }}
    evt.currentTarget.classList.add("active");
    // Init charts first (works even at zero width), then resize once tab is visible
    setTimeout(function() {{
        initChartsForTab(tabName);
        setTimeout(function() {{
            window.dispatchEvent(new Event('resize'));
            document.querySelectorAll('[id$="Chart"]').forEach(function(el) {{
                var inst = echarts.getInstanceByDom(el);
                if (inst) inst.resize();
            }});
        }}, 150);
    }}, 50);
}}

// Collapsible sections
function toggleCollapse(el) {{
    el.classList.toggle('open');
    var body = el.nextElementSibling;
    body.classList.toggle('open');
}}

var chartsInitialized = {{}};
var filteredDataCache = [...detailedData];
var radarChartInstance = null;

// Safe echarts init — works even when element is hidden (width=0)
function _echartsInit(id) {{
    var el = document.getElementById(id);
    if (!el) return null;
    var existing = echarts.getInstanceByDom(el);
    if (existing) return existing;
    return echarts.init(el);
}}

function initChartsForTab(tabName) {{
    if (chartsInitialized[tabName]) {{
        // Tab already initialised — just resize all charts
        document.querySelectorAll('[id$="Chart"]').forEach(function(el) {{
            var inst = echarts.getInstanceByDom(el);
            if (inst) inst.resize();
        }});
        return;
    }}
    chartsInitialized[tabName] = true;
    if (tabName === 'analysis-tab') initAnalysisCharts();
    else if (tabName === 'decarbonisation-tab') initDecarbonisationCharts();
}}

// Decarbonization & Value Engineering tab — A1-A5 embodied carbon (Y) vs cost (X)
function initDecarbonisationCharts() {{
    var el = document.getElementById('costVsEcChart');
    if (!el) return;
    var chart = _echartsInit('costVsEcChart');
    // [costMid, ecExact, ecPlus5, costLo, costHi, name, color, labelDx, labelDy, align]
    // A1-A5 kgCO2e/m2, like-for-like case study (same GIA 5,062 m2, same transport,
    // A5a & waste). ecExact = computed value (the dot); ecPlus5 = a small allowance
    // shown only as a faint, unlabelled line above the dot. Each system is labelled
    // in-chart with an arrow to its dot; labelDx/Dy (px) fan the labels clear of the
    // lower-left cluster.
    var systems = [
        [181,   278, 292, 177, 185, 'RC Flat Slab',             '#e74c3c',  30,  -2, 'left'],
        [300,   258, 271, 280, 320, 'Steel + Hollowcore',       '#c0392b', -34, -12, 'right'],
        [195,   236, 248, 190, 200, 'In Situ Hollowcore',       '#f39c12',  40, -14, 'left'],
        [161,   233, 245, 156, 166, 'PT Band Beam',             '#27ae60', -20, -30, 'right'],
        [168,   227, 239, 163, 173, 'PT Flat Slab w/ Col Caps', '#8e44ad', -46,   2, 'right'],
        [170.5, 223, 234, 166, 175, 'PT Flat Slab',             '#3498db',  22,  30, 'left']
    ];
    chart.setOption({{
        tooltip: {{
            trigger: 'item',
            formatter: function(p) {{
                var v = p.value;
                return '<b>' + v[5] + '</b><br>A1&ndash;A5: <b>' + v[1] + '</b> kgCO₂e/m²<br>Cost: €' + v[3] + '–' + v[4] + ' /m²';
            }}
        }},
        grid: {{ left: 68, right: 26, top: 18, bottom: 46 }},
        xAxis: {{ type: 'value', name: 'Cost (€/m²)', nameLocation: 'middle', nameGap: 28, min: 138, max: 342,
                  axisLabel: {{ formatter: '€{{value}}' }}, splitLine: {{ lineStyle: {{ color: '#eef2f6' }} }} }},
        yAxis: {{ type: 'value', name: 'A1–A5 (kgCO₂e/m²)', nameLocation: 'middle', nameGap: 46, min: 208, max: 300,
                  splitLine: {{ lineStyle: {{ color: '#eef2f6' }} }} }},
        series: [{{
            type: 'custom',
            data: systems,
            clip: false,
            renderItem: function(params, api) {{
                var s = systems[params.dataIndex];
                var color = s[6];
                var pBase = api.coord([s[0], s[1]]);
                var pTop = api.coord([s[0], s[2]]);
                var lx = pBase[0] + s[7], ly = pBase[1] + s[8];
                // unit vector from label toward the dot (for the arrow)
                var vx = pBase[0] - lx, vy = pBase[1] - ly;
                var L = Math.sqrt(vx * vx + vy * vy) || 1;
                var ux = vx / L, uy = vy / L, perpX = -uy, perpY = ux;
                var ex = pBase[0] - ux * 10, ey = pBase[1] - uy * 10;   // arrow tip at dot edge
                var detail = s[1] + ' kgCO₂e/m² · €' + s[3] + '–' + s[4];
                return {{
                    type: 'group',
                    children: [
                        {{ type: 'line', shape: {{ x1: pBase[0], y1: pBase[1], x2: pTop[0], y2: pTop[1] }},
                          style: {{ stroke: '#cbd5e1', lineWidth: 1.4 }} }},
                        {{ type: 'line', shape: {{ x1: lx, y1: ly, x2: ex, y2: ey }},
                          style: {{ stroke: color, lineWidth: 1.2 }} }},
                        {{ type: 'polygon', shape: {{ points: [
                              [ex, ey],
                              [ex - ux * 8 + perpX * 4, ey - uy * 8 + perpY * 4],
                              [ex - ux * 8 - perpX * 4, ey - uy * 8 - perpY * 4]
                          ] }}, style: {{ fill: color }} }},
                        {{ type: 'circle', shape: {{ cx: pBase[0], cy: pBase[1], r: 8 }},
                          style: {{ fill: color, stroke: '#ffffff', lineWidth: 1.6 }} }},
                        {{ type: 'text', style: {{ text: s[5], x: lx, y: ly - 1,
                          textAlign: s[9], textVerticalAlign: 'bottom',
                          font: 'bold 11px sans-serif', fill: '#1e293b' }} }},
                        {{ type: 'text', style: {{ text: detail, x: lx, y: ly + 2,
                          textAlign: s[9], textVerticalAlign: 'top',
                          font: '9px sans-serif', fill: '#64748b' }} }}
                    ]
                }};
            }}
        }}]
    }});
}}

// ==========================================
// ANALYSIS TAB CHARTS
// ==========================================
function initAnalysisCharts() {{
    // ── Materials Contribution Histogram — stacked bars per material by A1-A3/A4/A5 ──
    var histEl = document.getElementById('materialsHistogramChart');
    if (histEl) {{
        var histChart = _echartsInit('materialsHistogramChart');
        // Per-material per-stage data (tCO₂e). Arrays come from material_stage_emissions.
        var histRaw = [
            {{ name: 'Concrete',        a1a3: Number(concreteEmissions[0] || 0),        a4: Number(concreteEmissions[1] || 0),        a5: Number(concreteEmissions[2] || 0) }},
            {{ name: 'Rebar',           a1a3: Number(rebarEmissions[0] || 0),           a4: Number(rebarEmissions[1] || 0),           a5: Number(rebarEmissions[2] || 0) }},
            {{ name: 'Steel Section',   a1a3: Number(structuralSteelEmissions[0] || 0), a4: Number(structuralSteelEmissions[1] || 0), a5: Number(structuralSteelEmissions[2] || 0) }},
            {{ name: 'Post Tensioning', a1a3: Number(ptEmissions[0] || 0),              a4: Number(ptEmissions[1] || 0),              a5: Number(ptEmissions[2] || 0) }},
            {{ name: 'Timber',          a1a3: Number(timberEmissions[0] || 0),          a4: Number(timberEmissions[1] || 0),          a5: Number(timberEmissions[2] || 0) }}
        ].map(function(r) {{ r.total = r.a1a3 + r.a4 + r.a5; return r; }})
         .filter(function(r) {{ return r.total > 0; }})
         .sort(function(a, b) {{ return b.total - a.total; }});

        var histLabels = histRaw.map(function(r) {{ return r.name; }});
        var histA1A3   = histRaw.map(function(r) {{ return Number(r.a1a3.toFixed(3)); }});
        var histA4     = histRaw.map(function(r) {{ return Number(r.a4.toFixed(3)); }});
        var histA5     = histRaw.map(function(r) {{ return Number(r.a5.toFixed(3)); }});

        histChart.setOption({{
            tooltip: {{
                trigger: 'axis',
                axisPointer: {{ type: 'shadow' }},
                formatter: function(params) {{
                    if (!params || !params.length) return '';
                    var name = params[0].axisValue;
                    var total = 0;
                    var rows = params.map(function(p) {{
                        total += Number(p.value || 0);
                        return p.marker + p.seriesName + ': <b>' + Number(p.value || 0).toFixed(3) + '</b> tCO₂e';
                    }}).join('<br>');
                    return '<b>' + name + '</b><br>' + rows +
                           '<br><span style="color:#64748b;">Total: <b>' + total.toFixed(3) + '</b> tCO₂e</span>';
                }}
            }},
            legend: {{ top: 0, textStyle: {{ fontSize: 10 }}, itemWidth: 12, itemHeight: 8 }},
            grid: {{ left: '12%', right: '4%', top: '20%', bottom: '14%' }},
            xAxis: {{
                type: 'category',
                data: histLabels,
                axisLabel: {{ fontSize: 11, fontWeight: 600, interval: 0 }}
            }},
            yAxis: {{
                type: 'value',
                name: 'tCO₂e',
                nameTextStyle: {{ fontSize: 10 }},
                axisLabel: {{ fontSize: 10 }}
            }},
            series: [
                {{ name: 'A1-A3', type: 'bar', stack: 'total', data: histA1A3, itemStyle: {{ color: '#2c3e50' }} }},
                {{ name: 'A4',    type: 'bar', stack: 'total', data: histA4,   itemStyle: {{ color: '#f39c12' }} }},
                {{ name: 'A5',    type: 'bar', stack: 'total', data: histA5,   itemStyle: {{ color: '#27ae60' }} }}
            ]
        }});
    }}

    var lcBar = _echartsInit('lifecycleBarChart');
    var stageUnc = (Number(dashboardInsights.stage_uncertainty_pct || 25) / 100.0);
    var totalStage = stageEmissions.reduce(function(s, v) {{ return s + Number(v || 0); }}, 0);
    var low = totalStage * (1 - stageUnc);
    var high = totalStage * (1 + stageUnc);
    lcBar.setOption({{
        tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }},
        legend: {{ top: 0, textStyle: {{ fontSize: 10 }} }},
        grid: {{ left: '14%', right: '8%', top: '18%', bottom: '14%' }},
        xAxis: {{ type: 'value', name: 'tCO2e' }},
        yAxis: {{ type: 'category', data: ['A1-A5 total'] }},
        series: [
            {{ name: 'A1-A3', type: 'bar', stack: 'sum', data: [Number(stageEmissions[0] || 0)], itemStyle: {{ color: colors.a1a3 }} }},
            {{ name: 'A4', type: 'bar', stack: 'sum', data: [Number(stageEmissions[1] || 0)], itemStyle: {{ color: colors.a4 }} }},
            {{ name: 'A5', type: 'bar', stack: 'sum', data: [Number(stageEmissions[2] || 0)], itemStyle: {{ color: colors.a5 }} }},
            {{
                type: 'custom',
                name: 'Uncertainty',
                data: [[low, high]],
                renderItem: function(params, api) {{
                    var y = api.coord([0, 0])[1];
                    var p1 = api.coord([api.value(0), 0]);
                    var p2 = api.coord([api.value(1), 0]);
                    return {{
                        type: 'group',
                        children: [
                            {{ type: 'line', shape: {{ x1: p1[0], y1: y, x2: p2[0], y2: y }}, style: {{ stroke: '#64748b', lineWidth: 2 }} }},
                            {{ type: 'line', shape: {{ x1: p1[0], y1: y-5, x2: p1[0], y2: y+5 }}, style: {{ stroke: '#64748b', lineWidth: 2 }} }},
                            {{ type: 'line', shape: {{ x1: p2[0], y1: y-5, x2: p2[0], y2: y+5 }}, style: {{ stroke: '#64748b', lineWidth: 2 }} }}
                        ]
                    }};
                }},
                tooltip: {{ formatter: 'Uncertainty band: ' + low.toFixed(2) + ' to ' + high.toFixed(2) + ' tCO2e' }}
            }}
        ]
    }});

    // Stage Pie (small)
    var stagePie = _echartsInit('stagePieChart');
    stagePie.setOption({{
        tooltip: {{ trigger: 'item', formatter: '{{b}}: {{c}} tCO2e ({{d}}%)' }},
        series: [{{
            type: 'pie',
            radius: ['40%', '70%'],
            center: ['50%', '50%'],
            data: [
                {{ name: 'A1-A3', value: stageEmissions[0]?.toFixed(2), itemStyle: {{ color: colors.a1a3 }} }},
                {{ name: 'A4', value: stageEmissions[1]?.toFixed(2), itemStyle: {{ color: colors.a4 }} }},
                {{ name: 'A5', value: stageEmissions[2]?.toFixed(2), itemStyle: {{ color: colors.a5 }} }}
            ],
            label: {{ show: true, formatter: '{{b}}\\n{{d}}%', fontSize: 10 }},
            emphasis: {{ itemStyle: {{ shadowBlur: 8, shadowColor: 'rgba(0,0,0,0.2)' }} }}
        }}]
    }});

    // Init filtered table
    updateFilteredTable(detailedData);

    // Update emission filter labels with actual data-driven thresholds
    (function() {{
        var sel = document.getElementById('filter-emission');
        if (!sel) return;
        var opts = sel.options;
        for (var i = 0; i < opts.length; i++) {{
            if (opts[i].value === 'high') opts[i].text = 'High (> ' + EC_HIGH_LABEL + ' tCO2e)';
            else if (opts[i].value === 'medium') opts[i].text = 'Medium (' + EC_LOW_LABEL + '\u2013' + EC_HIGH_LABEL + ' tCO2e)';
            else if (opts[i].value === 'low') opts[i].text = 'Low (< ' + EC_LOW_LABEL + ' tCO2e)';
        }}
    }})();

    window.addEventListener('resize', function() {{
        // 'mekko' was a chart variable from an earlier version of this
        // dashboard; it was removed but this resize call was left behind,
        // referencing a variable that was never declared. Since it's the
        // first array element, accessing it threw on EVERY resize event
        // (including the synthetic one openTab() fires after every tab
        // switch) before lcBar/stagePie ever got a chance to resize either.
        [lcBar, stagePie].forEach(function(c) {{ if (c) c.resize(); }});
    }});
}}

// ==========================================
// FILTER FUNCTIONS
// ==========================================
function applyFilters() {{
    var material = document.getElementById('filter-material').value;
    var category = document.getElementById('filter-category').value;
    var level = document.getElementById('filter-level').value;
    var emission = document.getElementById('filter-emission').value;
    var filtered = [...detailedData];
    if (material !== 'all') filtered = filtered.filter(function(d) {{ return d.material === material; }});
    if (category !== 'all') filtered = filtered.filter(function(d) {{ return d.category === category; }});
    if (level !== 'all') filtered = filtered.filter(function(d) {{ return (d.level||'') === level; }});
    if (emission !== 'all') {{
        if (emission === 'high') filtered = filtered.filter(function(d) {{ return (d.total||0) > EC_HIGH; }});
        else if (emission === 'medium') filtered = filtered.filter(function(d) {{ return (d.total||0) >= EC_LOW && (d.total||0) <= EC_HIGH; }});
        else if (emission === 'low') filtered = filtered.filter(function(d) {{ return (d.total||0) > 0 && (d.total||0) < EC_LOW; }});
        else if (emission === 'zero') filtered = filtered.filter(function(d) {{ return !(d.total||0); }});
    }}
    filteredDataCache = filtered;
    updateFilteredTable(filtered);
    var totalF = filtered.reduce(function(s, d) {{ return s + (d.total||0); }}, 0);
    document.getElementById('filterSummary').innerHTML = filtered.length + ' elements | ' + totalF.toFixed(2) + ' tCO2e (' + (totalEmission > 0 ? (totalF/totalEmission*100).toFixed(1) : '0') + '%)';
}}

function resetFilters() {{
    document.getElementById('filter-material').value = 'all';
    document.getElementById('filter-category').value = 'all';
    document.getElementById('filter-level').value = 'all';
    document.getElementById('filter-emission').value = 'all';
    filteredDataCache = [...detailedData];
    updateFilteredTable(detailedData);
    document.getElementById('filterSummary').innerHTML = detailedData.length + ' elements | ' + totalEmission.toFixed(2) + ' tCO2e';
}}

function updateFilteredTable(data) {{
    var tbody = document.getElementById('filteredDataBody');

    function baseMemberName(d) {{
        var desc = String(d.description || '').trim();
        if (desc.indexOf(' - ') >= 0) return desc.split(' - ')[0].trim();
        if (desc) return desc;
        return String(d.category || d.material || 'Unknown');
    }}

    function materialRank(d) {{
        var m = String(d.material || '').toLowerCase();
        if (m === 'concrete') return 0;
        if (m === 'rebar') return 1;
        if (m === 'steel section') return 2;
        if (m === 'post tensioning') return 3;
        return 4;
    }}

    function sortedByMemberFlow(rows) {{
        var groups = {{}};
        rows.forEach(function(d) {{
            var key = baseMemberName(d);
            if (!groups[key]) groups[key] = [];
            groups[key].push(d);
        }});
        var groupList = Object.keys(groups).map(function(k) {{
            var rs = groups[k];
            var concrete = rs.find(function(r) {{ return String(r.material || '') === 'Concrete'; }});
            var keyTotal = concrete ? Number(concrete.total || 0) : Math.max.apply(null, rs.map(function(r) {{ return Number(r.total || 0); }}));
            return {{ key: k, rows: rs, keyTotal: keyTotal }};
        }});
        groupList.sort(function(a, b) {{
            if (b.keyTotal !== a.keyTotal) return b.keyTotal - a.keyTotal;
            return a.key.localeCompare(b.key);
        }});
        var out = [];
        groupList.forEach(function(g) {{
            g.rows.sort(function(a, b) {{
                var ra = materialRank(a), rb = materialRank(b);
                if (ra !== rb) return ra - rb;
                return (Number(b.total || 0) - Number(a.total || 0));
            }});
            out = out.concat(g.rows);
        }});
        return out;
    }}

    function familyTypeLabel(d) {{
        var fam = String(d.family || '').trim();
        var typ = String(d.ifc_type || '').trim();
        if (!fam && !typ) return '-';
        return (fam || '-') + ' / ' + (typ || '-');
    }}

    var sorted = sortedByMemberFlow(data).slice(0, 50);
    tbody.innerHTML = sorted.map(function(d) {{
        var a1a5 = ((d.a1_a3||0)+(d.a4||0)+(d.a5||0)).toFixed(2);
        var totalVal = d.total||0;
        var zeroStyle = totalVal === 0 ? 'color:#94a3b8;font-style:italic;' : 'font-weight:600;';
        var totalDisp = totalVal === 0 ? 'No EC Data' : totalVal.toFixed(3);
        var matCell = escH(d.material||'N/A') + ((d.description||'') ? ('<div style="font-size:0.68rem;color:#64748b;">' + escH(d.description) + '</div>') : '');
        return '<tr><td>'+matCell+'</td><td>'+escH(d.category||'N/A')+'</td><td>'+escH(familyTypeLabel(d))+'</td><td style="text-align:right;">'+(d.mass||0).toLocaleString()+'</td><td style="text-align:right;">'+a1a5+'</td><td style="text-align:right;'+zeroStyle+'">'+totalDisp+'</td></tr>';
    }}).join('');
}}

// ==========================================
// SOLUTIONS TAB CHARTS
// ==========================================
function initSolutionsCharts() {{
    var wfChart = _echartsInit('strategyWaterfallChart');
    var sankey = _echartsInit('boqSankeyChart');
    var epdDonut = _echartsInit('epdCoverageDonut');

    function allowedStrategies() {{
        var s = dashboardInsights.strategy_deltas || {{}};
        var base = [
            {{ key: 'structural', name: 'Structural optimisation', delta: Number(s.structural || 0), stage: 'Concept / Schematic Design' }},
            {{ key: 'ggbs_70', name: '70% GGBS', delta: Number(s.ggbs_70 || 0), stage: 'Detailed Design' }},
            {{ key: 'ggbs_50', name: '50% GGBS', delta: Number(s.ggbs_50 || 0), stage: 'Detailed Design' }},
            {{ key: 'rebar', name: 'Rebar optimisation', delta: Number(s.rebar || 0), stage: 'Detailed Design' }},
            {{ key: 'epd', name: 'EPD substitution', delta: Number(s.epd || 0), stage: 'Tender' }},
            {{ key: 'transport', name: 'Transport refinement', delta: Number(s.transport || 0), stage: 'Tender' }},
            {{ key: 'a5_refine', name: 'A5a refinement', delta: Number(s.a5_refine || 0), stage: 'Tender' }}
        ];
        if (currentStage === 'Concept / Schematic Design') return base.filter(function(x) {{ return x.key === 'structural' || x.key.indexOf('ggbs') === 0; }});
        if (currentStage === 'Detailed Design') return base.filter(function(x) {{ return x.key.indexOf('ggbs') === 0 || x.key === 'rebar' || x.key === 'epd'; }});
        return base.filter(function(x) {{ return x.stage === 'Tender'; }});
    }}

    function updateWaterfall(steps) {{
        if (!wfChart) return;
        var names = steps.map(function(s) {{ return s.label; }});
        var vals = steps.map(function(s) {{ return Number(s.remaining || 0); }});
        wfChart.setOption({{
            tooltip: {{ trigger: 'axis' }},
            grid: {{ left: '10%', right: '5%', top: '10%', bottom: '16%' }},
            xAxis: {{ type: 'category', data: names, axisLabel: {{ rotate: 20, fontSize: 10 }} }},
            yAxis: {{ type: 'value', name: 'tCO2e' }},
            series: [{{
                type: 'bar',
                data: vals.map(function(v, i) {{ return {{ value: v, itemStyle: {{ color: i === 0 ? '#dc2626' : (i === vals.length - 1 ? '#16a34a' : '#0ea5e9') }} }}; }}),
                label: {{ show: true, position: 'top', formatter: function(p) {{ return Number(p.value).toFixed(2); }} }}
            }}]
        }});
    }}

    function updateSankey() {{
        if (!sankey) return;
        var massMap = dashboardInsights.material_mass_by_type || {{}};
        var massTotalT = Object.keys(massMap).reduce(function(s, k) {{ return s + Number(massMap[k] || 0); }}, 0) / 1000.0;
        sankey.setOption({{
            tooltip: {{ trigger: 'item', formatter: '{{b}}: {{c}}' }},
            series: [{{
                type: 'sankey',
                data: [
                    {{ name: 'BOQ mass (t)' }},
                    {{ name: 'Weighted factor' }},
                    {{ name: 'A1-A3' }},
                    {{ name: 'A4' }},
                    {{ name: 'A5' }},
                    {{ name: 'Total' }}
                ],
                links: [
                    {{ source: 'BOQ mass (t)', target: 'Weighted factor', value: Number(massTotalT.toFixed(2)) }},
                    {{ source: 'Weighted factor', target: 'A1-A3', value: Number((stageEmissions[0] || 0).toFixed(2)) }},
                    {{ source: 'A1-A3', target: 'A4', value: Number((stageEmissions[1] || 0).toFixed(2)) }},
                    {{ source: 'A4', target: 'A5', value: Number((stageEmissions[2] || 0).toFixed(2)) }},
                    {{ source: 'A5', target: 'Total', value: Number(totalEmission.toFixed(2)) }}
                ],
                lineStyle: {{ color: 'gradient', curveness: 0.5 }},
                emphasis: {{ focus: 'adjacency' }}
            }}]
        }});
    }}

    function updateEpdDonut() {{
        if (!epdDonut) return;
        var cov = Number(dashboardInsights.epd_coverage_pct || 0);
        epdDonut.setOption({{
            tooltip: {{ trigger: 'item' }},
            series: [{{
                type: 'pie', radius: ['45%', '70%'],
                data: [
                    {{ name: 'EPD-covered mass', value: cov, itemStyle: {{ color: '#16a34a' }} }},
                    {{ name: 'Generic/default mass', value: Math.max(0, 100 - cov), itemStyle: {{ color: '#cbd5e1' }} }}
                ],
                label: {{ formatter: '{{b}}\\n{{d}}%' }}
            }}]
        }});
    }}

    window._updateGoalSeek = function() {{
        var targetMap = {{ 'A++': 50, 'A+': 100, 'A': 150, 'B': 200, 'C': 250, 'D': 300 }};
        var sel = document.getElementById('goalRatingSelect');
        if (!sel) return;
        var targetInt = Number(targetMap[sel.value] || 200);
        var targetTotal = (targetInt * projectArea) / 1000.0;
        var remaining = Number(totalEmission);
        var rows = "";
        var steps = [{{ label: 'Baseline', remaining: remaining }}];
        var actions = allowedStrategies().filter(function(x) {{ return x.delta > 0; }}).sort(function(a, b) {{ return b.delta - a.delta; }});

        for (var i = 0; i < actions.length; i++) {{
            var a = actions[i];
            remaining = Math.max(0, remaining - a.delta);
            var remInt = projectArea > 0 ? (remaining * 1000 / projectArea) : 0;
            rows += '<tr><td>' + (i + 1) + '. ' + a.name + '</td><td style="text-align:right;">' + a.delta.toFixed(2) + '</td><td style="text-align:right;">' + remaining.toFixed(2) + '</td><td style="text-align:right;">' + remInt.toFixed(1) + '</td></tr>';
            steps.push({{ label: a.name, remaining: remaining }});
            if (remaining <= targetTotal) break;
        }}

        var tbody = document.getElementById('goalSeekTableBody');
        if (tbody) tbody.innerHTML = rows || '<tr><td colspan="4" style="text-align:center;color:#94a3b8;">No active interventions for this stage</td></tr>';
        var saved = Math.max(0, totalEmission - remaining);
        var finalInt = projectArea > 0 ? (remaining * 1000 / projectArea) : 0;
        var status = finalInt <= targetInt ? 'Meets target' : 'Gap remains';

        var savedEl = document.getElementById('goalSaved'); if (savedEl) savedEl.textContent = saved.toFixed(2) + ' t';
        var finEl = document.getElementById('goalFinal'); if (finEl) finEl.textContent = remaining.toFixed(2) + ' t';
        var finIntEl = document.getElementById('goalFinalIntensity'); if (finIntEl) finIntEl.textContent = finalInt.toFixed(1) + ' kg/m²';
        var statusEl = document.getElementById('goalStatus'); if (statusEl) statusEl.textContent = status;

        updateWaterfall(steps);
        updateSankey();
        updateEpdDonut();
    }};

    updateSankey();
    updateEpdDonut();
    window._updateGoalSeek();
    window.addEventListener('resize', function() {{
        [wfChart, sankey, epdDonut].forEach(function(c) {{ if (c) c.resize(); }});
    }});
}}

// ==========================================
// SCENARIO TABLE
// ==========================================
function updateScenarioTable() {{
    if (typeof window._updateGoalSeek === 'function') window._updateGoalSeek();
}}

function updateScenarioDisplay() {{ updateScenarioTable(); }}

function updateGoalSeek() {{ updateScenarioTable(); }}

function openTabById(tabName) {{
    var buttons = document.querySelectorAll('.tab-button');
    for (var i = 0; i < buttons.length; i++) {{
        var btn = buttons[i];
        var onclick = btn.getAttribute('onclick') || '';
        if (onclick.indexOf(tabName) >= 0) {{
            btn.click();
            return;
        }}
    }}
}}

function openTenderSpecOnePager() {{
    var rating = (document.getElementById('goalRatingSelect') || {{ value: 'B' }}).value;
    var w = window.open('', '_blank');
    if (!w) return;
    var html = '' +
      '<html><head><title>Tender EC Specification</title><style>' +
            'body{{font-family:Segoe UI,Arial,sans-serif;padding:28px;color:#0f172a;}}h1{{margin:0 0 8px;}}h2{{margin:18px 0 6px;font-size:16px;}}ul{{margin-top:6px;}} .box{{border:1px solid #cbd5e1;border-radius:8px;padding:12px;margin-top:8px;background:#f8fafc;}}' +
      '</style></head><body>' +
      '<h1>Tender EC Specification</h1>' +
      '<div>Project target rating: <b>' + rating + '</b> | Stage: <b>' + currentStage + '</b></div>' +
      '<div class="box"><h2>Required EPD Coverage</h2><ul><li>Concrete suppliers provide verified product EPDs.</li><li>Rebar/steel suppliers provide mill-specific EPDs.</li><li>Declare EPD validity period and module scope.</li></ul></div>' +
      '<div class="box"><h2>Max Material EC Limits</h2><ul><li>Concrete: enforce tender-specific A1-A3 max values per grade/spec.</li><li>Steel/Rebar: submit declared kgCO2e/kg for supplied products.</li><li>Transport: provide confirmed haul distances and mode split.</li></ul></div>' +
      '<div class="box"><h2>Contractor Data Deliverables</h2><ul><li>A5a waste rates confirmed by contractor method statement.</li><li>Supplier declaration pack attached to payment milestones.</li><li>Any substitution requires updated EC calculation before approval.</li></ul></div>' +
      '<div style="margin-top:18px;font-size:12px;color:#64748b;">Generated from Advanced Dashboard tender stage controls. Use Print to PDF for issue.</div>' +
      '</body></html>';
    w.document.write(html);
    w.document.close();
}}

// ==========================================
// THREE.JS 3D IFC VIEWER (Advanced Dashboard)
// ==========================================
let threeScene2, threeCamera2, threeRenderer2, threeControls2;
let buildingMeshes2 = [];

// Category colours — darker architectural palette with higher opacity
const CAT_COLORS_2 = {{
    'Column':               0x5a7a9e,  // deeper steel blue
    'Beam':                 0x9a6c38,  // richer sandstone
    'Slab/Floor':           0x4e7d5e,  // deeper sage green
    'Wall':                 0x8a7060,  // deeper warm stone
    'Foundation/Footing':   0x5c5040,  // dark earth
    'Pile':                 0x4a5e50,  // deep slate green
    'Roof':                 0x4d7080,  // deep cool blue-grey
    'Stair':                0x7a6080,  // deep mauve
    'Reinforcement':        0x6a5a50,  // deep warm grey-brown
    'Post-Tensioning':      0x4a6e6e,  // deep teal-grey
    'Glazing/Curtain Wall': 0x6a9ab4,  // deeper sky blue
    'Door':                 0x9a7840,  // deeper warm tan
    'Other':                0x7a8490   // deeper neutral grey
}};

// Resolve display category: check element name/description for non-structural keywords
function resolveCategory2(cat, name) {{
    var n = (name||'').toLowerCase();
    if (/glaz|curtain|cladding|facade|glazed/.test(n)) return 'Glazing';
    if (/window/.test(n)) return 'Glazing';
    if (/door/.test(n)) return 'Door';
    if (/railing|balustrade|handrail/.test(n)) return 'Railing';
    if (/ceiling|soffit/.test(n)) return 'Ceiling';
    if (/insul/.test(n)) return 'Insulation';
    if (/parapet/.test(n)) return 'Parapet';
    return cat || 'Other';
}}

// Emission colour: fixed 25-unit bands from 400-700
// Below 400 clamps to the 400 colour, above 700 clamps to the 700 colour.
const _EC_MIN = 400, _EC_MAX = 700;
const _EC_ANCHORS = [
    [400, [0x14,0x5a,0x14]],   // dark green    — min boundary
    [450, [0x1a,0x7a,0x1a]],   // darker green
    [500, [0x5c,0xb8,0x5c]],   // green
    [550, [0x90,0xee,0x90]],   // light green
    [600, [0xf0,0xe0,0x20]],   // yellow
    [625, [0xf5,0xc0,0x10]],   // amber
    [650, [0xf0,0x70,0x10]],   // orange
    [675, [0xe0,0x40,0x20]],   // orange-red
    [700, [0x8b,0x00,0x00]],   // darkest red   — max boundary
];
function _ecInterp(val) {{
    if (val <= _EC_MIN) return new THREE.Color(_EC_ANCHORS[0][1][0]/255, _EC_ANCHORS[0][1][1]/255, _EC_ANCHORS[0][1][2]/255);
    if (val >= _EC_MAX) return new THREE.Color(_EC_ANCHORS[_EC_ANCHORS.length-1][1][0]/255, _EC_ANCHORS[_EC_ANCHORS.length-1][1][1]/255, _EC_ANCHORS[_EC_ANCHORS.length-1][1][2]/255);
    let lo = _EC_ANCHORS[0], hi = _EC_ANCHORS[_EC_ANCHORS.length-1];
    for (let i = 0; i < _EC_ANCHORS.length-1; i++) {{
        if (val >= _EC_ANCHORS[i][0] && val <= _EC_ANCHORS[i+1][0]) {{
            lo = _EC_ANCHORS[i]; hi = _EC_ANCHORS[i+1]; break;
        }}
    }}
    const t = (val-lo[0])/(hi[0]-lo[0]);
    return new THREE.Color(
        Math.round(lo[1][0]+t*(hi[1][0]-lo[1][0]))/255,
        Math.round(lo[1][1]+t*(hi[1][1]-lo[1][1]))/255,
        Math.round(lo[1][2]+t*(hi[1][2]-lo[1][2]))/255
    );
}}
function emissionColor2(pm3) {{
    if (pm3 <= 0) return new THREE.Color(0x94a3b8); // No EC — grey
    const band = Math.max(_EC_MIN, Math.min(_EC_MAX, Math.floor(pm3 / 25) * 25));
    return _ecInterp(band);
}}

function totalColor2(t, p25, p50, p75, p90) {{
    if (t <= 0)    return new THREE.Color(0x94a3b8);
    if (t <= p25)  return new THREE.Color(0x4ade80);
    if (t <= p50)  return new THREE.Color(0xa3e635);
    if (t <= p75)  return new THREE.Color(0xfbbf24);
    if (t <= p90)  return new THREE.Color(0xf97316);
    return new THREE.Color(0xef4444);
}}

function get3DStageMode2() {{
    const sel = document.getElementById('ec-stage-filter-2');
    return sel ? sel.value : 'a1a5';
}}

function getStageTotal2(g) {{
    const stageMode = get3DStageMode2();
    if (stageMode === 'a1a3') return Number(g.a1_a3_t || 0);
    return Number(g.total || 0);
}}

function getStageIntensity2(g) {{
    const stageMode = get3DStageMode2();
    if (stageMode === 'a1a3') return Number(g.per_m3_a1a3 || 0);
    return Number(g.per_m3 || 0);
}}

function buildMeshFromData(g) {{
    if (g.v && g.t && g.v.length >= 9 && g.t.length >= 3) {{
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(g.v), 3));
        geo.setIndex(new THREE.BufferAttribute(new Uint32Array(g.t), 1));
        geo.computeVertexNormals();
        return geo;
    }}
    const gw = Math.max(g.w, 0.05), gd = Math.max(g.d, 0.05), gh = Math.max(g.h, 0.05);
    const box = new THREE.BoxGeometry(gw, gh, gd);
    box.translate(g.x + gw/2, g.z + gh/2, g.y + gd/2);
    return box;
}}

function init3DViewer2() {{
    var loadingEl = document.getElementById('three-loading-2');
    if (typeof THREE === 'undefined' || typeof THREE.OrbitControls === 'undefined') {{
        if (!window._3dLoadRetries2) window._3dLoadRetries2 = 0;
        window._3dLoadRetries2++;
        if (window._3dLoadRetries2 < 40) {{ setTimeout(init3DViewer2, 500); return; }}
        if (loadingEl) loadingEl.innerHTML = '<div style="color:#e74c3c;">Failed to load 3D libraries.</div>';
        return;
    }}
    const canvas = document.getElementById('three-canvas-2');
    if (!canvas || geometryData2.length === 0) {{
        if (loadingEl) loadingEl.innerHTML = '<div style="color:#888;">No 3D geometry data.</div>';
        return;
    }}
    const container = document.getElementById('three-container-2') || canvas.parentElement;
    let w = container.clientWidth, h = container.clientHeight;
    if (w < 10 || h < 10) {{
        if (!window._3dRetries2) window._3dRetries2 = 0;
        window._3dRetries2++;
        if (window._3dRetries2 < 20) {{ setTimeout(init3DViewer2, 300); return; }}
        w = 700; h = 500;
    }}
    try {{
        function pct(arr, p) {{ return arr.length ? arr[Math.max(0, Math.floor(arr.length*p/100)-1)] : 0; }}
        const totVals = geometryData2.map(g => g.total||0).filter(v => v > 0).sort((a,b) => a-b);
        const totP25 = pct(totVals, 25) || 0.02;
        const totP50 = pct(totVals, 50) || 0.05;
        const totP75 = pct(totVals, 75) || 0.15;
        const totP90 = pct(totVals, 90) || 0.3;

        threeScene2 = new THREE.Scene();
        threeScene2.background = new THREE.Color(0xdce6ef);  // light blue-grey

        threeCamera2 = new THREE.PerspectiveCamera(50, w/h, 0.1, 5000);
        threeRenderer2 = new THREE.WebGLRenderer({{ canvas: canvas, antialias: true }});
        threeRenderer2.setSize(w, h);
        threeRenderer2.setPixelRatio(Math.min(window.devicePixelRatio, 2));

        threeScene2.add(new THREE.AmbientLight(0xffffff, 1.0));
        const dl = new THREE.DirectionalLight(0xffffff, 0.3);
        dl.position.set(50, 100, 50);
        threeScene2.add(dl);

        if (loadingEl) loadingEl.style.display = 'none';

        let mnX=Infinity, mnY=Infinity, mnZ=Infinity;
        let mxX=-Infinity, mxY=-Infinity, mxZ=-Infinity;
        buildingMeshes2 = [];

        let meshBuildFailures = 0;
        geometryData2.forEach((g, idx) => {{
            // One malformed element must not take the other 2000+ down with it —
            // this used to be unguarded, so a single bad element threw out of
            // the forEach callback and silently abandoned every element after
            // it (the only visible symptom: a near-empty scene, no error shown,
            // since the loadingEl the outer catch writes to was already hidden
            // by this point).
            try {{
                const geo = buildMeshFromData(g);
                const color = totalColor2(g.total, totP25, totP50, totP75, totP90);
                const mat = new THREE.MeshLambertMaterial({{
                    color: color, transparent: true, opacity: 0.95,
                    side: THREE.DoubleSide
                }});
                const mesh = new THREE.Mesh(geo, mat);
                mesh.userData = {{ idx: idx, data: g, totP25, totP50, totP75, totP90 }};
                threeScene2.add(mesh);
                buildingMeshes2.push({{ mesh: mesh, data: g, idx: idx }});
                geo.computeBoundingBox();
                const bb = geo.boundingBox;
                if (bb) {{
                    mnX=Math.min(mnX,bb.min.x); mnY=Math.min(mnY,bb.min.y); mnZ=Math.min(mnZ,bb.min.z);
                    mxX=Math.max(mxX,bb.max.x); mxY=Math.max(mxY,bb.max.y); mxZ=Math.max(mxZ,bb.max.z);
                }}
            }} catch (meshErr) {{
                meshBuildFailures++;
                if (meshBuildFailures <= 5) {{
                    console.warn('3D Viewer: skipped element ' + idx + ' (' + (g && g.name) + '):', meshErr);
                }}
            }}
        }});
        if (meshBuildFailures > 0) {{
            console.warn('3D Viewer: ' + meshBuildFailures + ' of ' + geometryData2.length + ' elements failed to build and were skipped.');
        }}

        // Update legend with actual thresholds
        var leg = document.getElementById('ec-legend-2');
        if (leg) {{
            // Build fixed 400-700 gradient legend with 25-unit bands.
            let swatches = '';
            for (let v = 400; v <= 700; v += 25) {{
                const c = _ecInterp(v);
                const hex = '#' + Math.round(c.r*255).toString(16).padStart(2,'0') +
                    Math.round(c.g*255).toString(16).padStart(2,'0') +
                    Math.round(c.b*255).toString(16).padStart(2,'0');
                swatches += '<span style="display:inline-block;width:10px;height:14px;background:' + hex + ';"></span>';
            }}
            let labels = '';
            for (let v = 400; v <= 700; v += 25) {{
                const lbl = v === 400 ? '\u2264400' : v === 700 ? '\u2265700' : String(v);
                labels += '<span style="font-size:0.6rem;color:#888;writing-mode:vertical-rl;transform:rotate(180deg);height:28px;line-height:10px;">' + lbl + '</span>';
            }}
            leg.innerHTML =
                '<div style="font-weight:600;font-size:0.72rem;margin-bottom:4px;">Emission Intensity (kgCO\u2082e/m\u00b3)</div>' +
                '<div style="display:flex;gap:0;margin-bottom:1px;">' + swatches + '</div>' +
                '<div style="display:flex;gap:0;margin-bottom:4px;">' + labels + '</div>' +
                '<div style="display:flex;align-items:center;gap:4px;font-size:0.7rem;"><span style="display:inline-block;width:14px;height:10px;border-radius:2px;background:#94a3b8;"></span>No EC Data</div>';
        }}

        // Keep the full model bounds for fit-to-view (also used as fallback
        // when every mesh is filtered out).
        window._modelBounds2 = {{ mnX: mnX, mnY: mnY, mnZ: mnZ, mxX: mxX, mxY: mxY, mxZ: mxZ }};

        threeControls2 = new THREE.OrbitControls(threeCamera2, threeRenderer2.domElement);
        threeControls2.enableDamping = true;
        threeControls2.dampingFactor = 0.1;
        threeControls2.minPolarAngle = 0;
        threeControls2.maxPolarAngle = Math.PI;  // full vertical range

        // Frame the whole model: distance derived from the bounding sphere and
        // the camera's vertical AND horizontal FOV, so nothing is cut off at
        // the edges regardless of model proportions or panel aspect ratio
        // (the old fixed offset cropped tall/wide models). Near/far follow the
        // fitted distance, so large site coordinates can't clip geometry away.
        fit3DView2();

        // Populate level filter dropdown from geometry data
        var levelSel = document.getElementById('level-filter-3d');
        if (levelSel) {{
            var uniqueLevels = [...new Set(geometryData2.map(g => (g.level||'').trim()).filter(l => l && l !== 'None' && l !== 'nan' && l !== 'N/A'))].sort();
            uniqueLevels.forEach(function(lv) {{
                var opt = document.createElement('option');
                opt.value = lv; opt.text = lv;
                levelSel.appendChild(opt);
            }});
        }}

        const ray = new THREE.Raycaster();
        const mv = new THREE.Vector2();

        canvas.addEventListener('click', function(e) {{
            const r = canvas.getBoundingClientRect();
            mv.x = ((e.clientX-r.left)/r.width)*2-1;
            mv.y = -((e.clientY-r.top)/r.height)*2+1;
            ray.setFromCamera(mv, threeCamera2);
            const hits = ray.intersectObjects(buildingMeshes2.filter(b=>b.mesh.visible).map(b=>b.mesh));
            if (hits.length > 0) {{
                const d = hits[0].object.userData.data;
                if (window._hl2 && window._hl2 !== hits[0].object) window._hl2.material.emissive.setHex(0x000000);
                hits[0].object.material.emissive.setHex(0x222222);
                window._hl2 = hits[0].object;
                const ud = hits[0].object.userData;
                const lvlStr = d.level ? ('<tr><td style="color:#666;padding:2px 0;">Level</td><td style="font-weight:600;">' + escH(d.level) + '</td></tr>') : '';
                const stageMode = get3DStageMode2();
                const stageLabel = stageMode === 'a1a3' ? 'A1-A3' : 'A1-A5';
                const totalEc = getStageTotal2(d);
                const intensityEc = getStageIntensity2(d);
                const noEC = !intensityEc || intensityEc === 0;
                const _ic = noEC ? new THREE.Color(0x94a3b8) : _ecInterp(intensityEc);
                const intColor = noEC ? '#94a3b8' : '#' + Math.round(_ic.r*255).toString(16).padStart(2,'0') + Math.round(_ic.g*255).toString(16).padStart(2,'0') + Math.round(_ic.b*255).toString(16).padStart(2,'0');
                const dispCat = resolveCategory2(d.cat, d.name);
                document.getElementById('element-info-2').innerHTML =
                    '<b style="font-size:0.85rem;">' + escH(d.name) + '</b>' +
                    '<table style="width:100%;font-size:0.73rem;margin-top:4px;border-collapse:collapse;">' +
                    '<tr><td style="color:#666;padding:2px 0;">Category</td><td style="font-weight:600;">' + escH(dispCat) + '</td></tr>' +
                    lvlStr +
                    '<tr><td style="color:#666;padding:2px 0;">Total EC (' + stageLabel + ')</td><td style="font-weight:600;">' + (totalEc > 0 ? totalEc.toFixed(3) + ' tCO\u2082e' : '<span style="color:#94a3b8;">No EC Data</span>') + '</td></tr>' +
                    '<tr><td style="color:#666;padding:2px 0;">Intensity (' + stageLabel + ')</td><td style="font-weight:600;color:' + intColor + ';">' + (noEC ? 'N/A' : intensityEc.toFixed(1) + ' kgCO\u2082e/m\u00b3') + '</td></tr>' +
                    '<tr><td style="color:#666;padding:2px 0;">Dimensions</td><td>' + d.w.toFixed(2) + ' \u00d7 ' + d.d.toFixed(2) + ' \u00d7 ' + d.h.toFixed(2) + ' m</td></tr>' +
                    '</table>';
            }}
        }});

        canvas.addEventListener('mousemove', function(e) {{
            const r = canvas.getBoundingClientRect();
            mv.x = ((e.clientX-r.left)/r.width)*2-1;
            mv.y = -((e.clientY-r.top)/r.height)*2+1;
            ray.setFromCamera(mv, threeCamera2);
            const hits = ray.intersectObjects(buildingMeshes2.filter(b=>b.mesh.visible).map(b=>b.mesh));
            const tt = document.getElementById('element-tooltip-2');
            if (hits.length > 0) {{
                const d = hits[0].object.userData.data;
                const stageMode = get3DStageMode2();
                const stageLabel = stageMode === 'a1a3' ? 'A1-A3' : 'A1-A5';
                const totalEc = getStageTotal2(d);
                const intensityEc = getStageIntensity2(d);
                const noEC = !intensityEc || intensityEc === 0;
                const dispCat = resolveCategory2(d.cat, d.name);
                tt.innerHTML = '<b>' + escH(d.name) + '</b><br>' + escH(dispCat) +
                    (d.level ? '<br>Level: ' + escH(d.level) : '') +
                    '<br>' + stageLabel + ': ' + (noEC ? 'No EC Data' : intensityEc.toFixed(1) + ' kgCO\u2082e/m\u00b3') +
                    '<br>' + (totalEc > 0 ? totalEc.toFixed(3) + ' tCO\u2082e' : '');
                tt.style.display = 'block';
                tt.style.left = (e.clientX-r.left+12) + 'px';
                tt.style.top  = (e.clientY-r.top-10)  + 'px';
                document.body.style.cursor = 'pointer';
            }} else {{
                tt.style.display = 'none';
                document.body.style.cursor = 'default';
            }}
        }});

        // Resize the render buffer whenever the CONTAINER changes size — not
        // just the window. Inside the app the dashboard lives in an iframe /
        // tab whose layout can change without any window resize, which left a
        // stale buffer and a cropped-looking canvas.
        function _resize3D2() {{
            const rw=container.clientWidth, rh=container.clientHeight;
            if (rw>10 && rh>10) {{
                threeCamera2.aspect = rw/rh;
                threeCamera2.updateProjectionMatrix();
                threeRenderer2.setSize(rw, rh);
            }}
        }}
        window.addEventListener('resize', _resize3D2);
        if (window.ResizeObserver) {{
            new ResizeObserver(_resize3D2).observe(container);
        }}
        // Double-click = re-frame the (visible) model
        canvas.addEventListener('dblclick', function() {{ fit3DView2(); }});

        (function anim() {{
            requestAnimationFrame(anim);
            threeControls2.update();
            threeRenderer2.render(threeScene2, threeCamera2);
        }})();

    }} catch(e) {{
        console.error('3D Viewer error:', e);
        if (loadingEl) loadingEl.innerHTML = '<div style="color:#e74c3c;">' + e.message + '</div>';
    }}
}}

function fit3DView2() {{
    // Frame the visible meshes (falls back to the full model bounds).
    if (!threeCamera2 || !threeControls2) return;
    let mn=[Infinity,Infinity,Infinity], mx=[-Infinity,-Infinity,-Infinity], any=false;
    (buildingMeshes2||[]).forEach(function(b) {{
        if (!b.mesh.visible) return;
        const bb = b.mesh.geometry.boundingBox;
        if (!bb) return;
        any = true;
        mn[0]=Math.min(mn[0],bb.min.x); mn[1]=Math.min(mn[1],bb.min.y); mn[2]=Math.min(mn[2],bb.min.z);
        mx[0]=Math.max(mx[0],bb.max.x); mx[1]=Math.max(mx[1],bb.max.y); mx[2]=Math.max(mx[2],bb.max.z);
    }});
    if (!any) {{
        const B = window._modelBounds2;
        if (B) {{
            mn=[B.mnX,B.mnY,B.mnZ]; mx=[B.mxX,B.mxY,B.mxZ];
        }} else if (typeof geometryData2 !== 'undefined' && geometryData2.length) {{
            // Last-resort fallback: neither per-mesh bounding boxes nor the
            // cached model-wide bounds were available (e.g. every mesh got
            // filtered to invisible, or ran before boundingBox existed).
            // geometryData2 itself is always present, so derive a bounds
            // estimate from each element's own position + box dimensions
            // rather than leaving the camera at its unfitted default.
            mn=[Infinity,Infinity,Infinity]; mx=[-Infinity,-Infinity,-Infinity];
            geometryData2.forEach(function(g) {{
                const hw=(g.w||0.3)/2, hd=(g.d||0.3)/2, hh=(g.h||0.3)/2;
                mn[0]=Math.min(mn[0],g.x-hw); mn[1]=Math.min(mn[1],g.y-hd); mn[2]=Math.min(mn[2],g.z-hh);
                mx[0]=Math.max(mx[0],g.x+hw); mx[1]=Math.max(mx[1],g.y+hd); mx[2]=Math.max(mx[2],g.z+hh);
            }});
        }} else {{
            return;
        }}
    }}
    const cx=(mn[0]+mx[0])/2, cy=(mn[1]+mx[1])/2, cz=(mn[2]+mx[2])/2;
    const sx=mx[0]-mn[0], sy=mx[1]-mn[1], sz=mx[2]-mn[2];
    const radius = Math.max(Math.sqrt(sx*sx+sy*sy+sz*sz)/2, 5);
    const fovV = threeCamera2.fov * Math.PI/180;
    const fovH = 2*Math.atan(Math.tan(fovV/2)*Math.max(threeCamera2.aspect, 0.1));
    // Distance so the bounding sphere fits the NARROWER field of view (+12% margin)
    const dist = radius / Math.sin(Math.min(fovV, fovH)/2) * 1.12;
    threeCamera2.near = Math.max(dist/1000, 0.01);
    threeCamera2.far  = dist + radius*200;
    // Unit-length view direction (isometric-ish), position = centre + dir × dist
    threeCamera2.position.set(cx + dist*0.62, cy + dist*0.48, cz + dist*0.62);
    threeCamera2.updateProjectionMatrix();
    threeControls2.target.set(cx, cy, cz);
    threeControls2.update();
}}

function update3DColors2() {{
    const mode = document.getElementById('color-mode-2').value;
    const stageMode = get3DStageMode2();
    const stageLabel = stageMode === 'a1a3' ? 'A1-A3' : 'A1-A5';

    function pct(arr, p) {{
        return arr.length ? arr[Math.max(0, Math.floor(arr.length * p / 100) - 1)] : 0;
    }}

    const totalVals = geometryData2.map(g => getStageTotal2(g)).filter(v => v > 0).sort((a, b) => a - b);

    const totP25 = pct(totalVals, 25) || (stageMode === 'a1a3' ? 0.01 : 0.02);
    const totP50 = pct(totalVals, 50) || (stageMode === 'a1a3' ? 0.03 : 0.05);
    const totP75 = pct(totalVals, 75) || (stageMode === 'a1a3' ? 0.08 : 0.15);
    const totP90 = pct(totalVals, 90) || (stageMode === 'a1a3' ? 0.15 : 0.3);

    buildingMeshes2.forEach(bm => {{
        let c;
        if (mode === 'per_m3') c = emissionColor2(getStageIntensity2(bm.data));
        else if (mode === 'total') c = totalColor2(getStageTotal2(bm.data), totP25, totP50, totP75, totP90);
        else {{
            const dispCat = resolveCategory2(bm.data.cat, bm.data.name);
            c = new THREE.Color(CAT_COLORS_2[dispCat] || CAT_COLORS_2[bm.data.cat] || 0x94a3b8);
        }}
        bm.mesh.material.color.copy(c);
    }});
    // Update legend
    var leg = document.getElementById('ec-legend-2');
    if (leg) {{
        if (mode === 'total') {{
            const p25 = totP25.toFixed(2), p50 = totP50.toFixed(2), p75 = totP75.toFixed(2), p90 = totP90.toFixed(2);
            leg.innerHTML =
                '<div style="font-weight:600;font-size:0.72rem;margin-bottom:4px;">Total Emission (' + stageLabel + ') (tCO\u2082e)</div>' +
                '<div style="display:flex;flex-direction:column;gap:2px;font-size:0.7rem;">' +
                '<div style="display:flex;align-items:center;gap:4px;"><span style="display:inline-block;width:14px;height:10px;border-radius:2px;background:#4ade80;"></span>\u2264 ' + p25 + '</div>' +
                '<div style="display:flex;align-items:center;gap:4px;"><span style="display:inline-block;width:14px;height:10px;border-radius:2px;background:#a3e635;"></span>\u2264 ' + p50 + '</div>' +
                '<div style="display:flex;align-items:center;gap:4px;"><span style="display:inline-block;width:14px;height:10px;border-radius:2px;background:#fbbf24;"></span>\u2264 ' + p75 + '</div>' +
                '<div style="display:flex;align-items:center;gap:4px;"><span style="display:inline-block;width:14px;height:10px;border-radius:2px;background:#f97316;"></span>\u2264 ' + p90 + '</div>' +
                '<div style="display:flex;align-items:center;gap:4px;"><span style="display:inline-block;width:14px;height:10px;border-radius:2px;background:#ef4444;"></span>> ' + p90 + '</div>' +
                '<div style="display:flex;align-items:center;gap:4px;"><span style="display:inline-block;width:14px;height:10px;border-radius:2px;background:#94a3b8;"></span>No Data</div></div>';
        }} else if (mode === 'per_m3') {{
            let swatches = '', labels = '';
            for (let v = 400; v <= 700; v += 25) {{
                const c = _ecInterp(v);
                const hex = '#' + Math.round(c.r*255).toString(16).padStart(2,'0') + Math.round(c.g*255).toString(16).padStart(2,'0') + Math.round(c.b*255).toString(16).padStart(2,'0');
                swatches += '<span style="display:inline-block;width:10px;height:14px;background:' + hex + ';"></span>';
                const lbl = v === 400 ? '\u2264400' : v === 700 ? '\u2265700' : String(v);
                labels += '<span style="font-size:0.6rem;color:#888;writing-mode:vertical-rl;transform:rotate(180deg);height:28px;line-height:10px;">' + lbl + '</span>';
            }}
            leg.innerHTML =
                '<div style="font-weight:600;font-size:0.72rem;margin-bottom:4px;">Emission Intensity (' + stageLabel + ') (kgCO\u2082e/m\u00b3)</div>' +
                '<div style="display:flex;gap:0;margin-bottom:1px;">' + swatches + '</div>' +
                '<div style="display:flex;gap:0;margin-bottom:4px;">' + labels + '</div>' +
                '<div style="display:flex;align-items:center;gap:4px;font-size:0.7rem;"><span style="display:inline-block;width:14px;height:10px;border-radius:2px;background:#94a3b8;"></span>No EC Data</div>';
        }} else {{
            leg.innerHTML = '<div style="font-weight:600;font-size:0.72rem;margin-bottom:4px;">Colored by Category</div>';
        }}
    }}
}}

function update3DFilters2() {{
    const boxes = document.querySelectorAll('.cat-box-2');
    const visCats = new Set();
    boxes.forEach(b => {{ if (b.classList.contains('active')) visCats.add(b.dataset.cat); }});
    const selLevel = (document.getElementById('level-filter-3d') || {{}}).value || 'all';
    buildingMeshes2.forEach(bm => {{
        const dispCat = resolveCategory2(bm.data.cat, bm.data.name);
        const catOk = visCats.has(bm.data.cat) || visCats.has(dispCat);
        const elemLevel = (bm.data.level || '').trim();
        const lvOk = selLevel === 'all' || elemLevel === selLevel || elemLevel.toLowerCase() === selLevel.toLowerCase();
        bm.mesh.visible = catOk && lvOk;
    }});
}}

function update3DLevelFilter() {{
    update3DFilters2();
}}

function toggleAllCats2(show) {{
    document.querySelectorAll('.cat-box-2').forEach(b => {{
        if (show) b.classList.add('active'); else b.classList.remove('active');
    }});
    update3DFilters2();
}}

// ==========================================
// INIT — wait for ECharts CDN before drawing
// ==========================================
document.addEventListener('DOMContentLoaded', function() {{
    // Ensure analysis tab is visible
    var analysisTab = document.getElementById("analysis-tab");
    if (analysisTab) analysisTab.style.display = "block";

    // Poll until ECharts library is loaded (CDN may take a moment)
    var maxWait = 100;  // 100 × 100ms = 10 sec max
    function tryInit(attempt) {{
        if (typeof echarts !== 'undefined') {{
            initChartsForTab('analysis-tab');
            // Trigger a resize after a short delay to fix zero-width charts
            setTimeout(function() {{
                document.querySelectorAll('[id$="Chart"]').forEach(function(el) {{
                    var inst = echarts.getInstanceByDom(el);
                    if (inst) inst.resize();
                }});
            }}, 300);
        }} else if (attempt < maxWait) {{
            setTimeout(function() {{ tryInit(attempt + 1); }}, 100);
        }}
    }}
    tryInit(0);

    if (geometryData2.length > 0) {{
        function tryInit3D(attempt) {{
            if (typeof THREE !== 'undefined' && typeof THREE.OrbitControls !== 'undefined') {{
                init3DViewer2();
                update3DColors2();
            }} else if (attempt < maxWait) {{
                setTimeout(function() {{ tryInit3D(attempt + 1); }}, 100);
            }}
        }}
        tryInit3D(0);
    }}
}});
</script>
"""
