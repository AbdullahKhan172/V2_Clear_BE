"""
Professional Structural Embodied Carbon Report Generator (C.L.E.A.R.)
Generates A3 landscape Word documents with embedded charts.
Based on A1-A5 upfront embodied carbon calculations (subset of EN 15978) and the SCORS (IStructE) structural carbon benchmark.

All tables use WidthType.DXA. Content width is derived from page size and margins.
"""

import os
import io
import base64
import pandas as pd
import numpy as np

from calculations import (A5A_EMISSION_FACTOR_KGCO2E_PER_SQM as A5A_DEFAULT,
                          SEAI_CF_OUTWARD_ROAD, SEAI_CF_RETURN_ROAD, SEAI_CF_SEA)
from catalogue import SEAI_TRANSPORT_TABLE

try:
    from docx import Document
    from docx.shared import Pt, Emu, RGBColor, Twips
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
    from docx.enum.section import WD_ORIENT
    from docx.oxml.ns import nsdecls, qn
    from docx.oxml import parse_xml
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib import colors as mcolors
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

try:
    import trimesh
    TRIMESH_AVAILABLE = True
except ImportError:
    TRIMESH_AVAILABLE = False


# =============================================================================
# CONSTANTS
# =============================================================================

PAGE_WIDTH_DXA = 23820         # A3 landscape width (16.54 in)
PAGE_HEIGHT_DXA = 16830        # A3 landscape height (11.69 in)
PAGE_MARGIN_DXA = 720          # 0.5 in margins for compact professional layouts
CONTENT_WIDTH_DXA = PAGE_WIDTH_DXA - (2 * PAGE_MARGIN_DXA)
HALF_WIDTH_DXA = CONTENT_WIDTH_DXA // 2
HALF_CHART_WIDTH_DXA = min(HALF_WIDTH_DXA - 200, 8400)

if DOCX_AVAILABLE:
    COLORS = {
        'primary': RGBColor(31, 78, 121),
        'secondary': RGBColor(14, 116, 144),
        'accent': RGBColor(0, 168, 150),
        'dark': RGBColor(33, 37, 41),
        'gray': RGBColor(108, 117, 125),
        'light_gray': RGBColor(200, 200, 200),
        'white': RGBColor(255, 255, 255),
        'concrete': RGBColor(46, 134, 222),
        'steel': RGBColor(231, 76, 60),
        'pt': RGBColor(0, 168, 150),
        'warning': RGBColor(210, 74, 52),
        'success': RGBColor(39, 174, 96),
        'na_gray': RGBColor(153, 153, 153),
        'kpi_green': RGBColor(22, 163, 74),
        'kpi_blue': RGBColor(52, 152, 219),
    }

    MATERIAL_COLORS_RGB = {
        'Concrete': COLORS['concrete'],
        'Rebar': COLORS['steel'],
        'Steel Section': RGBColor(192, 57, 43),
        'Steel': COLORS['steel'],
        'Post Tensioning': COLORS['pt'],
        'Timber': RGBColor(22, 163, 74),
    }

    EFFICIENCY_BANDS = {
        'A++': {'range': '≤50', 'desc': 'Outstanding structural carbon', 'color': RGBColor(0, 122, 57)},
        'A+': {'range': '51–100', 'desc': 'Excellent structural carbon', 'color': RGBColor(45, 168, 79)},
        'A': {'range': '101–150', 'desc': 'Very good structural carbon', 'color': RGBColor(158, 207, 47)},
        'B': {'range': '151–200', 'desc': 'Good - SCORS target band', 'color': RGBColor(200, 180, 41)},
        'C': {'range': '201–250', 'desc': 'Moderate - around industry average', 'color': RGBColor(246, 190, 91)},
        'D': {'range': '251–300', 'desc': 'Improvement needed', 'color': RGBColor(240, 138, 47)},
        'E': {'range': '301–350', 'desc': 'Below average', 'color': RGBColor(226, 72, 42)},
        'F': {'range': '351–400', 'desc': 'Poor Performance', 'color': RGBColor(193, 31, 40)},
        'G': {'range': '>400', 'desc': 'Very Poor - Urgent action required', 'color': RGBColor(111, 13, 15)},
    }
else:
    COLORS = {}
    MATERIAL_COLORS_RGB = {}
    EFFICIENCY_BANDS = {}


CHART_COLORS = {
    'Concrete': '#2E86DE',
    'Rebar': '#E74C3C',
    'Steel Section': '#C0392B',
    'Steel': '#E74C3C',
    'Post Tensioning': '#00A896',
    'Timber': '#16A34A',
    'Other': '#8E9AAF',
}

CHART_BG = '#F8FBFF'
CHART_TITLE = '#1F4E79'

STAGE_CHART_COLORS = {
    'A1-A3': '#6C5CE7',
    'A4': '#00B4D8',
    'A5': '#FF9F1C',
}

TABLE_HEADER_HEX = '1F4E79'
ROW_STRIPE_HEX = 'F3F8FD'
ROW_HIGHLIGHT_HEX = 'E7F1FF'
ROW_TOTAL_HEX = 'E6F7F1'

ADV_DASH_CAT_COLORS = {
    'Column': '#5a7a9e',
    'Beam': '#9a6c38',
    'Slab/Floor': '#4e7d5e',
    'Wall': '#8a7060',
    'Foundation/Footing': '#5c5040',
    'Pile': '#4a5e50',
    'Roof': '#4d7080',
    'Stair': '#7a6080',
    'Reinforcement': '#6a5a50',
    'Post-Tensioning': '#4a6e6e',
    'Glazing/Curtain Wall': '#6a9ab4',
    'Glazing': '#6a9ab4',
    'Door': '#9a7840',
    'Railing': '#7f8c8d',
    'Ceiling': '#95a5a6',
    'Insulation': '#7d8fa3',
    'Parapet': '#8d6e63',
    'Other': '#7a8490',
}

REDUCTION_NOTES = {
    'Concrete': 'GGBS/PFA replacement, optimized strength specification',
    'Rebar': 'High recycled content (EAF), EPD-verified reinforcement',
    'Steel Section': 'Optimized section sizes and high-recycled-content section steel',
    'Steel': 'High recycled content (EAF), optimized sections',
    'Post Tensioning': 'Efficient strand layout, reduced concrete volume',
}

def _a5a_factor_of(data):
    """A5a site-activity factor (kgCO2e/m\u00b2 GIA) this run was calculated with.
    Prefers the value the engine recorded on the model; falls back to metrics,
    then the SEAI default. Report text must read this, never hard-code 28."""
    v = getattr(data, 'a5a_factor', None)
    if v is None:
        v = (getattr(data, 'metrics', None) or {}).get('a5a_factor')
    try:
        return float(v)
    except (TypeError, ValueError):
        return A5A_DEFAULT


def _a5a_is_override(data):
    return abs(_a5a_factor_of(data) - A5A_DEFAULT) > 1e-6


def _stage_drivers(data):
    """Key-driver text per stage. The A5 driver names the site-activity factor
    this assessment used, so an override is visible in the stage table itself."""
    _a5a = _a5a_factor_of(data)
    _src = 'user override' if _a5a_is_override(data) else 'per SEAI'
    return {
        'A1-A3': 'Cement production, steel manufacturing, aggregate extraction',
        'A4': 'Transport to site per SEAI (Table 2 distances, Table 3 factors); road out + empty return',
        'A5': (f'Construction activities (A5a: {_a5a:g} kgCO\u2082e/m\u00b2 {_src}), '
               f'material wastage, site energy'),
    }


# =============================================================================
# CHART GENERATION
# =============================================================================

def generate_material_pie_chart(summary_df, total_emission):
    if not MATPLOTLIB_AVAILABLE:
        return None
    fig, ax = plt.subplots(figsize=(4.2, 3.0), facecolor=CHART_BG)
    materials = summary_df['Material Type'].tolist()
    values = [round(v, 3) for v in summary_df['Total Emission(tCO2e)'].tolist()]
    colors = [CHART_COLORS.get(m, CHART_COLORS['Other']) for m in materials]
    wedges, texts, autotexts = ax.pie(
        values, labels=None,
        autopct=lambda pct: f'{pct:.1f}%' if pct > 4 else '',
        colors=colors, startangle=110,
        shadow=False, wedgeprops={'linewidth': 1.2, 'edgecolor': 'white', 'width': 0.45})
    for at in autotexts:
        at.set_fontsize(7)
        at.set_fontweight('bold')
        at.set_color('#23395B')
        ax.text(0, 0, f'{total_emission:.3f}\ntCO\u2082e', ha='center', va='center',
            fontsize=9, fontweight='bold', color=CHART_TITLE)
    legend_labels = [f'{m}: {v:.3f} tCO\u2082e ({v / total_emission * 100:.1f}%)'
                     if total_emission > 0 else f'{m}: {v:.3f} tCO\u2082e'
                     for m, v in zip(materials, values)]
    ax.legend(wedges, legend_labels, title="Materials", loc="center left",
              bbox_to_anchor=(1.02, 0.5), fontsize=6, title_fontsize=7, frameon=False)
    ax.set_title('Material Breakdown', fontsize=9, fontweight='bold', color=CHART_TITLE, pad=6)
    ax.set_facecolor(CHART_BG)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=180, bbox_inches='tight', facecolor=CHART_BG)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_lifecycle_pie_chart(stage_emissions):
    if not MATPLOTLIB_AVAILABLE:
        return None
    if not stage_emissions or sum(stage_emissions.values()) <= 0:
        return None
    fig, ax = plt.subplots(figsize=(4.2, 3.0), facecolor=CHART_BG)
    stages = list(stage_emissions.keys())
    values = list(stage_emissions.values())
    colors = [STAGE_CHART_COLORS.get(s, '#6B7280') for s in stages]
    total = sum(values)
    wedges, texts, autotexts = ax.pie(
        values, labels=None,
        autopct=lambda pct: f'{pct:.1f}%' if pct > 4 else '',
        colors=colors, startangle=105,
        shadow=False, wedgeprops={'linewidth': 1.2, 'edgecolor': 'white', 'width': 0.45})
    for at in autotexts:
        at.set_fontsize(7)
        at.set_fontweight('bold')
        at.set_color('#23395B')
        ax.text(0, 0, f'{total:.3f}\ntCO\u2082e', ha='center', va='center',
            fontsize=9, fontweight='bold', color=CHART_TITLE)
    stage_names = {'A1-A3': 'Product Stage', 'A4': 'Transport', 'A5': 'Construction'}
    legend_labels = [f'{stage_names.get(s, s)}: {v:.3f} tCO\u2082e' for s, v in zip(stages, values)]
    ax.legend(wedges, legend_labels, title="Stages", loc="center left",
              bbox_to_anchor=(1.02, 0.5), fontsize=6, title_fontsize=7, frameon=False)
    ax.set_title('Lifecycle Stages (A1-A5)', fontsize=9, fontweight='bold', color=CHART_TITLE, pad=6)
    ax.set_facecolor(CHART_BG)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=180, bbox_inches='tight', facecolor=CHART_BG)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_lifecycle_bar_chart(stage_emissions):
    if not MATPLOTLIB_AVAILABLE:
        return None
    fig, ax = plt.subplots(figsize=(6.2, 2.2), facecolor=CHART_BG)
    stages = list(stage_emissions.keys())
    values = list(stage_emissions.values())
    colors = [STAGE_CHART_COLORS.get(s, '#6B7280') for s in stages]
    total = sum(values)
    bars = ax.bar(stages, values, color=colors, edgecolor='white', linewidth=1.5)
    for bar, val in zip(bars, values):
        h = bar.get_height()
        pct = (val / total * 100) if total > 0 else 0
        ax.text(bar.get_x() + bar.get_width() / 2., h + 0.02 * max(values),
                f'{val:.3f}\n({pct:.1f}%)', ha='center', va='bottom',
                fontsize=7, fontweight='bold', color='#1F4E79')
    ax.set_ylabel('Emissions (tCO\u2082e)', fontsize=8, fontweight='bold')
    ax.set_title('Lifecycle Stage Emissions (A1-A5)', fontsize=9, fontweight='bold', color=CHART_TITLE, pad=6)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#AFC7E0')
    ax.spines['bottom'].set_color('#AFC7E0')
    ax.set_ylim(0, max(values) * 1.35)
    ax.yaxis.grid(True, linestyle='--', alpha=0.3, color='#BFD7EA')
    ax.tick_params(labelsize=7)
    ax.set_facecolor(CHART_BG)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=180, bbox_inches='tight', facecolor=CHART_BG)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_rating_scale_chart(current_rating, per_sqm):
    if not MATPLOTLIB_AVAILABLE:
        return None
    fig, ax = plt.subplots(figsize=(6.2, 1.6), facecolor='white')
    ratings = ['A++', 'A+', 'A', 'B', 'C', 'D', 'E', 'F', 'G']
    clrs = ['#007A39', '#2DA84F', '#9ECF2F', '#C8B429', '#F6D25B', '#F08A2F', '#E2482A', '#C11F28', '#6F0D0F']
    for i, (r, c) in enumerate(zip(ratings, clrs)):
        alpha = 1.0 if r == current_rating else 0.4
        ax.barh(0, 1, left=i, color=c, alpha=alpha, edgecolor='white', linewidth=2)
        fw = 'bold' if r == current_rating else 'normal'
        fs = 11 if r == current_rating else 8
        ax.text(i + 0.5, 0, r, ha='center', va='center', fontsize=fs, fontweight=fw, color='white')
    idx = ratings.index(current_rating) if current_rating in ratings else 4
    ax.annotate(f'Your Project: {per_sqm:.3f} kgCO\u2082e/m\u00b2',
                xy=(idx + 0.5, 0.55), xytext=(idx + 0.5, 1.1),
                ha='center', fontsize=8, fontweight='bold', color='#1F4E79',
                arrowprops=dict(arrowstyle='->', color='#1F4E79', lw=2))
    ax.set_xlim(0, 9)
    ax.set_ylim(-0.6, 1.5)
    ax.axis('off')
    ax.set_title('SCORS Efficiency Rating Scale', fontsize=10, fontweight='bold', color='#1F4E79', pad=8)
    ax.text(0.5, -0.45, '\u226450', ha='center', fontsize=6, color='#666666')
    ax.text(4.5, -0.45, '201\u2013250', ha='center', fontsize=6, color='#666666')
    ax.text(8.5, -0.45, '>400', ha='center', fontsize=6, color='#666666')
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=180, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    buf.seek(0)
    return buf


def _strip_instance_id(name):
    """Strip trailing Revit-style instance ids from names."""
    if not name or ':' not in name:
        return name
    parts = name.rsplit(':', 1)
    if len(parts) == 2 and parts[1].strip().isdigit():
        return parts[0]
    return name


def _prepare_ifc_emission_points(geometry_data, detailed_df):
    """Attach total emissions (tCO2e) and geometry for IFC preview rendering."""
    if not geometry_data:
        return []

    total_map = {}
    vol_map = {}
    cat_total = {}
    cat_vol = {}

    if detailed_df is not None and not detailed_df.empty:
        for _, row in detailed_df.iterrows():
            desc = str(row.get('Description', ''))
            base = desc.rsplit(' - ', 1)[0].strip() if ' - ' in desc else desc
            base = _strip_instance_id(base)

            total = float(row.get('Total Emission(tCO2e)', 0) or 0)
            vol = float(row.get('Total Volume(m3)', 0) or 0)
            cat = str(row.get('Category', 'Other') or 'Other')

            total_map[base] = total_map.get(base, 0) + total
            vol_map[base] = vol_map.get(base, 0) + vol

            cat_total[cat] = cat_total.get(cat, 0) + total
            cat_vol[cat] = cat_vol.get(cat, 0) + vol

    name_counts = {}
    cat_counts = {}
    normalized_names = []
    for g in geometry_data:
        name = _strip_instance_id(str(g.get('name', '')))
        cat = str(g.get('category', 'Other') or 'Other')
        normalized_names.append(name)
        name_counts[name] = name_counts.get(name, 0) + 1
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    points = []
    for i, g in enumerate(geometry_data):
        name = normalized_names[i]
        cat = str(g.get('category', 'Other') or 'Other')

        count = max(name_counts.get(name, 1), 1)
        total = total_map.get(name, 0) / count
        vol = vol_map.get(name, 0) / count

        if total <= 0 and cat in cat_total:
            cat_count = max(cat_counts.get(cat, 1), 1)
            total = cat_total.get(cat, 0) / cat_count
            vol = cat_vol.get(cat, 0) / cat_count if cat_vol.get(cat, 0) > 0 else vol

        intensity = (total * 1000 / vol) if vol > 0 else 0

        w = max(float(g.get('width', 0.25) or 0.25), 0.05)
        d = max(float(g.get('depth', 0.25) or 0.25), 0.05)
        h = max(float(g.get('height', 0.25) or 0.25), 0.05)

        vertices = g.get('vertices')
        triangles = g.get('triangles')
        has_mesh = bool(vertices and triangles and len(vertices) >= 3 and len(triangles) >= 1)

        points.append({
            'x': float(g.get('x', 0) or 0),
            'y': float(g.get('y', 0) or 0),
            'z': float(g.get('z', 0) or 0),
            'w': w,
            'd': d,
            'h': h,
            'name': str(g.get('name', '') or ''),
            'category': cat,
            'total': max(float(total), 0),
            'intensity': max(float(intensity), 0),
            'vertices': vertices if has_mesh else None,
            'triangles': triangles if has_mesh else None,
        })

    return points


def _resolve_display_category(category, element_name=''):
    """Mirror Advanced Dashboard category resolution for consistent IFC coloring."""
    name = str(element_name or '').lower()
    if any(k in name for k in ['glaz', 'curtain', 'cladding', 'facade', 'glazed', 'window']):
        return 'Glazing'
    if 'door' in name:
        return 'Door'
    if any(k in name for k in ['railing', 'balustrade', 'handrail']):
        return 'Railing'
    if any(k in name for k in ['ceiling', 'soffit']):
        return 'Ceiling'
    if 'insul' in name:
        return 'Insulation'
    if 'parapet' in name:
        return 'Parapet'
    return str(category or 'Other')


def _category_hex_color(display_category, raw_category='Other'):
    """Pick category color using dashboard palette with robust fallback."""
    if display_category in ADV_DASH_CAT_COLORS:
        return ADV_DASH_CAT_COLORS[display_category]
    if raw_category in ADV_DASH_CAT_COLORS:
        return ADV_DASH_CAT_COLORS[raw_category]
    return ADV_DASH_CAT_COLORS['Other']


def _box_faces_from_origin(x, y, z, w, d, h):
    """Create 6 rectangular faces from origin corner (x,y,z) with extents (w,d,h)."""
    x0, x1 = x, x + w
    y0, y1 = y, y + d
    z0, z1 = z, z + h
    return [
        [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)],
        [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)],
        [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)],
        [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)],
        [(x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1)],
        [(x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1)],
    ]


def _mesh_faces(vertices, triangles, max_tris=1800):
    """Return mesh triangle faces with light decimation to preserve IFC fidelity."""
    verts = np.array(vertices, dtype=float)
    tris = list(triangles)
    if max_tris and len(tris) > max_tris:
        # Evenly sample across full triangle list to reduce aliasing artifacts.
        sample_idx = np.linspace(0, len(tris) - 1, num=max_tris, dtype=int)
        tris = [tris[i] for i in sample_idx]

    faces = []
    for tri in tris:
        if len(tri) < 3:
            continue
        i0, i1, i2 = int(tri[0]), int(tri[1]), int(tri[2])
        if max(i0, i1, i2) < len(verts):
            faces.append([verts[i0], verts[i1], verts[i2]])
    return faces


def generate_ifc_preview_chart(geometry_data, detailed_df=None, model_name='IFC Model'):
    """Generate simple 3D IFC preview colored by category."""
    if not MATPLOTLIB_AVAILABLE or not geometry_data:
        return None, None

    points = _prepare_ifc_emission_points(geometry_data, detailed_df)
    if not points:
        return None, None

    totals = [p['total'] for p in points if p['total'] > 0]

    fig = plt.figure(figsize=(7.2, 4.2), facecolor='white')
    ax = fig.add_subplot(111, projection='3d')

    mins = np.array([np.inf, np.inf, np.inf])
    maxs = np.array([-np.inf, -np.inf, -np.inf])

    for p in points:
        display_cat = _resolve_display_category(p['category'], p['name'])
        color = _category_hex_color(display_cat, p['category'])

        faces = None
        if p.get('vertices') is not None and p.get('triangles') is not None:
            faces = _mesh_faces(p['vertices'], p['triangles'])
            if faces:
                verts = np.array(p['vertices'], dtype=float)
                mins = np.minimum(mins, verts.min(axis=0))
                maxs = np.maximum(maxs, verts.max(axis=0))

        if not faces:
            faces = _box_faces_from_origin(p['x'], p['y'], p['z'], p['w'], p['d'], p['h'])
            mins = np.minimum(mins, np.array([p['x'], p['y'], p['z']]))
            maxs = np.maximum(maxs, np.array([p['x'] + p['w'], p['y'] + p['d'], p['z'] + p['h']]))

        poly = Poly3DCollection(faces, linewidths=0.15, edgecolor=(0, 0, 0, 0.1))
        poly.set_facecolor(color)
        poly.set_alpha(0.55)
        ax.add_collection3d(poly)

    if np.isfinite(mins).all() and np.isfinite(maxs).all():
        span = np.maximum(maxs - mins, 1.0)
        margin = span.max() * 0.06
        ax.set_xlim(mins[0] - margin, maxs[0] + margin)
        ax.set_ylim(mins[1] - margin, maxs[1] + margin)
        ax.set_zlim(mins[2] - margin, maxs[2] + margin)

    ax.set_title(f'{model_name}', fontsize=9, fontweight='bold', color=CHART_TITLE, pad=8)
    ax.view_init(elev=22, azim=35)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_zticks([])
    ax.grid(False)
    ax.set_axis_off()
    ax.set_facecolor('white')

    present_categories = sorted({_resolve_display_category(p['category'], p['name']) for p in points})
    legend_patches = [mpatches.Patch(color=_category_hex_color(c, c), label=c) for c in present_categories[:8]]
    ax.legend(handles=legend_patches, loc='upper left', bbox_to_anchor=(1.02, 1.0),
              fontsize=6, frameon=False, title='Categories', title_fontsize=7)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=180, bbox_inches='tight', facecolor=CHART_BG)
    plt.close(fig)
    buf.seek(0)

    summary = {
        'min_total': min(totals) if totals else 0.0,
        'avg_total': (sum(totals) / len(totals)) if totals else 0.0,
        'max_total': max(totals) if totals else 0.0,
        'elements': len(points),
        'category_count': len({_resolve_display_category(p['category'], p['name']) for p in points}),
    }
    return buf, summary


def _extract_glb_mesh_faces(model_base64, max_faces=2200):
    """Decode a GLB and return triangle faces suitable for matplotlib Poly3DCollection."""
    if not TRIMESH_AVAILABLE or not model_base64:
        return None, None, None

    try:
        raw = base64.b64decode(model_base64)
        scene_or_mesh = trimesh.load(io.BytesIO(raw), file_type='glb', force='scene')

        meshes = []
        if isinstance(scene_or_mesh, trimesh.Trimesh):
            meshes = [scene_or_mesh]
        elif hasattr(scene_or_mesh, 'geometry'):
            meshes = [m for m in scene_or_mesh.geometry.values() if isinstance(m, trimesh.Trimesh)]

        if not meshes:
            return None, None, None

        merged = trimesh.util.concatenate(meshes) if len(meshes) > 1 else meshes[0]
        if merged.vertices is None or merged.faces is None or len(merged.faces) == 0:
            return None, None, None

        vertices = np.asarray(merged.vertices, dtype=float)
        faces_idx = np.asarray(merged.faces, dtype=int)

        if max_faces and len(faces_idx) > max_faces:
            sample_idx = np.linspace(0, len(faces_idx) - 1, num=max_faces, dtype=int)
            faces_idx = faces_idx[sample_idx]

        faces = vertices[faces_idx]
        mins = vertices.min(axis=0)
        maxs = vertices.max(axis=0)
        return faces, mins, maxs
    except Exception:
        return None, None, None


def generate_glb_preview_chart(model_base64, model_name='GLB Model'):
    """Generate a static 3D preview image from GLB geometry for Word reports."""
    if not MATPLOTLIB_AVAILABLE or not model_base64:
        return None, None

    faces, mins, maxs = _extract_glb_mesh_faces(model_base64)
    if faces is None:
        return None, None

    fig = plt.figure(figsize=(7.2, 4.2), facecolor=CHART_BG)
    ax = fig.add_subplot(111, projection='3d')

    mesh = Poly3DCollection(faces, linewidths=0.0, edgecolor='none')
    mesh.set_facecolor('#7faacc')
    mesh.set_alpha(0.96)
    ax.add_collection3d(mesh)

    span = np.maximum(maxs - mins, 1.0)
    margin = span.max() * 0.06
    ax.set_xlim(mins[0] - margin, maxs[0] + margin)
    ax.set_ylim(mins[1] - margin, maxs[1] + margin)
    ax.set_zlim(mins[2] - margin, maxs[2] + margin)

    ax.set_title(f'{model_name} - 3D GLB View', fontsize=9, fontweight='bold', color=CHART_TITLE, pad=8)
    ax.view_init(elev=20, azim=35)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_zticks([])
    ax.grid(False)
    ax.set_axis_off()
    ax.set_facecolor('white')

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=190, bbox_inches='tight', facecolor=CHART_BG)
    plt.close(fig)
    buf.seek(0)

    summary = {
        'faces': int(len(faces)),
        'vertices_est': int(len(faces) * 3),
    }
    return buf, summary


def _add_ifc_model_preview_section(doc, data, **_kwargs):
    """Add IFC 3D model preview with emission summary alongside."""
    model_type = str(getattr(data, 'model_type', 'none')).lower()
    geometry_data = getattr(data, 'geometry_data', []) or []
    if model_type != 'ifc' or not geometry_data:
        return

    preview, _stats = generate_ifc_preview_chart(
        geometry_data,
        detailed_df=getattr(data, 'detailed_df', None),
        model_name=data.project_info.get('project_name', 'IFC Model'),
    )
    if not preview:
        return

    metrics = data.metrics
    stage_em = data.stage_emissions or {}
    summary_df = data.summary_df

    _add_section_header(doc, 'IFC MODEL & EMISSION SUMMARY')

    # 2-column layout: model left, summary right
    layout = doc.add_table(rows=1, cols=2)
    _setup_table(layout, [HALF_WIDTH_DXA + 500, HALF_WIDTH_DXA - 500], border_color='FFFFFF')

    # Left: 3D model image
    left = layout.rows[0].cells[0]
    img_p = left.paragraphs[0]
    img_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    img_p.add_run().add_picture(preview, width=Twips(HALF_WIDTH_DXA))

    # Right: emission summary
    right = layout.rows[0].cells[1]

    # Total Emissions
    _add_run(right.paragraphs[0], 'TOTAL EMISSIONS', size=7, bold=True, color=COLORS['gray'])
    p_total = right.add_paragraph()
    _add_run(p_total, f"{metrics['total_emission_ton']:.3f} tCO\u2082e", size=14, bold=True, color=COLORS['kpi_green'])

    # Per m²
    _add_run(right.add_paragraph(), 'CARBON INTENSITY', size=7, bold=True, color=COLORS['gray'])
    p_sqm = right.add_paragraph()
    _add_run(p_sqm, f"{metrics['total_emission_per_sqm']:.1f} kgCO\u2082e/m\u00b2", size=12, bold=True, color=COLORS['kpi_green'])

    # A1-A3 breakdown
    a1a3 = stage_em.get('A1-A3', 0)
    a4 = stage_em.get('A4', 0)
    a5 = stage_em.get('A5', 0)
    _add_run(right.add_paragraph(), '', size=4)  # spacer
    _add_run(right.add_paragraph(), 'LIFECYCLE STAGES', size=7, bold=True, color=COLORS['gray'])

    stage_data = [('A1-A3 (Product)', a1a3), ('A4 (Transport)', a4), ('A5 (Construction)', a5)]
    total_stage = sum(v for _, v in stage_data) or 1
    for label, val in stage_data:
        p = right.add_paragraph()
        pct = val / total_stage * 100
        _add_run(p, f'{label}: ', size=8, color=COLORS['dark'])
        _add_run(p, f'{val:.3f} tCO\u2082e ({pct:.0f}%)', size=8, bold=True, color=COLORS['primary'])

    # Material breakdown
    _add_run(right.add_paragraph(), '', size=4)  # spacer
    _add_run(right.add_paragraph(), 'MATERIALS', size=7, bold=True, color=COLORS['gray'])

    if summary_df is not None and not summary_df.empty:
        total_em = metrics['total_emission_ton'] or 1
        for _, row in summary_df.iterrows():
            mat = str(row.get('Material', ''))
            em = float(row.get('Total Emission(tCO2e)', 0) or 0)
            if em <= 0:
                continue
            pct = em / total_em * 100
            mat_color = MATERIAL_COLORS_RGB.get(mat, COLORS['dark'])
            p = right.add_paragraph()
            _add_run(p, f'{mat}: ', size=8, color=mat_color)
            _add_run(p, f'{em:.3f} tCO\u2082e ({pct:.0f}%)', size=8, bold=True, color=COLORS['dark'])


def _add_glb_model_preview_section(doc, data, large=True):
    """Add GLB model preview section to Word using static 3D snapshot."""
    model_type = str(getattr(data, 'model_type', 'none')).lower()
    model_base64 = getattr(data, 'model_base64', '')
    if model_type != 'glb' or not model_base64:
        return

    _add_section_header(doc, 'GLB MODEL PREVIEW')

    preview, preview_stats = generate_glb_preview_chart(
        model_base64,
        model_name=data.project_info.get('project_name', 'GLB Model'),
    )

    if preview:
        img_para = doc.add_paragraph()
        img_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        target_width = int((CONTENT_WIDTH_DXA - 1400) * 0.90) if large else int(min(HALF_WIDTH_DXA + 700, 9800) * 0.90)
        img_para.add_run().add_picture(preview, width=Twips(target_width))

    caption_table = doc.add_table(rows=1, cols=1)
    _setup_table(
        caption_table,
        [1],
        border_color='D9E2EC',
        body_height_dxa=220,
        header_height_dxa=220,
    )

    cap = caption_table.rows[0].cells[0]
    _add_run(cap.paragraphs[0], 'MODEL SNAPSHOT DETAILS', size=9, bold=True, color=COLORS['secondary'])
    _add_run(cap.add_paragraph(), 'Model type: GLB/GLTF', size=8, color=COLORS['dark'])

    if preview_stats:
        _add_run(cap.add_paragraph(), f"Rendered faces: {preview_stats['faces']:,}", size=8, color=COLORS['gray'])
        _add_run(cap.add_paragraph(), f"Approx. vertices shown: {preview_stats['vertices_est']:,}", size=8, color=COLORS['gray'])
        _add_run(cap.add_paragraph(), 'Snapshot generated from uploaded GLB geometry.', size=8, italic=True, color=COLORS['gray'])
    else:
        _add_run(
            cap.add_paragraph(),
            'GLB preview unavailable in this runtime. Install trimesh to render GLB snapshots in Word.',
            size=8,
            italic=True,
            color=COLORS['warning'],
        )


# =============================================================================
# DOCX HELPER FUNCTIONS
# =============================================================================

_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def _set_cell_width_dxa(cell, width_dxa):
    """Set cell width using DXA units directly."""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    for existing in tcPr.findall(qn('w:tcW')):
        tcPr.remove(existing)
    tcW = parse_xml(f'<w:tcW {nsdecls("w")} w:w="{width_dxa}" w:type="dxa"/>')
    tcPr.append(tcW)


def _set_table_width_dxa(table, width_dxa):
    """Set table-level width using DXA."""
    tbl = table._tbl
    tblPr = tbl.tblPr
    if tblPr is None:
        tblPr = parse_xml(f'<w:tblPr {nsdecls("w")}/>')
        tbl.insert(0, tblPr)
    for existing in tblPr.findall(qn('w:tblW')):
        tblPr.remove(existing)
    tblW = parse_xml(f'<w:tblW {nsdecls("w")} w:w="{width_dxa}" w:type="dxa"/>')
    tblPr.append(tblW)


def _set_table_layout_fixed(table):
    """Fix table layout to exact widths."""
    tblPr = table._tbl.tblPr
    if tblPr is None:
        tblPr = parse_xml(f'<w:tblPr {nsdecls("w")}/>')
        table._tbl.insert(0, tblPr)
    for existing in tblPr.findall(qn('w:tblLayout')):
        tblPr.remove(existing)
    layout = parse_xml(f'<w:tblLayout {nsdecls("w")} w:type="fixed"/>')
    tblPr.append(layout)


def _set_col_widths_dxa(table, widths_dxa):
    """Set exact column widths in DXA on every cell."""
    for col_idx, w in enumerate(widths_dxa):
        for row in table.rows:
            if col_idx < len(row.cells):
                _set_cell_width_dxa(row.cells[col_idx], w)


def _normalize_widths(widths_dxa, target_total=CONTENT_WIDTH_DXA):
    """Scale input column widths to match target table width exactly."""
    if not widths_dxa:
        return []

    current_total = sum(widths_dxa)
    if current_total <= 0:
        base = target_total // len(widths_dxa)
        widths = [base] * len(widths_dxa)
        widths[-1] += target_total - sum(widths)
        return widths

    if current_total == target_total:
        return list(widths_dxa)

    scaled = [max(1, int(round((w / current_total) * target_total))) for w in widths_dxa]
    delta = target_total - sum(scaled)
    scaled[-1] += delta
    return scaled


def _set_row_min_height(row, height_dxa):
    """Set minimum row height in DXA while allowing expansion for long content."""
    trPr = row._tr.get_or_add_trPr()
    for existing in trPr.findall(qn('w:trHeight')):
        trPr.remove(existing)
    tr_h = parse_xml(f'<w:trHeight {nsdecls("w")} w:val="{height_dxa}" w:hRule="atLeast"/>')
    trPr.append(tr_h)


def _set_table_row_heights(table, body_height_dxa=300, header_height_dxa=420):
    """Apply consistent row heights for cleaner table rhythm."""
    for idx, row in enumerate(table.rows):
        _set_row_min_height(row, header_height_dxa if idx == 0 else body_height_dxa)


def _align_table_columns(table, numeric_cols=None, center_cols=None):
    """Align table columns for readability: numbers right, categories centered."""
    numeric_cols = set(numeric_cols or [])
    center_cols = set(center_cols or [])

    for row_idx, row in enumerate(table.rows):
        for col_idx, cell in enumerate(row.cells):
            if row_idx == 0:
                target_alignment = WD_ALIGN_PARAGRAPH.CENTER
            elif col_idx in numeric_cols:
                target_alignment = WD_ALIGN_PARAGRAPH.RIGHT
            elif col_idx in center_cols:
                target_alignment = WD_ALIGN_PARAGRAPH.CENTER
            else:
                target_alignment = WD_ALIGN_PARAGRAPH.LEFT

            for para in cell.paragraphs:
                para.alignment = target_alignment


def _setup_table(
    table,
    widths_dxa,
    border_color="CCCCCC",
    target_total=CONTENT_WIDTH_DXA,
    body_height_dxa=300,
    header_height_dxa=420,
):
    """Full table setup: width, layout, column widths, borders."""
    widths_dxa = _normalize_widths(widths_dxa, target_total=target_total)
    total = sum(widths_dxa)
    _set_table_width_dxa(table, total)
    _set_table_layout_fixed(table)
    _set_col_widths_dxa(table, widths_dxa)
    _set_table_borders(table, border_color)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    for row in table.rows:
        for cell in row.cells:
            _set_cell_margins(cell)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP

    _set_table_row_heights(table, body_height_dxa=body_height_dxa, header_height_dxa=header_height_dxa)

    table.autofit = False


def _set_table_borders(table, color="D9E2EC"):
    """Set table borders."""
    tblPr = table._tbl.tblPr
    if tblPr is None:
        tblPr = parse_xml(f'<w:tblPr {nsdecls("w")}/>')
        table._tbl.insert(0, tblPr)
    for existing in tblPr.findall(qn('w:tblBorders')):
        tblPr.remove(existing)
    borders = parse_xml(
        f'<w:tblBorders {nsdecls("w")}>'
        f'<w:top w:val="single" w:sz="2" w:color="{color}"/>'
        f'<w:left w:val="single" w:sz="2" w:color="{color}"/>'
        f'<w:bottom w:val="single" w:sz="2" w:color="{color}"/>'
        f'<w:right w:val="single" w:sz="2" w:color="{color}"/>'
        f'<w:insideH w:val="single" w:sz="2" w:color="{color}"/>'
        f'<w:insideV w:val="single" w:sz="2" w:color="{color}"/>'
        f'</w:tblBorders>')
    tblPr.append(borders)


def _set_cell_shading(cell, hex_color):
    """Set cell background using ShadingType.CLEAR (w:val='clear')."""
    tcPr = cell._tc.get_or_add_tcPr()
    for existing in tcPr.findall(qn('w:shd')):
        tcPr.remove(existing)
    shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{hex_color}" w:val="clear"/>')
    tcPr.append(shading)


def _set_cell_margins(cell, top=40, bottom=40, left=90, right=90):
    """Set cell margins in DXA."""
    tcPr = cell._tc.get_or_add_tcPr()
    for existing in tcPr.findall(qn('w:tcMar')):
        tcPr.remove(existing)
    mar = parse_xml(
        f'<w:tcMar {nsdecls("w")}>'
        f'<w:top w:w="{top}" w:type="dxa"/>'
        f'<w:bottom w:w="{bottom}" w:type="dxa"/>'
        f'<w:left w:w="{left}" w:type="dxa"/>'
        f'<w:right w:w="{right}" w:type="dxa"/>'
        f'</w:tcMar>')
    tcPr.append(mar)


def _add_ruled_line(doc):
    """Add a horizontal rule as a paragraph with bottom border (replaces Unicode dividers)."""
    para = doc.add_paragraph()
    para.paragraph_format.space_before = Pt(0)
    para.paragraph_format.space_after = Pt(1)
    pPr = para._p.get_or_add_pPr()
    pBdr = parse_xml(
        f'<w:pBdr {nsdecls("w")}>'
        f'<w:bottom w:val="single" w:sz="3" w:color="8AAED1" w:space="1"/>'
        f'</w:pBdr>')
    pPr.append(pBdr)
    return para


def _add_section_header(doc, text):
    """Add section header with proper ruled line divider."""
    para = doc.add_paragraph()
    run = para.add_run(text)
    run.font.name = 'Calibri'
    run.font.size = Pt(12)
    run.font.bold = True
    run.font.color.rgb = COLORS['primary']
    para.paragraph_format.space_before = Pt(2)
    para.paragraph_format.space_after = Pt(0)
    _add_ruled_line(doc)


def _add_run(para, text, size=8, bold=False, color=None, italic=False):
    """Helper to add a styled run."""
    run = para.add_run(text)
    run.font.name = 'Calibri'
    run.font.size = Pt(size)
    run.font.bold = bold
    if italic:
        run.font.italic = True
    if color:
        run.font.color.rgb = color

    para.paragraph_format.space_before = Pt(0)
    para.paragraph_format.space_after = Pt(1)
    para.paragraph_format.line_spacing = 1.0

    return run


def _configure_document_style(doc):
    """Apply consistent typography/spacing defaults for a clean layout."""
    normal = doc.styles['Normal']
    normal.font.name = 'Calibri'
    normal.font.size = Pt(9)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(1)
    normal.paragraph_format.line_spacing = 1.0


def _field_value(val):
    """Return (text, is_empty) for a project field value."""
    if val is None or str(val).strip() in ('', 'N/A', 'None', 'nan'):
        return ('Not specified', True)
    return (str(val), False)


def _impact_color(pct_value):
    """Return semantic color for impact share percentages."""
    if pct_value >= 45:
        return COLORS['warning']
    if pct_value >= 20:
        return COLORS['secondary']
    return COLORS['primary']


def _build_member_summary(detailed_df, project_area):
    """Build a summary DataFrame grouped by structural member type (from excel_report logic)."""
    if detailed_df is None or detailed_df.empty:
        return pd.DataFrame()

    from reports.excel_report import _classify_member_type

    df = detailed_df.copy()
    df['_member_type'] = df.apply(_classify_member_type, axis=1)

    for c in ['Mass(kg)', 'Total Volume(m3)', 'A1-A3 Emission(kgCO2e)',
              'A4 Emission(kgCO2e)', 'A5 Emission(kgCO2e)',
              'Total Emission(tCO2e)']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)

    grouped = df.groupby('_member_type', sort=False).agg(
        total_vol=('Total Volume(m3)', 'sum'),
        total_mass=('Mass(kg)', 'sum'),
        a1_a3=('A1-A3 Emission(kgCO2e)', 'sum'),
        a4=('A4 Emission(kgCO2e)', 'sum'),
        a5=('A5 Emission(kgCO2e)', 'sum'),
        total_ton=('Total Emission(tCO2e)', 'sum'),
    ).reset_index()

    grand_total = grouped['total_ton'].sum()
    grouped['pct'] = grouped['total_ton'].apply(
        lambda x: (x / grand_total * 100) if grand_total > 0 else 0)
    grouped['per_sqm'] = grouped['total_ton'].apply(
        lambda x: (x * 1000 / project_area) if project_area > 0 else 0)

    for c in ['a1_a3', 'a4', 'a5']:
        grouped[c] = grouped[c] / 1000

    grouped = grouped.sort_values('total_ton', ascending=False).reset_index(drop=True)
    grouped.columns = [
        'Member Type', 'Total Volume (m\u00b3)', 'Total Mass (kg)',
        'A1-A3 (tCO2e)', 'A4 (tCO2e)', 'A5 (tCO2e)',
        'Total (tCO2e)', '% of Total', 'Per m\u00b2 (kgCO2e/m\u00b2)']
    return grouped


# =============================================================================
# MAIN REPORT GENERATION
# =============================================================================

def generate_word_report(data, output_file, compact_mode=True):
    """Generate professional structural embodied carbon report.

    compact_mode=True produces a denser continuous-flow report with fewer forced page breaks.
    """
    if not DOCX_AVAILABLE:
        print("Warning: python-docx not installed. Install with: pip install python-docx")
        return None

    output_dir = os.path.dirname(output_file)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)

    doc = Document()
    _configure_document_style(doc)

    # A3 landscape with compact margins for better content density
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width = Twips(PAGE_WIDTH_DXA)
    section.page_height = Twips(PAGE_HEIGHT_DXA)
    section.left_margin = Twips(PAGE_MARGIN_DXA)
    section.right_margin = Twips(PAGE_MARGIN_DXA)
    section.top_margin = Twips(PAGE_MARGIN_DXA)
    section.bottom_margin = Twips(PAGE_MARGIN_DXA)

    _add_page1_cover(doc, data)
    if not compact_mode:
        doc.add_page_break()
    _add_page2_analysis(doc, data)
    if not compact_mode:
        doc.add_page_break()
    _add_page3_benchmarks_and_quantities(doc, data)
    if not compact_mode:
        doc.add_page_break()
    _add_page4_methodology_and_summary(doc, data)

    doc.save(output_file)
    print(f"Word report generated: {output_file}")
    return output_file


# =============================================================================
# PAGE 1: COVER + PROJECT INFO + CHARTS
# =============================================================================

def _add_page1_cover(doc, data):
    project_info = data.project_info
    metrics = data.metrics
    rating = data.efficiency_rating

    # Title row: title + rating badge
    title_table = doc.add_table(rows=1, cols=2)
    _setup_table(title_table, [6500, 2860], border_color='FFFFFF')

    cell_title = title_table.rows[0].cells[0]
    p_title = cell_title.paragraphs[0]
    _add_run(p_title, 'STRUCTURAL EMBODIED CARBON ASSESSMENT', size=18, bold=True, color=COLORS['primary'])
    p_sub = cell_title.add_paragraph()
    _add_run(p_sub, 'C.L.E.A.R. - Carbon Lifecycle Evaluation and Reporting', size=8, color=COLORS['gray'])

    cell_badge = title_table.rows[0].cells[1]
    p_badge = cell_badge.paragraphs[0]
    p_badge.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _add_run(p_badge, 'EFFICIENCY RATING', size=8, color=COLORS['gray'])
    p_badge2 = cell_badge.add_paragraph()
    p_badge2.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    band_info = EFFICIENCY_BANDS.get(rating, {})
    _add_run(p_badge2, f' {rating} ', size=28, bold=True, color=band_info.get('color', COLORS['gray']))
    p_badge3 = cell_badge.add_paragraph()
    p_badge3.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _add_run(p_badge3, band_info.get('desc', ''), size=7, color=COLORS['gray'])

    # Project Info Table: 2 rows × 6 cols
    # Widths: [1400, 1400, 1560, 1400, 1400, 2200] = 9360
    info_widths = [1400, 1400, 1560, 1400, 1400, 2200]
    info_table = doc.add_table(rows=2, cols=6)
    _setup_table(info_table, info_widths)

    # Stage badge shown with the stage so the report's confidence level is
    # explicit (e.g. "Tender \u2014 Tender assessment \u00b15%").
    _stage_val = project_info.get('project_stage')
    _badge = project_info.get('stage_badge')
    _unc = project_info.get('uncertainty_pct')
    if _stage_val and _badge:
        _stage_val = f"{_stage_val} \u2014 {_badge}" + (f" \u00b1{_unc:g}%" if _unc else "")

    info_data_row1 = [
        ('PROJECT', project_info.get('project_name')),
        ('CLIENT', project_info.get('client_name')),
        ('LOCATION', project_info.get('location')),
        ('TYPE', project_info.get('project_type')),
        ('STAGE', _stage_val),
        ('AREA', f"{data.project_area:,.1f} m\u00b2"),
    ]

    for i, (label, raw_val) in enumerate(info_data_row1):
        cell = info_table.rows[0].cells[i]
        _set_cell_shading(cell, 'F8F9FA')
        _set_cell_margins(cell)
        p = cell.paragraphs[0]
        _add_run(p, label, size=6, color=COLORS['gray'])
        p2 = cell.add_paragraph()
        val_text, is_empty = _field_value(raw_val)
        if is_empty:
            _add_run(p2, val_text, size=8, italic=True, color=COLORS['na_gray'])
        else:
            _add_run(p2, val_text, size=8, bold=True, color=COLORS['dark'])

    _psqm = metrics['total_emission_per_sqm']
    _intensity_txt = f"{_psqm:.3f} kgCO\u2082e/m\u00b2"
    if _unc:
        try:
            _u = float(_unc)
            _intensity_txt += (f" ({_psqm * (1 - _u / 100):.0f}\u2013"
                               f"{_psqm * (1 + _u / 100):.0f} expected)")
        except (TypeError, ValueError):
            pass

    info_data_row2 = [
        ('TOTAL EMISSIONS', f"{metrics['total_emission_ton']:.3f} tCO\u2082e"),
        ('CARBON INTENSITY', _intensity_txt),
        ('EFFICIENCY RATING', rating),
        ('ASSESSOR', project_info.get('assessor_name')),
        ('MODEL', getattr(data, 'model_type', 'IFC')),
        ('DATE', data.report_date),
    ]

    # KPI colors matching the advanced dashboard card colors
    _kpi_row2_colors = {
        0: COLORS['kpi_green'],   # Total Emissions - green card
        1: COLORS['kpi_green'],   # Carbon Intensity - green card
        2: band_info.get('color', COLORS['primary']),  # Efficiency Rating - band color
        3: COLORS['primary'],     # Assessor
        4: COLORS['kpi_blue'],    # Model - blue card
        5: COLORS['primary'],     # Date
    }

    for i, (label, raw_val) in enumerate(info_data_row2):
        cell = info_table.rows[1].cells[i]
        _set_cell_shading(cell, 'EDF7ED')
        _set_cell_margins(cell)
        p = cell.paragraphs[0]
        _add_run(p, label, size=6, color=COLORS['gray'])
        p2 = cell.add_paragraph()
        val_text, is_empty = _field_value(raw_val)
        if is_empty:
            _add_run(p2, val_text, size=8, italic=True, color=COLORS['na_gray'])
        else:
            _add_run(p2, val_text, size=8, bold=True, color=_kpi_row2_colors.get(i, COLORS['primary']))

    _set_table_row_heights(info_table, body_height_dxa=520, header_height_dxa=520)

    # Biogenic carbon stored (timber) — a separate line, never netted into the
    # A1-A5 total above (EN 16485). Only shown when timber is present.
    _seq_ton = metrics.get('sequestration_ton', 0) or 0
    if _seq_ton < 0:
        _seq_p = doc.add_paragraph()
        _add_run(_seq_p, '∑ Biogenic carbon stored: ', size=8, bold=True,
                 color=RGBColor(22, 163, 74))
        _add_run(_seq_p, f"{_seq_ton:.2f} tCO₂e in timber — reported separately, "
                         f"not deducted from the A1–A5 total (released at end of life; EN 16485).",
                 size=8, color=COLORS['gray'])

    # Charts: Two pie charts side by side in 2-column table
    pie_mat = generate_material_pie_chart(data.summary_df, metrics['total_emission_ton'])
    pie_stage = generate_lifecycle_pie_chart(data.stage_emissions)
    bar_chart = generate_lifecycle_bar_chart(data.stage_emissions)
    rating_chart = generate_rating_scale_chart(rating, metrics['total_emission_per_sqm'])

    # Pie charts: 2-column table, each cell 4320 DXA (3 inches), total 8640
    # Centred within 9360 content width
    pie_table = doc.add_table(rows=1, cols=2)
    _setup_table(pie_table, [4680, 4680], border_color='D9E2EC')

    c1 = pie_table.rows[0].cells[0]
    p1 = c1.paragraphs[0]
    p1.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if pie_mat:
        p1.add_run().add_picture(pie_mat, width=Twips(HALF_CHART_WIDTH_DXA))
    else:
        _add_run(p1, '[Material Chart]', color=COLORS['gray'])

    c2 = pie_table.rows[0].cells[1]
    p2 = c2.paragraphs[0]
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if pie_stage:
        p2.add_run().add_picture(pie_stage, width=Twips(HALF_CHART_WIDTH_DXA))
    else:
        _add_run(p2, '[Lifecycle Chart]', color=COLORS['gray'])

    # Compact chart row: place bar and rating charts side-by-side to save vertical space.
    compact_chart_table = doc.add_table(rows=1, cols=2)
    _setup_table(compact_chart_table, [HALF_WIDTH_DXA, HALF_WIDTH_DXA], border_color='D9E2EC')

    bar_cell = compact_chart_table.rows[0].cells[0]
    bar_para = bar_cell.paragraphs[0]
    bar_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if bar_chart:
        bar_para.add_run().add_picture(bar_chart, width=Twips(HALF_CHART_WIDTH_DXA))
    else:
        _add_run(bar_para, '[Lifecycle Bar Chart]', color=COLORS['gray'])

    rating_cell = compact_chart_table.rows[0].cells[1]
    rating_para = rating_cell.paragraphs[0]
    rating_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if rating_chart:
        rating_para.add_run().add_picture(rating_chart, width=Twips(HALF_CHART_WIDTH_DXA))
    else:
        _add_run(rating_para, '[Rating Scale Chart]', color=COLORS['gray'])

# =============================================================================
# PAGE 2: MATERIAL ANALYSIS + LIFECYCLE + MEMBER BREAKDOWN
# =============================================================================

def _add_page2_analysis(doc, data):
    metrics = data.metrics
    summary_df = data.summary_df
    total_emission = metrics['total_emission_ton']
    project_area = data.project_area
    per_sqm = metrics['total_emission_per_sqm']

    _add_section_header(doc, 'MATERIAL + LIFECYCLE ANALYSIS')

    split_table = doc.add_table(rows=1, cols=2)
    _setup_table(split_table, [1, 1], border_color='D9E2EC', body_height_dxa=200, header_height_dxa=200)

    left_cell = split_table.rows[0].cells[0]
    right_cell = split_table.rows[0].cells[1]

    _add_run(left_cell.paragraphs[0], 'MATERIAL BREAKDOWN', size=9, bold=True, color=COLORS['primary'])
    _add_run(right_cell.paragraphs[0], 'LIFECYCLE STAGE BREAKDOWN', size=9, bold=True, color=COLORS['primary'])

    # Compact material table (reduced to 5 columns)
    mat_widths = [1900, 1200, 900, 1050, 2600]
    mat_table = left_cell.add_table(rows=len(summary_df) + 2, cols=5)
    _setup_table(
        mat_table,
        mat_widths,
        target_total=HALF_WIDTH_DXA - 260,
        body_height_dxa=250,
        header_height_dxa=340,
    )

    headers = ['MATERIAL', 'EMISSION (tCO\u2082e)', '% TOTAL', 'kgCO\u2082e/m\u00b2', 'REDUCTION STRATEGY']
    for i, h in enumerate(headers):
        cell = mat_table.rows[0].cells[i]
        _set_cell_shading(cell, TABLE_HEADER_HEX)
        _add_run(cell.paragraphs[0], h, size=8, bold=True, color=COLORS['white'])

    for idx, row in summary_df.iterrows():
        mat = row['Material Type']
        emission = row['Total Emission(tCO2e)']
        pct = (emission / total_emission * 100) if total_emission > 0 else 0
        mat_per_sqm = (emission * 1000 / project_area) if project_area > 0 else 0

        tr = mat_table.rows[idx + 1]
        _add_run(tr.cells[0].paragraphs[0], mat, size=8, bold=True,
                 color=MATERIAL_COLORS_RGB.get(mat, COLORS['dark']))
        impact_color = _impact_color(pct)
        _add_run(tr.cells[1].paragraphs[0], f"{emission:.3f}", size=8, bold=True, color=impact_color)
        _add_run(tr.cells[2].paragraphs[0], f"{pct:.1f}%", size=8, bold=True, color=impact_color)
        _add_run(tr.cells[3].paragraphs[0], f"{mat_per_sqm:.1f}", size=8, color=impact_color)
        _add_run(tr.cells[4].paragraphs[0], REDUCTION_NOTES.get(mat, '\u2014'), size=7)

        if idx % 2 == 0:
            for cell in tr.cells:
                _set_cell_shading(cell, ROW_STRIPE_HEX)

    total_row = mat_table.rows[len(summary_df) + 1]
    _add_run(total_row.cells[0].paragraphs[0], 'TOTAL', size=8, bold=True)
    _add_run(total_row.cells[1].paragraphs[0], f"{total_emission:.3f}", size=8, bold=True)
    _add_run(total_row.cells[2].paragraphs[0], '100%', size=8, bold=True)
    _add_run(total_row.cells[3].paragraphs[0], f"{per_sqm:.1f}", size=8, bold=True)
    for cell in total_row.cells:
        _set_cell_shading(cell, ROW_TOTAL_HEX)

    _align_table_columns(mat_table, numeric_cols=[1, 2, 3])

    # Compact lifecycle table on right side
    stage_emissions = data.stage_emissions
    total_stage = sum(stage_emissions.values())
    stage_widths = [900, 1200, 900, 2450]
    stage_table = right_cell.add_table(rows=len(stage_emissions) + 1, cols=4)
    _setup_table(
        stage_table,
        stage_widths,
        target_total=HALF_WIDTH_DXA - 260,
        body_height_dxa=250,
        header_height_dxa=340,
    )

    for i, h in enumerate(['STAGE', 'EMISSION (tCO\u2082e)', '% TOTAL', 'KEY DRIVERS']):
        cell = stage_table.rows[0].cells[i]
        _set_cell_shading(cell, TABLE_HEADER_HEX)
        _add_run(cell.paragraphs[0], h, size=8, bold=True, color=COLORS['white'])

    stage_drivers = _stage_drivers(data)
    for idx, (stage, emission) in enumerate(stage_emissions.items()):
        tr = stage_table.rows[idx + 1]
        pct = (emission / total_stage * 100) if total_stage > 0 else 0
        impact_color = _impact_color(pct)
        _add_run(tr.cells[0].paragraphs[0], stage, size=8, bold=True)
        _add_run(tr.cells[1].paragraphs[0], f"{emission:.3f}", size=8, bold=True, color=impact_color)
        _add_run(tr.cells[2].paragraphs[0], f"{pct:.1f}%", size=8, bold=True, color=impact_color)
        _add_run(tr.cells[3].paragraphs[0], stage_drivers.get(stage, '\u2014'), size=7)
        if idx % 2 == 0:
            for cell in tr.cells:
                _set_cell_shading(cell, ROW_STRIPE_HEX)

    _align_table_columns(stage_table, numeric_cols=[1, 2], center_cols=[0])

    # Structural member breakdown is intentionally rendered alongside SCORS on page 3.


# =============================================================================
# PAGE 3: SCORS BENCHMARKS + MATERIAL QUANTITIES + CARBON REDUCTION
# =============================================================================

def _add_page3_benchmarks_and_quantities(doc, data):
    metrics = data.metrics
    per_sqm = metrics['total_emission_per_sqm']
    current_rating = data.efficiency_rating
    total_emission = metrics['total_emission_ton']
    project_area = data.project_area

    _add_section_header(doc, 'SCORS EFFICIENCY RATING + STRUCTURAL MEMBER BREAKDOWN')

    top_split = doc.add_table(rows=1, cols=2)
    _setup_table(top_split, [1, 1], border_color='D9E2EC', body_height_dxa=180, header_height_dxa=180)
    scors_cell = top_split.rows[0].cells[0]
    member_cell = top_split.rows[0].cells[1]

    _add_run(scors_cell.paragraphs[0], 'SCORS EFFICIENCY RATING', size=9, bold=True, color=COLORS['primary'])
    _add_run(member_cell.paragraphs[0], 'STRUCTURAL MEMBER BREAKDOWN', size=9, bold=True, color=COLORS['primary'])

    rating_widths = [700, 1000, 2300, 1000]
    rating_table = scors_cell.add_table(rows=10, cols=4)
    _setup_table(
        rating_table,
        rating_widths,
        target_total=HALF_WIDTH_DXA - 260,
        body_height_dxa=230,
        header_height_dxa=330,
    )

    for i, h in enumerate(['RATING', 'kgCO\u2082e/m\u00b2', 'DESCRIPTION', 'STATUS']):
        cell = rating_table.rows[0].cells[i]
        _set_cell_shading(cell, TABLE_HEADER_HEX)
        _add_run(cell.paragraphs[0], h, size=8, bold=True, color=COLORS['white'])

    for idx, (rating, info) in enumerate(EFFICIENCY_BANDS.items()):
        tr = rating_table.rows[idx + 1]
        is_current = (rating == current_rating)

        _add_run(tr.cells[0].paragraphs[0], rating, size=10, bold=True, color=info['color'])
        _add_run(tr.cells[1].paragraphs[0], info['range'], size=8)
        _add_run(tr.cells[2].paragraphs[0], info['desc'], size=8)

        if is_current:
            _add_run(tr.cells[3].paragraphs[0], 'PROJECT', size=8, bold=True, color=COLORS['primary'])
            for cell in tr.cells:
                _set_cell_shading(cell, ROW_HIGHLIGHT_HEX)
        else:
            _add_run(tr.cells[3].paragraphs[0], '\u2014', color=COLORS['light_gray'])

    _align_table_columns(rating_table, center_cols=[0, 1, 3])

    member_df = _build_member_summary(data.detailed_df, project_area)
    if member_df is not None and not member_df.empty:
        mem_cols = ['MEMBER', 'A1-A3', 'A4-A5', 'TOTAL', 'kgCO\u2082e/m\u00b2']
        mem_widths = [2100, 1000, 1000, 1050, 1100]
        mem_table = member_cell.add_table(rows=len(member_df) + 2, cols=5)
        _setup_table(
            mem_table,
            mem_widths,
            target_total=HALF_WIDTH_DXA - 260,
            body_height_dxa=230,
            header_height_dxa=330,
        )

        for i, h in enumerate(mem_cols):
            cell = mem_table.rows[0].cells[i]
            _set_cell_shading(cell, TABLE_HEADER_HEX)
            _add_run(cell.paragraphs[0], h, size=8, bold=True, color=COLORS['white'])

        max_total = member_df['Total (tCO2e)'].max() if len(member_df) else 0
        for idx, row in member_df.iterrows():
            tr = mem_table.rows[idx + 1]
            a45 = row['A4 (tCO2e)'] + row['A5 (tCO2e)']
            pct_share = (row['Total (tCO2e)'] / max_total * 100) if max_total > 0 else 0
            tone = _impact_color(pct_share)

            _add_run(tr.cells[0].paragraphs[0], str(row['Member Type']), size=8, bold=True)
            _add_run(tr.cells[1].paragraphs[0], f"{row['A1-A3 (tCO2e)']:.3f}", size=8)
            _add_run(tr.cells[2].paragraphs[0], f"{a45:.3f}", size=8)
            _add_run(tr.cells[3].paragraphs[0], f"{row['Total (tCO2e)']:.3f}", size=8, bold=True, color=tone)
            _add_run(tr.cells[4].paragraphs[0], f"{row['Per m\u00b2 (kgCO2e/m\u00b2)']:.1f}", size=8, color=tone)

            if idx % 2 == 0:
                for cell in tr.cells:
                    _set_cell_shading(cell, ROW_STRIPE_HEX)

        gt_row = mem_table.rows[len(member_df) + 1]
        _add_run(gt_row.cells[0].paragraphs[0], 'TOTAL', size=8, bold=True)
        _add_run(gt_row.cells[1].paragraphs[0], f"{member_df['A1-A3 (tCO2e)'].sum():.3f}", size=8, bold=True)
        _add_run(gt_row.cells[2].paragraphs[0],
                 f"{(member_df['A4 (tCO2e)'].sum() + member_df['A5 (tCO2e)'].sum()):.3f}", size=8, bold=True)
        _add_run(gt_row.cells[3].paragraphs[0], f"{member_df['Total (tCO2e)'].sum():.3f}", size=8, bold=True)
        _add_run(gt_row.cells[4].paragraphs[0], f"{member_df['Per m\u00b2 (kgCO2e/m\u00b2)'].sum():.1f}", size=8, bold=True)
        for cell in gt_row.cells:
            _set_cell_shading(cell, ROW_TOTAL_HEX)

        _align_table_columns(mem_table, numeric_cols=[1, 2, 3, 4])
    else:
        info_para = member_cell.add_paragraph()
        _add_run(info_para, 'No member-level data available.', size=8, italic=True, color=COLORS['na_gray'])

    # SCORS structural carbon rating (band B = 200 kgCO2e/m2 target)
    scors_target = 200  # SCORS band-B upper bound (structural, A1-A5)

    status_para = doc.add_paragraph()
    status_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    status_para.paragraph_format.space_before = Pt(4)
    if per_sqm > scors_target:
        reduction = per_sqm - scors_target
        pct_red = (reduction / per_sqm) * 100
        _add_run(status_para,
                 f"SCORS: Reduction of {reduction:.1f} kgCO\u2082e/m\u00b2 ({pct_red:.1f}%) "
                 f"required to reach the SCORS B target of \u2264{scors_target} kgCO\u2082e/m\u00b2 "
                 f"(structural, A1\u2013A5)",
                 size=9, bold=True, color=COLORS['warning'])
    else:
        _add_run(status_para,
                 f"SCORS B or better \u2014 structural carbon within \u2264{scors_target} kgCO\u2082e/m\u00b2 "
                 f"(current rating {current_rating})",
                 size=9, bold=True, color=COLORS['success'])

    _add_section_header(doc, 'BENCHMARKS + QUANTITIES (COMPACT VIEW)')

    split_table = doc.add_table(rows=1, cols=2)
    _setup_table(split_table, [1, 1], border_color='D9E2EC', body_height_dxa=200, header_height_dxa=200)

    left_cell = split_table.rows[0].cells[0]
    right_cell = split_table.rows[0].cells[1]

    _add_run(left_cell.paragraphs[0], 'INDUSTRY BENCHMARK CONTEXT', size=9, bold=True, color=COLORS['primary'])
    _add_run(right_cell.paragraphs[0], 'MATERIAL QUANTITIES & CARBON FACTORS', size=9, bold=True, color=COLORS['primary'])

    bench_widths = [2700, 1200, 1200]
    bench_table = left_cell.add_table(rows=5, cols=3)
    _setup_table(
        bench_table,
        bench_widths,
        target_total=HALF_WIDTH_DXA - 260,
        body_height_dxa=250,
        header_height_dxa=340,
    )

    for i, h in enumerate(['BENCHMARK', 'kgCO\u2082e/m\u00b2', 'STATUS']):
        cell = bench_table.rows[0].cells[i]
        _set_cell_shading(cell, TABLE_HEADER_HEX)
        _add_run(cell.paragraphs[0], h, size=8, bold=True, color=COLORS['white'])

    benchmarks = [
        ('This Project', f'{per_sqm:.1f}', current_rating, True),
        ('Irish RC-frame average (IGBC)', '~340', 'Reference', False),
        ('SCORS B target', '200', 'B rating', False),
        ('SCORS A++ target', '50', 'A++ rating', False),
    ]
    for idx, (name, val, status, highlight) in enumerate(benchmarks):
        tr = bench_table.rows[idx + 1]
        _add_run(tr.cells[0].paragraphs[0], name, size=8, bold=highlight)
        _add_run(tr.cells[1].paragraphs[0], val, size=8, bold=highlight)
        _add_run(tr.cells[2].paragraphs[0], status, size=8)
        if highlight:
            for cell in tr.cells:
                _set_cell_shading(cell, ROW_HIGHLIGHT_HEX)
        elif idx % 2 == 0:
            for cell in tr.cells:
                _set_cell_shading(cell, ROW_STRIPE_HEX)

    _align_table_columns(bench_table, numeric_cols=[1], center_cols=[2])

    qty_widths = [2200, 1800, 1900, 1400]
    qty_headers = ['Material', 'Quantity', 'Carbon Factor', 'Source']

    # Gather quantities from detailed_df
    detailed_df = data.detailed_df
    qty_rows = []
    if detailed_df is not None and not detailed_df.empty:
        _rebar_eids = {'R_005', 'R_022', 'R_024'}
        _section_eids = {'R_001', 'R_004', 'R_014', 'R_020'}

        if 'e_id' in detailed_df.columns:
            eids = detailed_df['e_id'].astype(str)
            other_r = eids.str.startswith('R_', na=False) & ~eids.isin(_rebar_eids) & ~eids.isin(_section_eids)
            masks = {
                'Concrete': eids.str.startswith('C_', na=False),
                'Rebar': eids.isin(_rebar_eids),
                'Steel Section': eids.isin(_section_eids) | other_r,
                'Post Tensioning': eids.str.startswith('PT_', na=False),
                'Timber': eids.str.startswith('T_', na=False),
            }
        else:
            material_col = detailed_df['Material'].astype(str) if 'Material' in detailed_df.columns else pd.Series('', index=detailed_df.index)
            masks = {
                'Concrete': material_col == 'Concrete',
                'Rebar': material_col == 'Rebar',
                'Steel Section': material_col == 'Steel Section',
                'Post Tensioning': material_col == 'Post Tensioning',
                'Timber': material_col == 'Timber',
            }

        for mat_type in ['Concrete', 'Rebar', 'Steel Section', 'Post Tensioning', 'Timber']:
            mat_mask = masks.get(mat_type, pd.Series(False, index=detailed_df.index))
            if mat_mask.any():
                subset = detailed_df[mat_mask]
                total_vol = pd.to_numeric(subset.get('Total Volume(m3)', 0), errors='coerce').fillna(0).sum()
                total_mass = pd.to_numeric(subset.get('Mass(kg)', 0), errors='coerce').fillna(0).sum()
                a1a3_total = pd.to_numeric(subset.get('A1-A3 Emission(kgCO2e)', 0), errors='coerce').fillna(0).sum()
                avg_factor = (a1a3_total / total_mass) if total_mass > 0 else 0

                # Concrete and Timber are quantified by volume; steel/PT by mass.
                if mat_type in ('Concrete', 'Timber'):
                    qty_rows.append((mat_type, f"{total_vol:,.1f} m\u00b3",
                                     f"{avg_factor:.3f} kgCO\u2082e/kg", 'IGBC 2024'))
                else:
                    qty_rows.append((mat_type, f"{total_mass:,.0f} kg",
                                     f"{avg_factor:.4f} kgCO\u2082e/kg", 'IGBC 2024'))

    if not qty_rows:
        qty_rows = [('Concrete', 'TBC', 'TBC', 'IGBC 2024'),
                    ('Rebar', 'TBC', 'TBC', 'IGBC 2024'),
                    ('Steel Section', 'TBC', 'TBC', 'IGBC 2024'),
                    ('Post Tensioning', 'TBC', 'TBC', 'IGBC 2024')]

    qty_table = right_cell.add_table(rows=len(qty_rows) + 1, cols=4)
    _setup_table(
        qty_table,
        qty_widths,
        target_total=HALF_WIDTH_DXA - 260,
        body_height_dxa=250,
        header_height_dxa=340,
    )

    for i, h in enumerate(qty_headers):
        cell = qty_table.rows[0].cells[i]
        _set_cell_shading(cell, TABLE_HEADER_HEX)
        _add_run(cell.paragraphs[0], h, size=8, bold=True, color=COLORS['white'])

    for idx, row_data in enumerate(qty_rows):
        tr = qty_table.rows[idx + 1]
        for ci, val in enumerate(row_data):
            _add_run(tr.cells[ci].paragraphs[0], val, size=8,
                     bold=(ci == 0),
                     color=MATERIAL_COLORS_RGB.get(row_data[0], COLORS['dark']) if ci == 0 else None)
        if idx % 2 == 0:
            for cell in tr.cells:
                _set_cell_shading(cell, ROW_STRIPE_HEX)

    _align_table_columns(qty_table)

    # CARBON REDUCTION SCENARIOS
    _add_section_header(doc, 'CARBON REDUCTION SCENARIOS')

    red_widths = [3000, 2100, 2160, 2100]  # sum = 9360
    red_headers = ['Intervention', 'Saving (tCO\u2082e)', 'Saving (kgCO\u2082e/m\u00b2)', 'New Rating']

    from compliance import get_efficiency_rating

    # Detect context: what steel/PT was selected
    steel_type = getattr(data, 'steel_type', '')

    # Irish/EAF-based steel types — already low-carbon
    _EAF_STEEL_NAMES = {'Ireland Average Reinforcing Steel', 'Steel Section (Ireland)'}
    is_eaf_steel = steel_type in _EAF_STEEL_NAMES

    # EPD-based EAF steel factors (best available: Celsa ~0.39 kgCO2e/kg)
    _EPD_EAF_FACTOR = 0.39
    _IRELAND_AVG_FACTOR = 0.737

    # Calculate potential reductions
    reductions = []
    if project_area > 0 and total_emission > 0:
        # 1) 25% GGBS concrete
        concrete_emission = 0
        _mat_type_col = 'Material Type'
        if data.summary_df is not None and _mat_type_col in data.summary_df.columns:
            c_mask = data.summary_df[_mat_type_col] == 'Concrete'
            if c_mask.any():
                concrete_emission = data.summary_df.loc[c_mask, 'Total Emission(tCO2e)'].sum()
        if concrete_emission > 0:
            ggbs_saving = concrete_emission * 0.13  # ~13% reduction with 25% GGBS
            new_total = total_emission - ggbs_saving
            new_per_sqm = new_total * 1000 / project_area
            reductions.append(('25% GGBS in all concrete mixes',
                               f"{ggbs_saving:.3f}", f"{ggbs_saving * 1000 / project_area:.1f}",
                               get_efficiency_rating(new_per_sqm)))

        # 2) Steel scenario — context-aware
        steel_emission = 0
        if data.summary_df is not None and _mat_type_col in data.summary_df.columns:
            s_mask = data.summary_df[_mat_type_col].isin(['Steel', 'Rebar', 'Steel Section'])
            if s_mask.any():
                steel_emission = data.summary_df.loc[s_mask, 'Total Emission(tCO2e)'].sum()

        if steel_emission > 0:
            if is_eaf_steel:
                epd_reduction_pct = (_IRELAND_AVG_FACTOR - _EPD_EAF_FACTOR) / _IRELAND_AVG_FACTOR
                epd_saving = steel_emission * epd_reduction_pct
                new_total = total_emission - epd_saving
                new_per_sqm = new_total * 1000 / project_area
                reductions.append((f'EPD-verified EAF rebar (~{_EPD_EAF_FACTOR} kgCO\u2082e/kg)',
                                   f"{epd_saving:.3f}", f"{epd_saving * 1000 / project_area:.1f}",
                                   get_efficiency_rating(new_per_sqm)))
            else:
                eaf_saving = steel_emission * 0.40
                new_total = total_emission - eaf_saving
                new_per_sqm = new_total * 1000 / project_area
                reductions.append(('Switch to 100% EAF recycled steel',
                                   f"{eaf_saving:.3f}", f"{eaf_saving * 1000 / project_area:.1f}",
                                   get_efficiency_rating(new_per_sqm)))

        # 3) Combined
        combined_saving = 0
        combined_parts = []
        if concrete_emission > 0:
            combined_saving += concrete_emission * 0.13
            combined_parts.append('25% GGBS')
        if steel_emission > 0:
            if is_eaf_steel:
                combined_saving += steel_emission * ((_IRELAND_AVG_FACTOR - _EPD_EAF_FACTOR) / _IRELAND_AVG_FACTOR)
                combined_parts.append('EPD rebar')
            else:
                combined_saving += steel_emission * 0.40
                combined_parts.append('EAF steel')
        if combined_saving > 0 and len(combined_parts) > 1:
            new_total = total_emission - combined_saving
            new_per_sqm = new_total * 1000 / project_area
            reductions.append((f"Combined: {' + '.join(combined_parts)}",
                               f"{combined_saving:.3f}", f"{combined_saving * 1000 / project_area:.1f}",
                               get_efficiency_rating(new_per_sqm)))

    if not reductions:
        reductions = [('50% GGBS replacement', 'TBC', 'TBC', 'TBC'),
                      ('Material decarbonization', 'TBC', 'TBC', 'TBC'),
                      ('Combined strategies', 'TBC', 'TBC', 'TBC')]

    red_table = doc.add_table(rows=len(reductions) + 1, cols=4)
    _setup_table(red_table, red_widths)

    for i, h in enumerate(red_headers):
        cell = red_table.rows[0].cells[i]
        _set_cell_shading(cell, TABLE_HEADER_HEX)
        _add_run(cell.paragraphs[0], h, size=8, bold=True, color=COLORS['white'])

    for idx, (intervention, saving_t, saving_sqm, new_rating) in enumerate(reductions):
        tr = red_table.rows[idx + 1]
        _add_run(tr.cells[0].paragraphs[0], intervention, size=8)
        _add_run(tr.cells[1].paragraphs[0], saving_t, size=8)
        _add_run(tr.cells[2].paragraphs[0], saving_sqm, size=8)
        new_band = EFFICIENCY_BANDS.get(new_rating, {})
        _add_run(tr.cells[3].paragraphs[0], new_rating, size=8, bold=True,
                 color=new_band.get('color', COLORS['dark']))
        if idx % 2 == 0:
            for cell in tr.cells:
                _set_cell_shading(cell, ROW_STRIPE_HEX)

    _align_table_columns(red_table, numeric_cols=[1, 2], center_cols=[3])


# =============================================================================
# PAGE 4: METHODOLOGY + SUMMARY + ASSESSOR DECLARATION
# =============================================================================

def _add_page4_methodology_and_summary(doc, data):
    metrics = data.metrics

    _add_section_header(doc, 'METHODOLOGY & ASSESSMENT SCOPE')

    # Single-row compact card layout to maximize horizontal usage.
    meth_grid = doc.add_table(rows=1, cols=3)
    _setup_table(meth_grid, [1, 1, 1], border_color='D9E2EC', body_height_dxa=220, header_height_dxa=220)

    c1 = meth_grid.rows[0].cells[0]
    c2 = meth_grid.rows[0].cells[1]
    c3 = meth_grid.rows[0].cells[2]

    _add_run(c1.paragraphs[0], 'METHODOLOGY', size=9, bold=True, color=COLORS['secondary'])
    _a5a = _a5a_factor_of(data)
    for line in [
        'EN 15978 framework (subset application): A1-A5 upfront carbon only.',
        'IGBC factors with SEAI/Construct Innovate references.',
        'A4 transport per SEAI methodology (Table 3 factors, Table 2 distances; '
        f'road = {SEAI_CF_OUTWARD_ROAD:g} outward + {SEAI_CF_RETURN_ROAD:g} return).',
        f'A5a = {_a5a:g} kgCO2e/m2 GIA'
        + (' (user override).' if _a5a_is_override(data) else '.'),
    ]:
        _add_run(c1.add_paragraph(), f'- {line}', size=8)

    _add_run(c2.paragraphs[0], 'SCOPE + LIFECYCLE STAGES', size=9, bold=True, color=COLORS['secondary'])
    _add_run(c2.add_paragraph(), '- Excluded: B1-B7, C1-C4, Module D.', size=8)
    _add_run(c2.add_paragraph(), '- Included: A1-A3 Product stage.', size=8)
    _add_run(c2.add_paragraph(), '- Included: A4 Transport to site.', size=8)
    _add_run(c2.add_paragraph(), '- Included: A5 Construction + wastage.', size=8)

    _add_run(c3.paragraphs[0], 'BENCHMARK FRAMEWORKS', size=9, bold=True, color=COLORS['secondary'])
    for line in [
        'SCORS B target: <=200 kgCO2e/m2 (structural, A1-A5).',
        'SCORS rating scale: A++ to G.',
        'Metric basis: kgCO2e per m2 floor area.',
    ]:
        _add_run(c3.add_paragraph(), f'- {line}', size=8)

    # RESULTS SUMMARY + TRANSPORTATION
    _add_section_header(doc, 'ASSESSMENT SUMMARY')

    summ_table = doc.add_table(rows=1, cols=2)
    _setup_table(summ_table, [4500, 4500], border_color='FFFFFF')

    # Col 1: Key metrics
    c1 = summ_table.rows[0].cells[0]
    _set_cell_margins(c1, left=0, right=80)
    _add_run(c1.paragraphs[0], 'RESULTS SUMMARY', size=9, bold=True, color=COLORS['primary'])

    summary_items = [
        ('Total Embodied Carbon', f"{metrics['total_emission_ton']:.3f} tCO\u2082e"),
        ('Carbon Intensity', f"{metrics['total_emission_per_sqm']:.3f} kgCO\u2082e/m\u00b2"),
        ('Gross Floor Area', f"{data.project_area:,.1f} m\u00b2"),
        ('SCORS Rating', data.efficiency_rating),
    ]
    for label, value in summary_items:
        p = c1.add_paragraph()
        _add_run(p, f"{label}: ", size=8, color=COLORS['gray'])
        _add_run(p, value, size=8, bold=True, color=COLORS['dark'])

    # Material contribution
    p_mc = c1.add_paragraph()
    p_mc.paragraph_format.space_before = Pt(6)
    _add_run(p_mc, 'MATERIAL CONTRIBUTION', size=9, bold=True, color=COLORS['primary'])

    total = metrics['total_emission_ton']
    for _, row in data.summary_df.iterrows():
        mat = row['Material Type']
        emission = row['Total Emission(tCO2e)']
        pct = (emission / total * 100) if total > 0 else 0
        p = c1.add_paragraph()
        _add_run(p, f"{mat}: {emission:.3f} tCO\u2082e ({pct:.1f}%)",
                 size=8, bold=True, color=MATERIAL_COLORS_RGB.get(mat, COLORS['dark']))

    # Col 2: Transportation
    c2 = summ_table.rows[0].cells[1]
    _set_cell_margins(c2, left=80, right=0)
    _add_run(c2.paragraphs[0], 'TRANSPORT DISTANCES (A4, SEAI Table 2)', size=9, bold=True, color=COLORS['primary'])

    distances = getattr(data, 'distances', {})
    for fam, meta in SEAI_TRANSPORT_TABLE.items():
        sea = float(distances.get(f'{fam}_sea_distance', meta['sea']) or 0)
        road = float(distances.get(f'{fam}_road_distance', meta['road']) or 0)
        p = c2.add_paragraph()
        _add_run(p, f"{meta['label']}: ", size=8, color=COLORS['gray'])
        _add_run(p, f"{sea:.0f} km sea / {road:.0f} km road", size=8, bold=True, color=COLORS['dark'])
    p_cf = c2.add_paragraph()
    _add_run(p_cf, 'Factors (SEAI Table 3): ', size=7, color=COLORS['gray'])
    _add_run(p_cf, f"road {SEAI_CF_OUTWARD_ROAD:g} outward + {SEAI_CF_RETURN_ROAD:g} return; "
                   f"sea {SEAI_CF_SEA:g} kgCO2e/kg.km single leg", size=7, color=COLORS['dark'])

    # Waste factors
    p_wf = c2.add_paragraph()
    p_wf.paragraph_format.space_before = Pt(6)
    _add_run(p_wf, 'WASTE FACTORS (A5w)', size=9, bold=True, color=COLORS['primary'])

    waste_items = [
        ('Concrete', '5%'),
        ('Steel Reinforcement', '3%'),
        ('Post Tensioning', '1.5%'),
    ]
    for mat, pct in waste_items:
        p = c2.add_paragraph()
        _add_run(p, f"{mat}: ", size=8, color=COLORS['gray'])
        _add_run(p, pct, size=8, bold=True, color=COLORS['dark'])
    p_wf2 = c2.add_paragraph()
    _add_run(p_wf2, 'A5w = waste% × (A1-A3 + A4) per SEAI A5.3 (C2/C4 excluded).',
             size=7, color=COLORS['gray'])

    # KEY ASSUMPTIONS
    _add_section_header(doc, 'KEY ASSUMPTIONS & NOTES')

    assump_table = doc.add_table(rows=1, cols=2)
    _setup_table(assump_table, [4500, 4500], border_color='FFFFFF')

    ac1 = assump_table.rows[0].cells[0]
    _set_cell_margins(ac1, left=0, right=80)
    _add_run(ac1.paragraphs[0], 'ASSESSMENT ASSUMPTIONS', size=9, bold=True, color=COLORS['primary'])

    stage = data.project_info.get('stage', '')
    if 'Tender' in stage:
        stage_note = "Tender Stage: Supplier EPDs used where available."
    elif 'Detailed' in stage:
        stage_note = "Detailed Design: Generic catalogue rates; override with EPDs if available."
    else:
        stage_note = "Concept / Schematic Stage: Generic industry factors applied (\u00b120-30%)."

    _a5a = _a5a_factor_of(data)
    a5a_assumption = (
        f"A5a construction activities: {_a5a:g} kgCO\u2082e/m\u00b2 GIA "
        f"(project override \u2014 supersedes the SEAI default of {A5A_DEFAULT:g})"
        if _a5a_is_override(data) else
        f"A5a construction activities: {_a5a:g} kgCO\u2082e/m\u00b2 GIA (SEAI \u2014 70% of 40 kgCO\u2082e/m\u00b2)")
    assumptions = [
        stage_note,
        a5a_assumption,
        "A4 transport per SEAI methodology: Table 3 carbon factors, Table 2 default "
        f"distances by material; road = {SEAI_CF_OUTWARD_ROAD:g} outward + {SEAI_CF_RETURN_ROAD:g} return, sea single leg",
        "Data sourced from IGBC. Methodology from SEAI and Construct Innovate",
        "A5w site waste per SEAI A5.3: waste% × (A1-A3 + A4); end-of-life C2/C4 excluded (A1-A5 scope)",
        "Post-tensioning: no generic PT factor exists in the IGBC database — defaults to the Ireland "
        "rebar factor (0.737 kgCO2e/kg) per IStructE guidance, unless a named supplier EPD is selected",
        "Only 16% green concrete (GGBS/PFA) currently available in supply chain",
        "Only 30% EAF steel currently available from suppliers",
    ]
    for a in assumptions:
        p = ac1.add_paragraph()
        _add_run(p, a, size=8)

    # Rebar rates used
    rebar_rates_used = getattr(data, 'rebar_rates_used', []) or []
    if rebar_rates_used:
        p_rr = ac1.add_paragraph()
        p_rr.paragraph_format.space_before = Pt(5)
        _add_run(p_rr, 'REBAR RATES USED (kg/m\u00b3)', size=9, bold=True, color=COLORS['primary'])
        for r in rebar_rates_used:
            p = ac1.add_paragraph()
            _add_run(p, f"{r['element']}: ", size=8, color=COLORS['gray'])
            _add_run(p, f"{r['rate_kg_m3']:.0f} kg/m\u00b3", size=8, bold=True, color=COLORS['dark'])

    ac2 = assump_table.rows[0].cells[1]
    _set_cell_margins(ac2, left=80, right=0)

    # Concrete grade + GGBS
    concrete_specs_used = getattr(data, 'concrete_specs_used', []) or []
    if concrete_specs_used:
        _add_run(ac2.paragraphs[0], 'CONCRETE STRENGTH & GGBS CONTENT', size=9, bold=True, color=COLORS['primary'])
        for s in concrete_specs_used:
            p = ac2.add_paragraph()
            _add_run(p, f"{s['element']}: ", size=8, color=COLORS['gray'])
            _add_run(p,
                     f"{s['grade']} MPa, {s['ggbs_pct']}% GGBS "
                     f"\u2014 A1-A3 = {s['a1_a3']:.3f} kgCO\u2082e/kg",
                     size=8, bold=True, color=COLORS['dark'])
        p_blank = ac2.add_paragraph()
        p_blank.paragraph_format.space_before = Pt(4)
    else:
        _add_run(ac2.paragraphs[0], 'EMISSION FACTORS USED', size=9, bold=True, color=COLORS['primary'])

    # Emission factors used (without e_id)
    emission_factors_used = getattr(data, 'emission_factors_used', []) or []
    if emission_factors_used:
        p_ef = ac2.add_paragraph()
        p_ef.paragraph_format.space_before = Pt(4)
        _add_run(p_ef, 'EMISSION FACTORS USED', size=9, bold=True, color=COLORS['primary'])
        for f in emission_factors_used:
            p = ac2.add_paragraph()
            tag = ' [Custom]' if f.get('is_custom') else ''
            _add_run(p, f"{f['material']} \u2014 {f['name']}{tag}: ", size=8, color=COLORS['gray'])
            _add_run(p,
                     f"A1-A3 = {f['a1_a3']:.3f} kgCO\u2082e/kg  |  "
                     f"\u03c1 = {f['density']:.0f} kg/m\u00b3",
                     size=8, bold=True, color=COLORS['dark'])

    p_note = ac2.add_paragraph()
    p_note.paragraph_format.space_before = Pt(6)
    _add_run(p_note, 'POTENTIAL FOR REDUCTION', size=9, bold=True, color=COLORS['primary'])
    p_txt = ac2.add_paragraph()
    _add_run(p_txt,
             "The embodied carbon values presented in this report are based on national average "
             "datasets (IGBC). Once specific suppliers are appointed and product-specific "
             "Environmental Product Declarations (EPDs) are obtained, the actual embodied carbon values "
             "are expected to be lower. Supplier-specific EPDs typically reflect more efficient "
             "manufacturing processes, higher recycled content, and optimised supply chains.",
             size=8, color=COLORS['dark'])

    # Model visualization replaces the previous assessment information block.
    model_type = str(getattr(data, 'model_type', 'none')).lower()
    if model_type == 'ifc':
        _add_ifc_model_preview_section(doc, data, large=True)
    elif model_type == 'glb':
        _add_glb_model_preview_section(doc, data, large=True)

    # FOOTER — single paragraph with right-aligned tab stop
    _add_ruled_line(doc)
    footer_para = doc.add_paragraph()
    footer_para.paragraph_format.space_before = Pt(2)
    footer_para.paragraph_format.space_after = Pt(0)

    # Add a right-aligned tab stop at 9360 DXA
    pPr = footer_para._p.get_or_add_pPr()
    tabs = parse_xml(
        f'<w:tabs {nsdecls("w")}>'
        f'<w:tab w:val="right" w:pos="{CONTENT_WIDTH_DXA}"/>'
        f'</w:tabs>')
    pPr.append(tabs)

    _add_run(footer_para, f"Generated: {data.report_date}", size=7, color=COLORS['gray'])
    # Tab character
    tab_run = footer_para.add_run()
    tab_run.font.size = Pt(7)
    tab_run._r.append(parse_xml(f'<w:tab {nsdecls("w")}/>'))
    _add_run(footer_para, "C.L.E.A.R. - Carbon Lifecycle Evaluation and Reporting | A1-A5 upfront carbon (EN 15978 subset)",
             size=7, bold=True, color=COLORS['primary'])
