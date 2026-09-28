import pandas as pd
import os
from io import BytesIO
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')

from calculations import (A5A_EMISSION_FACTOR_KGCO2E_PER_SQM as A5A_DEFAULT,
                          SEAI_CF_OUTWARD_ROAD, SEAI_CF_RETURN_ROAD, SEAI_CF_SEA)
from catalogue import SEAI_TRANSPORT_TABLE


def _a5a_factor_of(data):
    """A5a site-activity factor (kgCO2e/m² GIA) this run was calculated with.
    Prefers the value the engine recorded on the model; falls back to metrics,
    then the SEAI default. Never hard-code 28 in report text — read this."""
    v = getattr(data, 'a5a_factor', None)
    if v is None:
        v = (getattr(data, 'metrics', None) or {}).get('a5a_factor')
    try:
        return float(v)
    except (TypeError, ValueError):
        return A5A_DEFAULT


def _a5a_is_override(data):
    return abs(_a5a_factor_of(data) - A5A_DEFAULT) > 1e-6


try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
    from openpyxl.utils.dataframe import dataframe_to_rows
    from openpyxl.utils import get_column_letter
    from openpyxl.chart import PieChart, BarChart, Reference
    from openpyxl.chart.label import DataLabelList
    from openpyxl.chart.series import DataPoint
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False


# Style definitions (only if openpyxl is available)
if OPENPYXL_AVAILABLE:
    HEADER_FILL = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
    SUBHEADER_FILL = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
    SUBHEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
    SECTION_FILL = PatternFill(start_color="D6DCE5", end_color="D6DCE5", fill_type="solid")
    SECTION_FONT = Font(bold=True, size=11, color="1F4E79")
    TITLE_FONT = Font(bold=True, size=16, color="1F4E79")
    SUBTITLE_FONT = Font(bold=True, size=12, color="2E75B6")
    DATA_FONT = Font(size=10)
    THIN_BORDER = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )
    CENTER_ALIGN = Alignment(horizontal='center', vertical='center')
    LEFT_ALIGN = Alignment(horizontal='left', vertical='center')
    RIGHT_ALIGN = Alignment(horizontal='right', vertical='center')
    WRAP_ALIGN = Alignment(horizontal='center', vertical='center', wrap_text=True)
    ACCENT_FILL = PatternFill(start_color="E8F0FE", end_color="E8F0FE", fill_type="solid")
    TOTAL_FILL = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    TOTAL_FONT = Font(bold=True, size=10, color="FFFFFF")
    MEMBER_COLORS = {
        'Beams': PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid"),
        'Columns': PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid"),
        'Slabs / Floors': PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid"),
        'Walls': PatternFill(start_color="FCE7F3", end_color="FCE7F3", fill_type="solid"),
        'Foundations': PatternFill(start_color="EDE9FE", end_color="EDE9FE", fill_type="solid"),
        'Stairs': PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid"),
        'Other': PatternFill(start_color="F3F4F6", end_color="F3F4F6", fill_type="solid"),
    }


def _classify_member_type(row):
    """
    Classify a structural member into a category (Beams, Columns, Slabs / Floors,
    Walls, Foundations, Stairs, Other).

    Uses the 'Category' column first. If that is empty / missing, falls back to
    pattern matching on 'e_id', 'Material', and 'Description'.
    """
    # 1. Try the explicit Category column
    category = str(row.get('Category', '')).strip()
    if category and category.lower() not in ('', 'nan', 'none', 'n/a', '-'):
        return _normalise_category(category)

    # 2. Fallback: pattern-match on e_id + Description
    e_id = str(row.get('e_id', '')).lower()
    desc = str(row.get('Description', '')).lower()
    material = str(row.get('Material', '')).lower()
    combined = f"{e_id} {desc} {material}"

    beam_kw = ['beam', 'bm', 'girder', 'lintel', 'transfer beam', 'band beam',
               'upstand', 'downstand']
    col_kw = ['column', 'col', 'pillar', 'pier', 'post']
    slab_kw = ['slab', 'floor', 'deck', 'topping', 'flat plate', 'flat slab',
               'raft', 'plank', 'hollow core', 'hollowcore', 'precast floor']
    wall_kw = ['wall', 'core wall', 'shear wall', 'retaining']
    found_kw = ['foundation', 'footing', 'pile', 'pile cap', 'pilecap',
                'ground beam', 'mat foundation', 'pad', 'strip footing',
                'base', 'substructure']
    stair_kw = ['stair', 'step', 'ramp', 'landing']

    for kw in beam_kw:
        if kw in combined:
            return 'Beams'
    for kw in col_kw:
        if kw in combined:
            return 'Columns'
    for kw in slab_kw:
        if kw in combined:
            return 'Slabs / Floors'
    for kw in wall_kw:
        if kw in combined:
            return 'Walls'
    for kw in found_kw:
        if kw in combined:
            return 'Foundations'
    for kw in stair_kw:
        if kw in combined:
            return 'Stairs'

    return 'Other'


def _normalise_category(raw):
    """Map common category names to a consistent label."""
    raw_lower = raw.lower().strip()

    beam_names = {'beam', 'beams', 'bm', 'girder', 'girders', 'lintel', 'lintels',
                  'transfer beam', 'band beam', 'upstand', 'downstand'}
    col_names = {'column', 'columns', 'col', 'cols', 'pillar', 'pillars',
                 'pier', 'piers', 'post', 'posts'}
    slab_names = {'slab', 'slabs', 'floor', 'floors', 'deck', 'decks',
                  'flat slab', 'flat plate', 'topping', 'slab/floor',
                  'slabs/floors', 'slab / floor', 'slabs / floors',
                  'hollow core', 'hollowcore', 'precast floor'}
    wall_names = {'wall', 'walls', 'shear wall', 'shear walls', 'core wall',
                  'core walls', 'retaining wall', 'retaining walls', 'retaining'}
    found_names = {'foundation', 'foundations', 'footing', 'footings',
                   'pile', 'piles', 'pile cap', 'pile caps', 'pilecap',
                   'ground beam', 'pad', 'pads', 'substructure',
                   'strip footing', 'mat foundation', 'raft'}
    stair_names = {'stair', 'stairs', 'staircase', 'staircases', 'step',
                   'steps', 'ramp', 'ramps', 'landing', 'landings'}

    if raw_lower in beam_names:
        return 'Beams'
    if raw_lower in col_names:
        return 'Columns'
    if raw_lower in slab_names:
        return 'Slabs / Floors'
    if raw_lower in wall_names:
        return 'Walls'
    if raw_lower in found_names:
        return 'Foundations'
    if raw_lower in stair_names:
        return 'Stairs'

    # Partial match fallback
    for kw in ['beam', 'girder', 'lintel']:
        if kw in raw_lower:
            return 'Beams'
    for kw in ['column', 'col', 'pillar']:
        if kw in raw_lower:
            return 'Columns'
    for kw in ['slab', 'floor', 'deck']:
        if kw in raw_lower:
            return 'Slabs / Floors'
    for kw in ['wall', 'retaining']:
        if kw in raw_lower:
            return 'Walls'
    for kw in ['foundation', 'footing', 'pile', 'pad']:
        if kw in raw_lower:
            return 'Foundations'
    for kw in ['stair', 'ramp', 'landing']:
        if kw in raw_lower:
            return 'Stairs'

    # If nothing matches, keep original with title case
    return raw.strip().title() if raw.strip() else 'Other'


def _build_member_summary(detailed_df, project_area):
    """
    Build a summary DataFrame grouped by structural member type.

    Returns a DataFrame with columns:
        Member Type, Total Mass (kg),
        A1-A3 (tCO2e), A4 (tCO2e), A5 (tCO2e),
        Total Emission (tCO2e), % of Total, Per m² (kgCO2e/m²)
    """
    if detailed_df is None or detailed_df.empty:
        return pd.DataFrame()

    df = detailed_df.copy()
    df['_member_type'] = df.apply(_classify_member_type, axis=1)

    # Ensure numeric
    for c in ['Mass(kg)', 'A1-A3 Emission(kgCO2e)', 'A4 Emission(kgCO2e)',
              'A5 Emission(kgCO2e)', 'Total Emission(tCO2e)']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)

    grouped = df.groupby('_member_type', sort=False).agg(
        total_mass=('Mass(kg)', 'sum'),
        a1_a3=('A1-A3 Emission(kgCO2e)', 'sum'),
        a4=('A4 Emission(kgCO2e)', 'sum'),
        a5=('A5 Emission(kgCO2e)', 'sum'),
        total_ton=('Total Emission(tCO2e)', 'sum'),
    ).reset_index()

    grand_total = grouped['total_ton'].sum()
    grouped['pct'] = grouped['total_ton'].apply(
        lambda x: (x / grand_total * 100) if grand_total > 0 else 0
    )
    grouped['per_sqm'] = grouped['total_ton'].apply(
        lambda x: (x * 1000 / project_area) if project_area > 0 else 0
    )

    # Convert A1-A3, A4, A5 from kg to ton
    for c in ['a1_a3', 'a4', 'a5']:
        grouped[c] = grouped[c] / 1000

    # Sort by total descending
    grouped = grouped.sort_values('total_ton', ascending=False).reset_index(drop=True)

    grouped.columns = [
        'Member Type', 'Total Mass (kg)',
        'A1-A3 (tCO2e)', 'A4 (tCO2e)', 'A5 (tCO2e)',
        'Total Emission (tCO2e)', '% of Total', 'Per m² (kgCO2e/m²)'
    ]

    return grouped


def generate_excel_report(data, output_file, include_charts=True):
    """Generate comprehensive Excel report with styling."""
    if not OPENPYXL_AVAILABLE:
        print("Warning: openpyxl not installed. Install with: pip install openpyxl")
        return None

    output_dir = os.path.dirname(output_file)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)

    wb = Workbook()

    # Create sheets
    _create_comprehensive_report_sheet(wb, data)
    _create_calculations_sheet(wb, data)
    _create_combined_summary_sheet(wb, data)

    wb.save(output_file)
    print(f"Excel report generated: {output_file}")
    return output_file


def _create_comprehensive_report_sheet(wb, data):
    """Create the main comprehensive report sheet with all sections."""
    ws = wb.active
    ws.title = "Comprehensive Report"

    current_row = 1

    # ===== TITLE SECTION =====
    ws.merge_cells(f'A{current_row}:H{current_row}')
    ws[f'A{current_row}'] = "C.L.E.A.R. - Carbon Lifecycle Evaluation and Reporting"
    ws[f'A{current_row}'].font = TITLE_FONT
    ws[f'A{current_row}'].alignment = CENTER_ALIGN
    current_row += 1

    ws.merge_cells(f'A{current_row}:H{current_row}')
    ws[f'A{current_row}'] = f"Report Generated: {data.report_date}"
    ws[f'A{current_row}'].font = Font(italic=True, size=10)
    ws[f'A{current_row}'].alignment = CENTER_ALIGN
    current_row += 3

    # ===== PROJECT INFORMATION SECTION =====
    ws.merge_cells(f'A{current_row}:D{current_row}')
    ws[f'A{current_row}'] = "PROJECT INFORMATION"
    ws[f'A{current_row}'].font = SECTION_FONT
    ws[f'A{current_row}'].fill = SECTION_FILL
    _apply_border_range(ws, current_row, 1, current_row, 4)
    current_row += 1

    project_info = data.project_info
    info_rows = [
        ("Project Name:", project_info.get('project_name', 'N/A')),
        ("Location:", project_info.get('location', 'N/A')),
        ("Client:", project_info.get('client_name', 'N/A')),
        ("Project Type:", project_info.get('project_type', 'N/A')),
        ("Project Stage:", project_info.get('project_stage', 'N/A')),
        ("Project Area:", f"{data.project_area:.2f} m²"),
    ]
    # Stage-aware assessment metadata (present when run via the web app)
    _badge = project_info.get('stage_badge')
    _unc = project_info.get('uncertainty_pct') or (data.metrics or {}).get('uncertainty_pct')
    if _badge:
        info_rows.append(("Assessment Level:", str(_badge)))
    if _unc:
        try:
            _ps = float((data.metrics or {}).get('total_emission_per_sqm', 0))
            _u = float(_unc)
            info_rows.append((
                "Expected Range:",
                f"{_ps * (1 - _u / 100):.0f}–{_ps * (1 + _u / 100):.0f} kgCO2e/m² "
                f"(±{_u:g}% at this design stage)"))
        except (TypeError, ValueError):
            pass
    _catver = (getattr(data, 'catalogue_version', '')
               or project_info.get('catalogue_version', ''))
    if _catver:
        info_rows.append(("Emission Data Version:", str(_catver)))

    for label, value in info_rows:
        ws[f'A{current_row}'] = label
        ws[f'A{current_row}'].font = Font(bold=True, size=10)
        ws[f'B{current_row}'] = str(value)
        ws[f'B{current_row}'].font = DATA_FONT
        _apply_border_range(ws, current_row, 1, current_row, 2)
        current_row += 1

    current_row += 2

    # ===== TRANSPORTATION DISTANCES SECTION =====
    ws.merge_cells(f'A{current_row}:D{current_row}')
    ws[f'A{current_row}'] = "TRANSPORTATION DISTANCES"
    ws[f'A{current_row}'].font = SECTION_FONT
    ws[f'A{current_row}'].fill = SECTION_FILL
    _apply_border_range(ws, current_row, 1, current_row, 4)
    current_row += 1

    # Headers
    headers = ["Material", "Sea Distance (km)", "Road Distance (km)"]
    for col, header in enumerate(headers, 1):
        ws.cell(row=current_row, column=col, value=header)
        ws.cell(row=current_row, column=col).font = SUBHEADER_FONT
        ws.cell(row=current_row, column=col).fill = SUBHEADER_FILL
        ws.cell(row=current_row, column=col).alignment = CENTER_ALIGN
        ws.cell(row=current_row, column=col).border = THIN_BORDER
    current_row += 1

    distances = data.distances
    # SEAI Table 2: per-material transport distances actually used this run.
    distance_rows = [
        (meta['label'],
         distances.get(f'{fam}_sea_distance', meta['sea']),
         distances.get(f'{fam}_road_distance', meta['road']))
        for fam, meta in SEAI_TRANSPORT_TABLE.items()
    ]

    for material, sea, road in distance_rows:
        ws.cell(row=current_row, column=1, value=material).font = DATA_FONT
        ws.cell(row=current_row, column=2, value=sea).font = DATA_FONT
        ws.cell(row=current_row, column=3, value=road).font = DATA_FONT
        ws.cell(row=current_row, column=2).alignment = CENTER_ALIGN
        ws.cell(row=current_row, column=3).alignment = CENTER_ALIGN
        _apply_border_range(ws, current_row, 1, current_row, 3)
        current_row += 1

    current_row += 2

    # ===== KEY METRICS SECTION =====
    ws.merge_cells(f'A{current_row}:D{current_row}')
    ws[f'A{current_row}'] = "KEY METRICS"
    ws[f'A{current_row}'].font = SECTION_FONT
    ws[f'A{current_row}'].fill = SECTION_FILL
    _apply_border_range(ws, current_row, 1, current_row, 4)
    current_row += 1

    metrics = data.metrics
    metrics_rows = [
        ("Total Emissions:", f"{metrics['total_emission_ton']:.2f} tCO2e"),
        ("Emissions per m²:", f"{metrics['total_emission_per_sqm']:.2f} kgCO2e/m²"),
        ("Efficiency Rating:", data.efficiency_rating),
    ]
    # Biogenic carbon stored (timber) — separate line, not netted into the total
    _seq = metrics.get('sequestration_ton', 0) or 0
    if _seq < 0:
        metrics_rows.append(
            ("Biogenic Carbon Stored:",
             f"{_seq:.2f} tCO2e (timber sequestration — reported separately, "
             f"not in the A1-A5 total; EN 16485)"))

    for label, value in metrics_rows:
        ws[f'A{current_row}'] = label
        ws[f'A{current_row}'].font = Font(bold=True, size=10)
        ws[f'B{current_row}'] = str(value)
        ws[f'B{current_row}'].font = DATA_FONT
        _apply_border_range(ws, current_row, 1, current_row, 2)
        current_row += 1

    current_row += 2

    # ===== MATERIAL BREAKDOWN SECTION =====
    ws.merge_cells(f'A{current_row}:H{current_row}')
    ws[f'A{current_row}'] = "MATERIAL BREAKDOWN"
    ws[f'A{current_row}'].font = SECTION_FONT
    ws[f'A{current_row}'].fill = SECTION_FILL
    _apply_border_range(ws, current_row, 1, current_row, 8)
    current_row += 1

    # Headers
    breakdown_headers = ["Material Type", "Emission (tCO2e)", "% of Total", "Per m² (kgCO2e/m²)"]
    for col, header in enumerate(breakdown_headers, 1):
        ws.cell(row=current_row, column=col, value=header)
        ws.cell(row=current_row, column=col).font = SUBHEADER_FONT
        ws.cell(row=current_row, column=col).fill = SUBHEADER_FILL
        ws.cell(row=current_row, column=col).alignment = CENTER_ALIGN
        ws.cell(row=current_row, column=col).border = THIN_BORDER
    current_row += 1

    summary_df = data.summary_df
    total_emission = metrics['total_emission_ton']
    project_area = data.project_area

    for idx, row in summary_df.iterrows():
        emission = row['Total Emission(tCO2e)']
        percentage = (emission / total_emission) * 100 if total_emission > 0 else 0
        per_sqm = (emission * 1000 / project_area) if project_area > 0 else 0

        ws.cell(row=current_row, column=1, value=row['Material Type']).font = DATA_FONT
        ws.cell(row=current_row, column=2, value=round(emission, 2)).font = DATA_FONT
        ws.cell(row=current_row, column=3, value=f"{percentage:.1f}%").font = DATA_FONT
        ws.cell(row=current_row, column=4, value=round(per_sqm, 2)).font = DATA_FONT

        ws.cell(row=current_row, column=2).alignment = CENTER_ALIGN
        ws.cell(row=current_row, column=3).alignment = CENTER_ALIGN
        ws.cell(row=current_row, column=4).alignment = CENTER_ALIGN
        _apply_border_range(ws, current_row, 1, current_row, 4)
        current_row += 1

    # Total row
    mat_total_vals = ["TOTAL", round(total_emission, 2), "100.0%",
                      round(metrics['total_emission_per_sqm'], 2)]
    for col, val in enumerate(mat_total_vals, 1):
        cell = ws.cell(row=current_row, column=col, value=val)
        cell.font = TOTAL_FONT
        cell.fill = TOTAL_FILL
        cell.border = THIN_BORDER
        cell.alignment = CENTER_ALIGN if col > 1 else LEFT_ALIGN
    current_row += 3

    # ===== STRUCTURAL MEMBER BREAKDOWN SECTION =====
    member_summary = _build_member_summary(data.detailed_df, data.project_area)
    if not member_summary.empty:
        ws.merge_cells(f'A{current_row}:H{current_row}')
        ws[f'A{current_row}'] = "STRUCTURAL MEMBER BREAKDOWN"
        ws[f'A{current_row}'].font = SECTION_FONT
        ws[f'A{current_row}'].fill = SECTION_FILL
        _apply_border_range(ws, current_row, 1, current_row, 8)
        current_row += 1

        member_headers = [
            "Member Type", "Total Mass (kg)",
            "A1-A3 (tCO2e)", "A4 (tCO2e)", "A5 (tCO2e)",
            "Total (tCO2e)", "% of Total", "Per m² (kgCO2e/m²)"
        ]
        for col, header in enumerate(member_headers, 1):
            cell = ws.cell(row=current_row, column=col, value=header)
            cell.font = SUBHEADER_FONT
            cell.fill = SUBHEADER_FILL
            cell.alignment = WRAP_ALIGN
            cell.border = THIN_BORDER
        current_row += 1

        grand_total_mass = 0
        grand_a1_a3 = 0
        grand_a4 = 0
        grand_a5 = 0

        for idx, mrow in member_summary.iterrows():
            member_type = mrow['Member Type']
            row_fill = MEMBER_COLORS.get(member_type, MEMBER_COLORS.get('Other'))
            alt_fill = ACCENT_FILL if idx % 2 == 0 else None

            values = [
                member_type,
                round(mrow['Total Mass (kg)'], 2),
                round(mrow['A1-A3 (tCO2e)'], 2),
                round(mrow['A4 (tCO2e)'], 2),
                round(mrow['A5 (tCO2e)'], 2),
                round(mrow['Total Emission (tCO2e)'], 2),
                f"{mrow['% of Total']:.1f}%",
                round(mrow['Per m² (kgCO2e/m²)'], 2),
            ]

            grand_total_mass += mrow['Total Mass (kg)']
            grand_a1_a3 += mrow['A1-A3 (tCO2e)']
            grand_a4 += mrow['A4 (tCO2e)']
            grand_a5 += mrow['A5 (tCO2e)']

            for col, val in enumerate(values, 1):
                cell = ws.cell(row=current_row, column=col, value=val)
                cell.font = DATA_FONT
                cell.border = THIN_BORDER
                if col == 1:
                    cell.alignment = LEFT_ALIGN
                    if row_fill:
                        cell.fill = row_fill
                else:
                    cell.alignment = CENTER_ALIGN
                    if alt_fill:
                        cell.fill = alt_fill
            current_row += 1

        # Total row
        total_values = [
            "TOTAL",
            round(grand_total_mass, 2),
            round(grand_a1_a3, 2),
            round(grand_a4, 2),
            round(grand_a5, 2),
            round(total_emission, 2),
            "100.0%",
            round(metrics['total_emission_per_sqm'], 2),
        ]
        for col, val in enumerate(total_values, 1):
            cell = ws.cell(row=current_row, column=col, value=val)
            cell.font = TOTAL_FONT
            cell.fill = TOTAL_FILL
            cell.alignment = CENTER_ALIGN
            cell.border = THIN_BORDER
            if col == 1:
                cell.alignment = LEFT_ALIGN
        current_row += 3

    # ===== LIFE CYCLE STAGE ANALYSIS SECTION =====
    ws.merge_cells(f'A{current_row}:H{current_row}')
    ws[f'A{current_row}'] = "LIFE CYCLE STAGE ANALYSIS"
    ws[f'A{current_row}'].font = SECTION_FONT
    ws[f'A{current_row}'].fill = SECTION_FILL
    _apply_border_range(ws, current_row, 1, current_row, 8)
    current_row += 1

    # Life cycle stages
    stage_headers = ["Stage", "Description", "Emission (tCO2e)", "% of Total"]
    for col, header in enumerate(stage_headers, 1):
        ws.cell(row=current_row, column=col, value=header)
        ws.cell(row=current_row, column=col).font = SUBHEADER_FONT
        ws.cell(row=current_row, column=col).fill = SUBHEADER_FILL
        ws.cell(row=current_row, column=col).alignment = CENTER_ALIGN
        ws.cell(row=current_row, column=col).border = THIN_BORDER
    current_row += 1

    # Calculate stage emissions from detailed_df (includes A5a from SEAI methodology)
    detailed_df = data.detailed_df
    stage_emissions = data.stage_emissions if hasattr(data, 'stage_emissions') else None
    stage_data = _calculate_life_cycle_stages(detailed_df, total_emission, stage_emissions,
                                              a5a_factor=_a5a_factor_of(data))

    for stage, desc, emission, pct in stage_data:
        ws.cell(row=current_row, column=1, value=stage).font = DATA_FONT
        ws.cell(row=current_row, column=2, value=desc).font = DATA_FONT
        ws.cell(row=current_row, column=3, value=round(emission, 2)).font = DATA_FONT
        ws.cell(row=current_row, column=4, value=f"{pct:.1f}%").font = DATA_FONT
        ws.cell(row=current_row, column=3).alignment = CENTER_ALIGN
        ws.cell(row=current_row, column=4).alignment = CENTER_ALIGN
        _apply_border_range(ws, current_row, 1, current_row, 4)
        current_row += 1

    current_row += 2

    # ===== EFFICIENCY RATING SCALE =====
    ws.merge_cells(f'A{current_row}:D{current_row}')
    ws[f'A{current_row}'] = "EFFICIENCY RATING SCALE"
    ws[f'A{current_row}'].font = SECTION_FONT
    ws[f'A{current_row}'].fill = SECTION_FILL
    _apply_border_range(ws, current_row, 1, current_row, 4)
    current_row += 1

    rating_scale = [
        ("A++", "≤50", "Outstanding", "006400"),
        ("A+", "51-100", "Excellent", "228B22"),
        ("A", "101-150", "Very Good", "32CD32"),
        ("B", "151-200", "SCORS Target", "9ACD32"),
        ("C", "201-250", "Good", "FFD700"),
        ("D", "251-300", "Average", "FFA500"),
        ("E", "301-350", "Below Average", "FF8C00"),
        ("F", "351-400", "Poor", "FF4500"),
        ("G", ">400", "Very Poor", "DC143C"),
    ]

    for rating, threshold, desc, color in rating_scale:
        highlight = rating == data.efficiency_rating
        ws.cell(row=current_row, column=1, value=rating)
        ws.cell(row=current_row, column=2, value=f"{threshold} kgCO2e/m²")
        ws.cell(row=current_row, column=3, value=desc)

        if highlight:
            for col in range(1, 4):
                ws.cell(row=current_row, column=col).fill = PatternFill(start_color=color, end_color=color, fill_type="solid")
                ws.cell(row=current_row, column=col).font = Font(bold=True, color="FFFFFF")
        else:
            for col in range(1, 4):
                ws.cell(row=current_row, column=col).font = DATA_FONT

        _apply_border_range(ws, current_row, 1, current_row, 3)
        current_row += 1

    current_row += 2

    # ===== METHODOLOGY & ASSUMPTIONS SECTION =====
    ws.merge_cells(f'A{current_row}:H{current_row}')
    ws[f'A{current_row}'] = "METHODOLOGY & ASSUMPTIONS"
    ws[f'A{current_row}'].font = SECTION_FONT
    ws[f'A{current_row}'].fill = SECTION_FILL
    _apply_border_range(ws, current_row, 1, current_row, 8)
    current_row += 1

    stage = data.project_info.get('stage', '')
    if 'Tender' in stage:
        stage_note = "Tender Stage: Supplier EPDs used where available."
    elif 'Detailed' in stage:
        stage_note = "Detailed Design: Generic catalogue rates used; override with EPDs if available."
    else:
        stage_note = "Concept / Schematic Stage: Generic industry factors applied (±20-30% accuracy)."

    # A5a factor this assessment was actually run with (override or SEAI default).
    _a5a = _a5a_factor_of(data)
    if _a5a_is_override(data):
        a5a_notes = [
            "A5 construction activities emissions use a project-specific site-activity factor:",
            f"  - Applied to this assessment: {_a5a:g} kgCO2e/m² GIA (user override)",
            f"  - Supersedes the SEAI default of {A5A_DEFAULT:g} kgCO2e/m² GIA "
            f"(70% of the 40 kgCO2e/m² full-building figure).",
        ]
    else:
        a5a_notes = [
            "A5 construction activities emissions calculated per SEAI methodology:",
            "  - Full building factor: 40 kgCO2e/m² GIA",
            f"  - Structural scope (70%): {_a5a:g} kgCO2e/m² GIA applied to this assessment.",
        ]
    methodology_notes = [
        stage_note,
        "Assessment follows EN 15978:2011 methodology for upfront embodied carbon (A1-A5).",
        "Data sourced from IGBC. Methodology adopted from SEAI and Construct Innovate.",
        "A4 transport per SEAI methodology:",
        f"  - Carbon factors (Table 3): road {SEAI_CF_OUTWARD_ROAD:g} outward (average laden) "
        f"+ {SEAI_CF_RETURN_ROAD:g} return (0% laden, derived); sea {SEAI_CF_SEA:g} kgCO2e/kg.km, single leg.",
        "  - Default distances by material from SEAI Table 2 (see Transport Distances).",
        "A5w site waste per SEAI A5.3: waste% x (A1-A3 + A4); end-of-life C2/C4 excluded (A1-A5 scope).",
        "Post-tensioning: no generic PT factor exists in the IGBC database - defaults to the Ireland "
        "rebar factor (0.737 kgCO2e/kg) per IStructE guidance, unless a named supplier EPD is selected.",
        *a5a_notes,
    ]

    for note in methodology_notes:
        ws[f'A{current_row}'] = note
        ws[f'A{current_row}'].font = Font(size=9, italic=True)
        current_row += 1

    current_row += 1

    # ── Rebar Rates Used ──────────────────────────────────────────────────────
    rebar_rates_used = getattr(data, 'rebar_rates_used', []) or []
    if rebar_rates_used:
        ws.merge_cells(f'A{current_row}:D{current_row}')
        ws[f'A{current_row}'] = "REBAR RATES USED"
        ws[f'A{current_row}'].font = Font(bold=True, size=10, color="1F4E79")
        ws[f'A{current_row}'].fill = PatternFill(start_color="EBF3FB", end_color="EBF3FB", fill_type="solid")
        current_row += 1
        for h, col in [("Element", 'A'), ("Rate (kg/m³)", 'B')]:
            ws[f'{col}{current_row}'] = h
            ws[f'{col}{current_row}'].font = Font(bold=True, size=9)
            ws[f'{col}{current_row}'].fill = SUBHEADER_FILL
            ws[f'{col}{current_row}'].font = Font(bold=True, size=9, color="FFFFFF")
        current_row += 1
        for r in rebar_rates_used:
            ws[f'A{current_row}'] = r['element']
            ws[f'B{current_row}'] = r['rate_kg_m3']
            ws[f'A{current_row}'].font = Font(size=9)
            ws[f'B{current_row}'].font = Font(size=9)
            ws[f'B{current_row}'].alignment = Alignment(horizontal='center')
            current_row += 1
        current_row += 1

    # ── Concrete Strength & GGBS Content ─────────────────────────────────────
    concrete_specs_used = getattr(data, 'concrete_specs_used', []) or []
    if concrete_specs_used:
        ws.merge_cells(f'A{current_row}:E{current_row}')
        ws[f'A{current_row}'] = "CONCRETE STRENGTH & GGBS CONTENT"
        ws[f'A{current_row}'].font = Font(bold=True, size=10, color="1F4E79")
        ws[f'A{current_row}'].fill = PatternFill(start_color="EBF3FB", end_color="EBF3FB", fill_type="solid")
        current_row += 1
        for h, col in [("Element", 'A'), ("Grade (MPa)", 'B'), ("GGBS %", 'C'), ("A1-A3 (kgCO2e/kg)", 'D')]:
            ws[f'{col}{current_row}'] = h
            ws[f'{col}{current_row}'].font = Font(bold=True, size=9, color="FFFFFF")
            ws[f'{col}{current_row}'].fill = SUBHEADER_FILL
            ws[f'{col}{current_row}'].alignment = Alignment(horizontal='center')
        current_row += 1
        for s in concrete_specs_used:
            ws[f'A{current_row}'] = s['element']
            ws[f'B{current_row}'] = s['grade']
            ws[f'C{current_row}'] = f"{s['ggbs_pct']}%"
            ws[f'D{current_row}'] = round(s['a1_a3'], 4)
            for col in ('A', 'B', 'C', 'D'):
                ws[f'{col}{current_row}'].font = Font(size=9)
                ws[f'{col}{current_row}'].alignment = Alignment(horizontal='center' if col != 'A' else 'left')
            current_row += 1
        current_row += 1

    # ── Emission Factors Used (no e_id) ──────────────────────────────────────
    emission_factors_used = getattr(data, 'emission_factors_used', []) or []
    if emission_factors_used:
        ws.merge_cells(f'A{current_row}:F{current_row}')
        ws[f'A{current_row}'] = "EMISSION FACTORS USED"
        ws[f'A{current_row}'].font = Font(bold=True, size=10, color="1F4E79")
        ws[f'A{current_row}'].fill = PatternFill(start_color="EBF3FB", end_color="EBF3FB", fill_type="solid")
        current_row += 1
        for h, col in [("Material", 'A'), ("Product Name", 'B'),
                       ("A1-A3 (kgCO2e/kg)", 'C'), ("Density (kg/m³)", 'D'), ("Data Type", 'E')]:
            ws[f'{col}{current_row}'] = h
            ws[f'{col}{current_row}'].font = Font(bold=True, size=9, color="FFFFFF")
            ws[f'{col}{current_row}'].fill = SUBHEADER_FILL
            ws[f'{col}{current_row}'].alignment = Alignment(horizontal='center')
        current_row += 1
        for f in emission_factors_used:
            ws[f'A{current_row}'] = f['material']
            ws[f'B{current_row}'] = f['name']
            ws[f'C{current_row}'] = round(f['a1_a3'], 4)
            ws[f'D{current_row}'] = round(f['density'], 1)
            ws[f'E{current_row}'] = 'Custom (User-entered)' if f.get('is_custom') else f.get('data_type', 'Generic')
            for col in ('A', 'B', 'C', 'D', 'E'):
                ws[f'{col}{current_row}'].font = Font(size=9)
                ws[f'{col}{current_row}'].alignment = Alignment(horizontal='center' if col not in ('A', 'B') else 'left')
            current_row += 1

    # ── Transport Distances ───────────────────────────────────────────────────
    distances = getattr(data, 'distances', {})
    if distances:
        current_row += 1
        ws.merge_cells(f'A{current_row}:D{current_row}')
        ws[f'A{current_row}'] = "TRANSPORT DISTANCES USED"
        ws[f'A{current_row}'].font = Font(bold=True, size=10, color="1F4E79")
        ws[f'A{current_row}'].fill = PatternFill(start_color="EBF3FB", end_color="EBF3FB", fill_type="solid")
        current_row += 1
        for h, col in [("Material", 'A'), ("Sea (km)", 'B'), ("Road (km)", 'C')]:
            ws[f'{col}{current_row}'] = h
            ws[f'{col}{current_row}'].font = Font(bold=True, size=9, color="FFFFFF")
            ws[f'{col}{current_row}'].fill = SUBHEADER_FILL
            ws[f'{col}{current_row}'].alignment = Alignment(horizontal='center')
        current_row += 1
        for fam, meta in SEAI_TRANSPORT_TABLE.items():
            ws[f'A{current_row}'] = meta['label']
            ws[f'B{current_row}'] = distances.get(f'{fam}_sea_distance', meta['sea'])
            ws[f'C{current_row}'] = distances.get(f'{fam}_road_distance', meta['road'])
            for col in ('A', 'B', 'C'):
                ws[f'{col}{current_row}'].font = Font(size=9)
                ws[f'{col}{current_row}'].alignment = Alignment(horizontal='center' if col != 'A' else 'left')
            current_row += 1

    # Set column widths
    ws.column_dimensions['A'].width = 30
    ws.column_dimensions['B'].width = 28
    ws.column_dimensions['C'].width = 20
    ws.column_dimensions['D'].width = 22
    ws.column_dimensions['E'].width = 20
    ws.column_dimensions['F'].width = 16
    ws.column_dimensions['G'].width = 18
    ws.column_dimensions['H'].width = 14
    ws.column_dimensions['I'].width = 20


def _create_calculations_sheet(wb, data):
    """Create the detailed calculations sheet with improved formatting."""
    ws = wb.create_sheet("Detailed Calculations")

    # Drop e_id column for display
    detailed_df = data.detailed_df
    display_cols = [c for c in detailed_df.columns if c != 'e_id']
    display_df = detailed_df[display_cols]

    num_cols = len(display_df.columns)
    last_col_letter = get_column_letter(num_cols)

    # Title
    ws.merge_cells(f'A1:{last_col_letter}1')
    ws['A1'] = "Detailed Emission Calculations"
    ws['A1'].font = TITLE_FONT
    ws['A1'].alignment = CENTER_ALIGN

    # Subtitle
    ws.merge_cells(f'A2:{last_col_letter}2')
    ws['A2'] = f"Project: {data.project_info.get('project_name', '')}  |  Area: {data.project_area:,.2f} m²  |  {data.report_date}"
    ws['A2'].font = Font(italic=True, size=9, color="666666")
    ws['A2'].alignment = CENTER_ALIGN

    # Headers at row 4
    header_labels = {
        'Mass(kg)': 'Mass (kg)',
        'Total Volume(m3)': 'Volume (m³)',
        'Density(kg/m3)': 'Density (kg/m³)',
        'A1-A3 Emission(kgCO2e)': 'A1-A3 (kgCO2e)',
        'A4 Emission(kgCO2e)': 'A4 (kgCO2e)',
        'A5 Emission(kgCO2e)': 'A5 (kgCO2e)',
        'A1-A5 Emission(tCO2e)': 'A1-A5 (tCO2e)',
        'Total Emission(tCO2e)': 'Total (tCO2e)',
    }

    for col, header in enumerate(display_df.columns, 1):
        label = header_labels.get(header, header)
        cell = ws.cell(row=4, column=col, value=label)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = WRAP_ALIGN
        cell.border = THIN_BORDER

    # Data rows with alternating fills
    for i, (_, row) in enumerate(display_df.iterrows()):
        row_num = i + 5
        row_fill = ACCENT_FILL if i % 2 == 0 else None
        for col_idx, value in enumerate(row, 1):
            cell = ws.cell(row=row_num, column=col_idx, value=value)
            cell.font = DATA_FONT
            cell.border = THIN_BORDER
            if isinstance(value, (int, float)):
                cell.alignment = CENTER_ALIGN
                if isinstance(value, float):
                    cell.number_format = '#,##0.00'
            else:
                cell.alignment = LEFT_ALIGN
            if row_fill:
                cell.fill = row_fill

    # Summary row at bottom
    last_data_row = len(display_df) + 5
    ws.cell(row=last_data_row, column=1, value="TOTALS")
    ws.cell(row=last_data_row, column=1).font = TOTAL_FONT
    ws.cell(row=last_data_row, column=1).fill = TOTAL_FILL
    ws.cell(row=last_data_row, column=1).border = THIN_BORDER

    sum_cols = {
        'Mass(kg)', 'A1-A3 Emission(kgCO2e)',
        'A4 Emission(kgCO2e)', 'A5 Emission(kgCO2e)',
        'A1-A5 Emission(tCO2e)', 'Total Emission(tCO2e)',
        'Total Volume(m3)',
    }
    for col_idx, header in enumerate(display_df.columns, 1):
        cell = ws.cell(row=last_data_row, column=col_idx)
        cell.font = TOTAL_FONT
        cell.fill = TOTAL_FILL
        cell.border = THIN_BORDER
        cell.alignment = CENTER_ALIGN
        if header in sum_cols:
            val = pd.to_numeric(display_df[header], errors='coerce').fillna(0).sum()
            cell.value = round(val, 2)
            cell.number_format = '#,##0.00'

    # Column widths
    col_width_map = {
        'Category': 14, 'Material': 18, 'Description': 24,
        'Correction Formula': 16, 'Waste': 10, 'Total Volume(m3)': 14,
        'Density(kg/m3)': 14, 'Mass(kg)': 16,
        'A1-A3 Emission(kgCO2e)': 16, 'A4 Emission(kgCO2e)': 14,
        'A5 Emission(kgCO2e)': 14, 'A1-A5 Emission(tCO2e)': 16,
        'Total Emission(tCO2e)': 16,
    }
    for col_idx, header in enumerate(display_df.columns, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = col_width_map.get(header, 15)

    # Freeze header
    ws.freeze_panes = 'A5'


def _create_combined_summary_sheet(wb, data):
    """Create a single combined summary sheet with material breakdown,
    member type breakdown, and per-element detail."""
    ws = wb.create_sheet("Summary & Breakdown")

    detailed_df = data.detailed_df
    project_area = data.project_area
    metrics = data.metrics
    total_emission = metrics['total_emission_ton']
    summary_df = data.summary_df

    current_row = 1

    # ===== SHEET TITLE =====
    ws.merge_cells(f'A{current_row}:H{current_row}')
    ws[f'A{current_row}'] = "C.L.E.A.R. — Emission Summary & Breakdown"
    ws[f'A{current_row}'].font = TITLE_FONT
    ws[f'A{current_row}'].alignment = CENTER_ALIGN
    current_row += 1

    ws.merge_cells(f'A{current_row}:H{current_row}')
    project_name = data.project_info.get('project_name', '')
    ws[f'A{current_row}'] = f"{project_name}  |  {data.project_area:,.2f} m²  |  {data.report_date}"
    ws[f'A{current_row}'].font = Font(italic=True, size=9, color="666666")
    ws[f'A{current_row}'].alignment = CENTER_ALIGN
    current_row += 2

    # ===== SECTION 1: MATERIAL BREAKDOWN =====
    ws.merge_cells(f'A{current_row}:D{current_row}')
    ws[f'A{current_row}'] = "MATERIAL BREAKDOWN"
    ws[f'A{current_row}'].font = SECTION_FONT
    ws[f'A{current_row}'].fill = SECTION_FILL
    _apply_border_range(ws, current_row, 1, current_row, 4)
    current_row += 1

    mat_headers = ["Material Type", "Emission (tCO2e)", "% of Total",
                   "Per m² (kgCO2e/m²)"]
    for col, header in enumerate(mat_headers, 1):
        cell = ws.cell(row=current_row, column=col, value=header)
        cell.font = SUBHEADER_FONT
        cell.fill = SUBHEADER_FILL
        cell.alignment = WRAP_ALIGN
        cell.border = THIN_BORDER
    current_row += 1

    mat_colors = {
        'Concrete': PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid"),
        'Rebar': PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid"),
        'Steel Section': PatternFill(start_color="FECACA", end_color="FECACA", fill_type="solid"),
        'Steel': PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid"),
        'Post Tensioning': PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid"),
        'Timber': PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid"),
    }

    for _, row in summary_df.iterrows():
        emission = row['Total Emission(tCO2e)']
        percentage = (emission / total_emission * 100) if total_emission > 0 else 0
        per_sqm = (emission * 1000 / project_area) if project_area > 0 else 0
        mat_name = row['Material Type']
        mfill = mat_colors.get(mat_name)

        vals = [mat_name, round(emission, 2), f"{percentage:.1f}%", round(per_sqm, 2)]
        for col, val in enumerate(vals, 1):
            cell = ws.cell(row=current_row, column=col, value=val)
            cell.font = DATA_FONT
            cell.border = THIN_BORDER
            cell.alignment = CENTER_ALIGN if col > 1 else LEFT_ALIGN
            if col == 1 and mfill:
                cell.fill = mfill
                cell.font = Font(bold=True, size=10)
        current_row += 1

    # Total row
    mat_total_row = current_row
    total_vals = ["TOTAL", round(total_emission, 2), "100.0%",
                  round(metrics['total_emission_per_sqm'], 2)]
    for col, val in enumerate(total_vals, 1):
        cell = ws.cell(row=current_row, column=col, value=val)
        cell.font = TOTAL_FONT
        cell.fill = TOTAL_FILL
        cell.border = THIN_BORDER
        cell.alignment = CENTER_ALIGN if col > 1 else LEFT_ALIGN
    current_row += 1

    # Remember where material data rows are for charts
    # Material names in col A, emissions in col B (rows mat_data_start to mat_total_row-1)
    mat_data_start = mat_total_row - len(summary_df)

    # --- Write lifecycle stage chart data to the right (cols F-G) ---
    stage_emissions = data.stage_emissions if hasattr(data, 'stage_emissions') else {}
    stage_data_row = mat_data_start - 1  # header row aligned with material header
    ws.cell(row=stage_data_row, column=6, value="Life Cycle Stage").font = SUBHEADER_FONT
    ws.cell(row=stage_data_row, column=6).fill = SUBHEADER_FILL
    ws.cell(row=stage_data_row, column=6).border = THIN_BORDER
    ws.cell(row=stage_data_row, column=6).alignment = CENTER_ALIGN
    ws.cell(row=stage_data_row, column=7, value="Emission (tCO2e)").font = SUBHEADER_FONT
    ws.cell(row=stage_data_row, column=7).fill = SUBHEADER_FILL
    ws.cell(row=stage_data_row, column=7).border = THIN_BORDER
    ws.cell(row=stage_data_row, column=7).alignment = CENTER_ALIGN

    stage_items = [
        ("A1-A3", stage_emissions.get('A1-A3', 0)),
        ("A4", stage_emissions.get('A4', 0)),
        ("A5", stage_emissions.get('A5', 0)),
    ]
    for si, (sname, sval) in enumerate(stage_items):
        r = stage_data_row + 1 + si
        ws.cell(row=r, column=6, value=sname).font = DATA_FONT
        ws.cell(row=r, column=6).border = THIN_BORDER
        ws.cell(row=r, column=6).alignment = CENTER_ALIGN
        ws.cell(row=r, column=7, value=round(sval, 2)).font = DATA_FONT
        ws.cell(row=r, column=7).border = THIN_BORDER
        ws.cell(row=r, column=7).alignment = CENTER_ALIGN
    stage_data_end = stage_data_row + len(stage_items)

    # --- Build member summary early so we can write chart data ---
    member_summary = _build_member_summary(detailed_df, project_area)

    # --- Write member chart data to the right (cols I-J) ---
    if not member_summary.empty:
        ws.cell(row=stage_data_row, column=9, value="Member Type").font = SUBHEADER_FONT
        ws.cell(row=stage_data_row, column=9).fill = SUBHEADER_FILL
        ws.cell(row=stage_data_row, column=9).border = THIN_BORDER
        ws.cell(row=stage_data_row, column=9).alignment = CENTER_ALIGN
        ws.cell(row=stage_data_row, column=10, value="Emission (tCO2e)").font = SUBHEADER_FONT
        ws.cell(row=stage_data_row, column=10).fill = SUBHEADER_FILL
        ws.cell(row=stage_data_row, column=10).border = THIN_BORDER
        ws.cell(row=stage_data_row, column=10).alignment = CENTER_ALIGN

        for mi, (_, mrow) in enumerate(member_summary.iterrows()):
            r = stage_data_row + 1 + mi
            ws.cell(row=r, column=9, value=mrow['Member Type']).font = DATA_FONT
            ws.cell(row=r, column=9).border = THIN_BORDER
            ws.cell(row=r, column=10, value=round(mrow['Total Emission (tCO2e)'], 2)).font = DATA_FONT
            ws.cell(row=r, column=10).border = THIN_BORDER
            ws.cell(row=r, column=10).alignment = CENTER_ALIGN
        member_chart_end = stage_data_row + len(member_summary)

    # ===== CHARTS: 3 side by side =====
    current_row += 1
    chart_anchor_row = current_row
    CHART_HEIGHT = 14  # rows
    CHART_WIDTH_COLS = 8  # ~columns wide per chart

    # --- Chart 1: Material Breakdown Pie ---
    pie = PieChart()
    pie.title = "Material Breakdown"
    pie.style = 10
    if mat_data_start >= mat_total_row:
        # No data rows — skip chart to avoid invalid Reference range
        pie = None
    if pie is not None:
        pie_data = Reference(ws, min_col=2, min_row=mat_data_start - 1,
                             max_row=mat_total_row - 1)
        pie_cats = Reference(ws, min_col=1, min_row=mat_data_start,
                             max_row=mat_total_row - 1)
    pie.add_data(pie_data, titles_from_data=True)
    pie.set_categories(pie_cats)
    pie.dataLabels = DataLabelList()
    if pie is not None:
        pie.dataLabels.showPercent = True
        pie.dataLabels.showVal = False
        pie.dataLabels.showCatName = True
        pie.dataLabels.showSerName = False
        chart_colors = ['4472C4', 'ED7D31', '70AD47']
        for ci, color in enumerate(chart_colors):
            try:
                pt = DataPoint(idx=ci)
                pt.graphicalProperties.solidFill = color
                pie.series[0].data_points.append(pt)
            except (IndexError, AttributeError):
                pass
        pie.width = 14
        pie.height = 10
        ws.add_chart(pie, f"A{chart_anchor_row}")

    # --- Chart 2: Lifecycle Stage Bar ---
    bar1 = BarChart()
    bar1.type = "col"
    bar1.title = "Life Cycle Stages"
    bar1.style = 10
    bar1.y_axis.title = "tCO2e"
    bar1_data = Reference(ws, min_col=7, min_row=stage_data_row,
                          max_row=stage_data_end)
    bar1_cats = Reference(ws, min_col=6, min_row=stage_data_row + 1,
                          max_row=stage_data_end)
    bar1.add_data(bar1_data, titles_from_data=True)
    bar1.set_categories(bar1_cats)
    bar1.shape = 4
    bar1.dataLabels = DataLabelList()
    bar1.dataLabels.showVal = True
    bar1.dataLabels.numFmt = '#,##0.00'
    if bar1.series:
        bar1.series[0].graphicalProperties.solidFill = "2E75B6"
    bar1.width = 14
    bar1.height = 10
    bar1.legend = None
    ws.add_chart(bar1, f"F{chart_anchor_row}")

    # --- Chart 3: Member Type Bar ---
    if not member_summary.empty:
        bar2 = BarChart()
        bar2.type = "col"
        bar2.title = "Emissions by Member Type"
        bar2.style = 10
        bar2.y_axis.title = "tCO2e"
        bar2_data = Reference(ws, min_col=10, min_row=stage_data_row,
                              max_row=member_chart_end)
        bar2_cats = Reference(ws, min_col=9, min_row=stage_data_row + 1,
                              max_row=member_chart_end)
        bar2.add_data(bar2_data, titles_from_data=True)
        bar2.set_categories(bar2_cats)
        bar2.shape = 4
        bar2.dataLabels = DataLabelList()
        bar2.dataLabels.showVal = True
        bar2.dataLabels.numFmt = '#,##0.00'
        # Color bars per member type
        member_chart_colors = ['4472C4', 'FFC000', '70AD47', 'ED7D31', '9B59B6', 'E74C3C', '95A5A6']
        if bar2.series:
            for ci in range(len(member_summary)):
                try:
                    pt = DataPoint(idx=ci)
                    pt.graphicalProperties.solidFill = member_chart_colors[ci % len(member_chart_colors)]
                    bar2.series[0].data_points.append(pt)
                except (IndexError, AttributeError):
                    pass
        bar2.width = 14
        bar2.height = 10
        bar2.legend = None
        ws.add_chart(bar2, f"K{chart_anchor_row}")

    current_row = chart_anchor_row + CHART_HEIGHT + 2

    # ===== SECTION 2: STRUCTURAL MEMBER BREAKDOWN =====

    if not member_summary.empty:
        num_member_cols = len(member_summary.columns)
        last_member_col = get_column_letter(num_member_cols)

        ws.merge_cells(f'A{current_row}:{last_member_col}{current_row}')
        ws[f'A{current_row}'] = "STRUCTURAL MEMBER BREAKDOWN"
        ws[f'A{current_row}'].font = SECTION_FONT
        ws[f'A{current_row}'].fill = SECTION_FILL
        _apply_border_range(ws, current_row, 1, current_row, num_member_cols)
        current_row += 1

        member_headers = list(member_summary.columns)
        for col, header in enumerate(member_headers, 1):
            cell = ws.cell(row=current_row, column=col, value=header)
            cell.font = SUBHEADER_FONT
            cell.fill = SUBHEADER_FILL
            cell.alignment = WRAP_ALIGN
            cell.border = THIN_BORDER
        current_row += 1

        grand_total_mass = 0
        grand_a1_a3 = 0
        grand_a4 = 0
        grand_a5 = 0

        for idx, mrow in member_summary.iterrows():
            member_type = mrow['Member Type']
            row_fill = MEMBER_COLORS.get(member_type, MEMBER_COLORS.get('Other'))
            alt_fill = ACCENT_FILL if idx % 2 == 0 else None

            values = [
                member_type,
                round(mrow['Total Mass (kg)'], 2),
                round(mrow['A1-A3 (tCO2e)'], 2),
                round(mrow['A4 (tCO2e)'], 2),
                round(mrow['A5 (tCO2e)'], 2),
                round(mrow['Total Emission (tCO2e)'], 2),
                f"{mrow['% of Total']:.1f}%",
                round(mrow['Per m² (kgCO2e/m²)'], 2),
            ]

            grand_total_mass += mrow['Total Mass (kg)']
            grand_a1_a3 += mrow['A1-A3 (tCO2e)']
            grand_a4 += mrow['A4 (tCO2e)']
            grand_a5 += mrow['A5 (tCO2e)']

            for col, val in enumerate(values, 1):
                cell = ws.cell(row=current_row, column=col, value=val)
                cell.font = DATA_FONT
                cell.border = THIN_BORDER
                if col == 1:
                    cell.alignment = LEFT_ALIGN
                    if row_fill:
                        cell.fill = row_fill
                        cell.font = Font(bold=True, size=10)
                else:
                    cell.alignment = CENTER_ALIGN
                    if alt_fill:
                        cell.fill = alt_fill
            current_row += 1

        # Total row
        total_values = [
            "TOTAL",
            round(grand_total_mass, 2),
            round(grand_a1_a3, 2),
            round(grand_a4, 2),
            round(grand_a5, 2),
            round(total_emission, 2),
            "100.0%",
            round(metrics['total_emission_per_sqm'], 2),
        ]
        for col, val in enumerate(total_values, 1):
            cell = ws.cell(row=current_row, column=col, value=val)
            cell.font = TOTAL_FONT
            cell.fill = TOTAL_FILL
            cell.alignment = CENTER_ALIGN
            cell.border = THIN_BORDER
            if col == 1:
                cell.alignment = LEFT_ALIGN
        current_row += 3

        # ===== SECTION 3: DETAILED BREAKDOWN PER MEMBER TYPE =====
        ws.merge_cells(f'A{current_row}:D{current_row}')
        ws[f'A{current_row}'] = "DETAILED BREAKDOWN PER MEMBER TYPE"
        ws[f'A{current_row}'].font = SECTION_FONT
        ws[f'A{current_row}'].fill = SECTION_FILL
        _apply_border_range(ws, current_row, 1, current_row, 4)
        current_row += 2

        df = detailed_df.copy()
        df['_member_type'] = df.apply(_classify_member_type, axis=1)

        for c in ['Mass(kg)', 'Total Emission(tCO2e)']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)

        ordered_types = member_summary['Member Type'].tolist()
        for mtype in ordered_types:
            sub_df = df[df['_member_type'] == mtype]
            if sub_df.empty:
                continue

            # Member type sub-header
            ws.merge_cells(f'A{current_row}:D{current_row}')
            cell = ws.cell(row=current_row, column=1, value=mtype)
            type_fill = MEMBER_COLORS.get(mtype, MEMBER_COLORS.get('Other'))
            cell.font = Font(bold=True, size=10, color="1F4E79")
            if type_fill:
                for c in range(1, 5):
                    ws.cell(row=current_row, column=c).fill = type_fill
            _apply_border_range(ws, current_row, 1, current_row, 4)
            current_row += 1

            # Sub-headers
            sub_headers = ["Material", "Description", "Mass (kg)", "Emission (tCO2e)"]
            for col, h in enumerate(sub_headers, 1):
                cell = ws.cell(row=current_row, column=col, value=h)
                cell.font = SUBHEADER_FONT
                cell.fill = SUBHEADER_FILL
                cell.alignment = CENTER_ALIGN
                cell.border = THIN_BORDER
            current_row += 1

            for ri, (_, erow) in enumerate(sub_df.iterrows()):
                vals = [
                    erow.get('Material', ''),
                    erow.get('Description', ''),
                    round(float(erow.get('Mass(kg)', 0)), 2),
                    round(float(erow.get('Total Emission(tCO2e)', 0)), 2),
                ]
                alt = ACCENT_FILL if ri % 2 == 0 else None
                for col, val in enumerate(vals, 1):
                    cell = ws.cell(row=current_row, column=col, value=val)
                    cell.font = DATA_FONT
                    cell.border = THIN_BORDER
                    if isinstance(val, (int, float)):
                        cell.alignment = CENTER_ALIGN
                        if isinstance(val, float):
                            cell.number_format = '#,##0.00'
                    else:
                        cell.alignment = LEFT_ALIGN
                    if alt:
                        cell.fill = alt
                current_row += 1

            # Subtotal
            sub_mass = sub_df['Mass(kg)'].sum()
            sub_emission = sub_df['Total Emission(tCO2e)'].sum()
            subtotal_vals = ["", "Subtotal", round(sub_mass, 2), round(sub_emission, 2)]
            for col, val in enumerate(subtotal_vals, 1):
                cell = ws.cell(row=current_row, column=col, value=val)
                cell.font = Font(bold=True, size=10)
                cell.border = THIN_BORDER
                if isinstance(val, (int, float)):
                    cell.alignment = CENTER_ALIGN
                    cell.number_format = '#,##0.00'
            current_row += 2

    # Set column widths
    col_widths = [22, 24, 18, 18, 16, 16, 20, 14]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _calculate_life_cycle_stages(detailed_df, total_emission, stage_emissions=None,
                                 a5a_factor=None):
    """Calculate emissions by life cycle stage. `a5a_factor` is quoted in the A5
    row label so the stage table names the site-activity rate actually used."""
    stages = []
    _a5a = A5A_DEFAULT if a5a_factor is None else float(a5a_factor)
    _a5a_label = (f"Construction (incl. A5a: {_a5a:g} kgCO2e/m² per SEAI)"
                  if abs(_a5a - A5A_DEFAULT) <= 1e-6 else
                  f"Construction (incl. A5a: {_a5a:g} kgCO2e/m², user override)")

    if detailed_df is None or detailed_df.empty:
        return [
            ("A1-A3", "Product Stage (Raw Material to Manufacturing)", 0, 0),
            ("A4", "Transport to Site", 0, 0),
            ("A5", _a5a_label, 0, 0),
        ]

    # Use pre-calculated stage emissions if available (includes A5a)
    if stage_emissions:
        a1_a3_emission = stage_emissions.get('A1-A3', 0)
        a4_emission = stage_emissions.get('A4', 0)
        a5_emission = stage_emissions.get('A5', 0)  # This now includes A5a
    else:
        # Fallback to dataframe calculation
        a1_a3_emission = 0
        a4_emission = 0
        a5_emission = 0

        if 'A1-A3 Emission(tonCO2e/kg)' in detailed_df.columns:
            a1_a3_emission = detailed_df['A1-A3 Emission(tonCO2e/kg)'].sum()
        elif 'Total Emission(tCO2e)' in detailed_df.columns:
            a1_a3_emission = detailed_df['Total Emission(tCO2e)'].sum() * 0.7

        if 'A4 Emission(tonCO2e/kg)' in detailed_df.columns:
            a4_emission = detailed_df['A4 Emission(tonCO2e/kg)'].sum()
        elif total_emission > 0:
            a4_emission = total_emission * 0.2

        if 'A5 Emission(tonCO2e/kg)' in detailed_df.columns:
            a5_emission = detailed_df['A5 Emission(tonCO2e/kg)'].sum()
        elif total_emission > 0:
            a5_emission = total_emission * 0.1

    # Calculate percentages
    a1_a3_pct = (a1_a3_emission / total_emission * 100) if total_emission > 0 else 0
    a4_pct = (a4_emission / total_emission * 100) if total_emission > 0 else 0
    a5_pct = (a5_emission / total_emission * 100) if total_emission > 0 else 0

    stages = [
        ("A1-A3", "Product Stage (Raw Material to Manufacturing)", a1_a3_emission, a1_a3_pct),
        ("A4", "Transport to Site", a4_emission, a4_pct),
        ("A5", _a5a_label, a5_emission, a5_pct),
    ]

    return stages


def _apply_border_range(ws, row_start, col_start, row_end, col_end):
    """Apply thin borders to a range of cells."""
    for row in range(row_start, row_end + 1):
        for col in range(col_start, col_end + 1):
            ws.cell(row=row, column=col).border = THIN_BORDER
