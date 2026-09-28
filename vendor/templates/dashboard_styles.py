"""
Dashboard HTML Head + CSS
=========================
Static markup extracted verbatim from html_template_2.py — pure string
literals, zero parameters, zero dynamic data. Kept separate so the ~1000
lines of CSS don't crowd out the functions that actually build page content
from `data`.
"""


def _get_html_head():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>C.L.E.A.R. Dashboard - Carbon Lifecycle Evaluation and Reporting</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
""" + _get_styles() + "</head><body>"


def _get_styles():
    return """<style>
:root {
    --primary: #1e3a5f;
    --primary-light: #2d5a87;
    --accent: #16a34a;
    --accent-light: #22c55e;
    --accent-bg: #f0fdf4;
    --blue: #3498db;
    --blue-light: #5dade2;
    --warning: #e67e22;
    --danger: #c0392b;
    --dark: #0f172a;
    --text: #1e293b;
    --text-light: #64748b;
    --text-muted: #94a3b8;
    --border: #e2e8f0;
    --border-dark: #cbd5e1;
    --card-bg: #ffffff;
    --bg: #f8fafb;
    --teal: #0f4c5c;
    --teal-light: #1a7a8a;
    --shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
    --shadow-md: 0 4px 6px rgba(0,0,0,0.05), 0 2px 4px rgba(0,0,0,0.03);
    --radius: 8px;
    --radius-sm: 6px;
}

* { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; }

body { background-color: var(--bg); color: var(--text); line-height: 1.5; font-size: 14px; }

.dashboard-container { width: 100%; max-width: 100%; margin: 0 auto; padding: 10px 12px; }

/* Project Info Bar - slim single line */
.project-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-left: 4px solid var(--accent);
    border-radius: var(--radius-sm);
    padding: 8px 16px;
    margin-bottom: 10px;
    font-size: 0.82rem;
    color: var(--text-light);
    box-shadow: var(--shadow);
}
.project-bar .project-name { font-weight: 700; color: var(--text); font-size: 1rem; }
.project-bar .divider { width: 1px; height: 16px; background: var(--border-dark); margin: 0 14px; }
.project-bar span { display: flex; align-items: center; gap: 5px; white-space: nowrap; font-size: 0.85rem; }
.project-bar i { font-size: 0.72rem; color: var(--accent); }

/* Tab Navigation */
.tab-navigation {
    display: flex;
    background: var(--card-bg);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    margin-bottom: 10px;
    border: 1px solid var(--border);
    overflow: hidden;
}
.tab-button {
    flex: 1;
    padding: 10px 16px;
    background: transparent;
    border: none;
    border-bottom: 3px solid transparent;
    cursor: pointer;
    font-weight: 600;
    font-size: 0.88rem;
    color: var(--text-light);
    transition: all 0.2s;
}
.tab-button:hover { color: var(--accent); background: var(--accent-bg); }
.tab-button.active { color: var(--accent); border-bottom-color: var(--accent); background: var(--accent-bg); }
.tab-button i { margin-right: 6px; }
.tab-content { display: none; }
.tab-content.active { display: block; }

/* Grid */
.grid { display: grid; gap: 12px; margin-bottom: 12px; }
.grid-4 { grid-template-columns: repeat(4, 1fr); }
.grid-3 { grid-template-columns: repeat(3, 1fr); }
.grid-2 { grid-template-columns: 1fr 1fr; }
.grid-2-1 { grid-template-columns: 2fr 1fr; }
.grid-1-2 { grid-template-columns: 1fr 2fr; }
.grid-12 { grid-template-columns: repeat(12, 1fr); }
.col-12 { grid-column: span 12; }
.col-8 { grid-column: span 8; }
.col-6 { grid-column: span 6; }
.col-4 { grid-column: span 4; }
.col-3 { grid-column: span 3; }

/* Cards */
.card {
    background: var(--card-bg);
    border-radius: var(--radius);
    padding: 14px;
    box-shadow: var(--shadow);
    border: 1px solid var(--border);
}
.card-title {
    font-size: 0.88rem;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 10px;
    padding-bottom: 8px;
    border-bottom: 2px solid var(--accent);
    display: flex;
    align-items: center;
    gap: 6px;
}
.card-title i { color: var(--accent); font-size: 0.85rem; }

/* KPI Metric Cards */
.kpi-card {
    background: var(--card-bg);
    border-radius: var(--radius);
    padding: 12px 14px;
    border: 1px solid var(--border);
    box-shadow: var(--shadow);
    position: relative;
    overflow: hidden;
}
.kpi-card::before {
    content: '';
    position: absolute;
    left: 0; top: 0; bottom: 0;
    width: 3px;
}
.kpi-card.green::before { background: var(--accent); }
.kpi-card.orange::before { background: var(--warning); }
.kpi-card.blue::before { background: var(--blue); }
.kpi-card.red::before { background: var(--danger); }
.kpi-label { font-size: 0.72rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 3px; }
.kpi-value { font-size: 1.8rem; font-weight: 700; color: var(--dark); line-height: 1.1; }
.kpi-unit { font-size: 0.75rem; color: var(--text-light); margin-top: 2px; }

/* Rating Badge */
.rating-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 4px 14px;
    border-radius: var(--radius-sm);
    color: white;
    font-weight: 700;
    font-size: 1.3rem;
    min-width: 50px;
}
.rating-badge.a-plus-plus, .rating-badge.a-plus, .rating-badge.a { background: linear-gradient(135deg, #16a34a, #22c55e); }
.rating-badge.b { background: linear-gradient(135deg, #eab308, #facc15); color: #1a1a1a; }
.rating-badge.c { background: linear-gradient(135deg, #ea580c, #f97316); }
.rating-badge.d { background: linear-gradient(135deg, #dc2626, #ef4444); }
.rating-badge.e, .rating-badge.f, .rating-badge.g { background: linear-gradient(135deg, #991b1b, #dc2626); }

/* SCORS Scale (CSS class names kept as .leti-* for backward compatibility) */
.leti-scale { width: 100%; }
.leti-row {
    display: flex;
    align-items: center;
    height: 26px;
    margin-bottom: 2px;
    font-size: 0.76rem;
}
.leti-label { width: 52px; flex-shrink: 0; text-align: right; padding-right: 6px; font-weight: 500; color: var(--text-light); font-size: 0.7rem; }
.leti-bar {
    height: 22px;
    display: flex;
    align-items: center;
    padding: 0 8px;
    color: white;
    font-weight: 600;
    font-size: 0.76rem;
    clip-path: polygon(0 0, calc(100% - 12px) 0, 100% 50%, calc(100% - 12px) 100%, 0 100%);
}
.leti-bar .desc { font-size: 0.65rem; font-weight: 400; opacity: 0.85; margin-left: 4px; }
.leti-Aplusplus { background: linear-gradient(90deg, #007a39, #00a651); width: 22%; min-width: 34px; }
.leti-Aplus     { background: linear-gradient(90deg, #2da84f, #5cb85c); width: 33%; min-width: 44px; }
.leti-A         { background: linear-gradient(90deg, #8bc34a, #9ecf2f); width: 44%; min-width: 54px; color: #1a1a1a; }
.leti-B         { background: linear-gradient(90deg, #cddc39, #e1da29); width: 55%; min-width: 64px; color: #1a1a1a; }
.leti-C         { background: linear-gradient(90deg, #ffc107, #f6d25b); width: 64%; min-width: 74px; color: #1a1a1a; }
.leti-D         { background: linear-gradient(90deg, #ff9800, #f08a2f); width: 73%; min-width: 84px; }
.leti-E         { background: linear-gradient(90deg, #ff5722, #e2482a); width: 82%; min-width: 94px; }
.leti-F         { background: linear-gradient(90deg, #f44336, #c11f28); width: 91%; min-width: 104px; }
.leti-G         { background: linear-gradient(90deg, #d32f2f, #6f0d0f); width: 100%; min-width: 114px; }
.leti-marker {
    display: inline-flex;
    align-items: center;
    margin-left: 6px;
    gap: 3px;
}
.leti-marker-arrow { width: 0; height: 0; border-top: 5px solid transparent; border-bottom: 5px solid transparent; border-right: 8px solid var(--accent); }
.leti-marker-label { background: var(--accent); color: white; padding: 2px 8px; border-radius: 3px; font-size: 0.65rem; font-weight: 600; white-space: nowrap; }

/* Carbon Budget Progress */
.budget-bar {
    height: 8px;
    background: var(--border);
    border-radius: 4px;
    overflow: hidden;
    margin: 6px 0;
}
.budget-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.5s;
}

/* Key Findings */
.findings-list { list-style: none; padding: 0; }
.findings-list li {
    padding: 5px 0;
    font-size: 0.86rem;
    color: var(--text);
    display: flex;
    align-items: center;
    gap: 8px;
    border-bottom: 1px solid var(--border);
}
.findings-list li:last-child { border-bottom: none; }
.findings-list .bullet { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.findings-list strong { color: var(--dark); }

/* Charts */
.chart-container { position: relative; height: 250px; }
.chart-container-sm { position: relative; height: 200px; }
.chart-container-lg { position: relative; height: 300px; }

/* Tables */
.data-table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
.data-table th {
    background: var(--primary);
    color: white;
    padding: 8px 10px;
    text-align: left;
    font-weight: 500;
    font-size: 0.8rem;
}
.data-table th:first-child { border-radius: var(--radius-sm) 0 0 0; }
.data-table th:last-child { border-radius: 0 var(--radius-sm) 0 0; }
.data-table td { padding: 7px 10px; border-bottom: 1px solid var(--border); }
.data-table tbody tr:nth-child(even) { background: #f8fafc; }
.data-table tbody tr:hover { background: rgba(22, 163, 74, 0.04); }
.data-table tbody tr.total-row { background: var(--accent-bg); font-weight: 600; }

/* Collapsible */
.collapse-toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
    font-size: 0.88rem;
    font-weight: 600;
    color: var(--accent);
    padding: 8px 0;
    user-select: none;
}
.collapse-toggle:hover { color: var(--primary); }
.collapse-toggle i { transition: transform 0.2s; font-size: 0.7rem; }
.collapse-toggle.open i { transform: rotate(90deg); }
.collapse-body { display: none; }
.collapse-body.open { display: block; }

/* 3D Model */
.model-area {
    border-radius: var(--radius);
    overflow: hidden;
    background: linear-gradient(135deg, #e8e8e8, #f5f5f5);
    border: 1px solid var(--border);
    min-height: 80px;
    position: relative;
}
.model-area.expanded { min-height: 450px; }
.no-model-mini {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    padding: 12px 20px;
    color: var(--text-muted);
    font-size: 0.82rem;
}
.no-model-mini i { font-size: 1.2rem; opacity: 0.5; }

/* Category toggle boxes */
.cat-box-2 {
    background: #e8edf2;
    color: #444;
    transition: background 0.15s, color 0.15s;
}
.cat-box-2.active {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary) !important;
}
.cat-box-2:hover { opacity: 0.85; }

/* Filter inline */
.filter-inline {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
    padding: 8px 0;
}
.filter-inline select {
    padding: 5px 8px;
    border: 1px solid var(--border-dark);
    border-radius: 4px;
    font-size: 0.82rem;
    color: var(--text);
    background: white;
    min-width: 120px;
}
.filter-inline select:focus { outline: none; border-color: var(--accent); }
.filter-btn {
    padding: 5px 12px;
    border: none;
    border-radius: 4px;
    font-size: 0.82rem;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
}
.filter-btn.primary { background: var(--accent); color: white; }
.filter-btn.primary:hover { background: #15803d; }
.filter-btn.secondary { background: var(--border); color: var(--text-light); }

/* Methodology bar */
.methodology-bar {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 16px;
    padding: 8px 16px;
    margin-top: 10px;
    font-size: 0.75rem;
    color: var(--text-muted);
    border-top: 1px solid var(--border);
    flex-wrap: wrap;
}
.methodology-bar span { display: flex; align-items: center; gap: 4px; }

/* ==================== */
/* FLOATING SIDEBAR     */
/* ==================== */
.sidebar-actions {
    position: fixed;
    right: 0;
    top: 50%;
    transform: translateY(-50%);
    z-index: 1000;
    display: flex;
    flex-direction: column;
    gap: 4px;
}
.sidebar-btn {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 10px 12px;
    background: var(--primary);
    color: white;
    border: none;
    border-radius: 8px 0 0 8px;
    cursor: pointer;
    font-size: 0.72rem;
    font-weight: 600;
    transition: all 0.3s;
    box-shadow: -2px 2px 8px rgba(0,0,0,0.15);
    white-space: nowrap;
}
.sidebar-btn:hover { padding-right: 20px; background: var(--accent); }
.sidebar-btn i { font-size: 0.85rem; }

/* ==================== */
/* SLIDE-OUT PANELS     */
/* ==================== */
.slide-panel {
    position: fixed;
    top: 0;
    right: -520px;
    width: 500px;
    height: 100vh;
    background: var(--card-bg);
    box-shadow: -4px 0 20px rgba(0,0,0,0.15);
    z-index: 2000;
    transition: right 0.3s ease;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}
.slide-panel.open { right: 0; }
.slide-panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 18px;
    background: var(--primary);
    color: white;
    flex-shrink: 0;
}
.slide-panel-header h3 { font-size: 0.95rem; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.slide-panel-close {
    background: rgba(255,255,255,0.15);
    border: none;
    color: white;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    cursor: pointer;
    font-size: 0.85rem;
    display: flex;
    align-items: center;
    justify-content: center;
}
.slide-panel-close:hover { background: rgba(255,255,255,0.3); }
.slide-panel-body { flex: 1; overflow-y: auto; padding: 16px; }
.panel-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.3);
    z-index: 1500;
    display: none;
}
.panel-overlay.open { display: block; }

/* ==================== */
/* VE METRIC CARDS      */
/* ==================== */
.ve-metric-card {
    text-align: center;
    padding: 12px;
    background: var(--card-bg);
    border-radius: var(--radius);
    border: 1px solid var(--border);
    box-shadow: var(--shadow);
    position: relative;
    overflow: hidden;
}
.ve-metric-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
}
.ve-metric-card.green::before { background: var(--accent); }
.ve-metric-card.blue::before { background: var(--blue); }
.ve-metric-card.orange::before { background: var(--warning); }
.ve-metric-card.purple::before { background: #8b5cf6; }
.ve-metric-label { font-size: 0.72rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.3px; }
.ve-metric-value { font-size: 1.3rem; font-weight: 700; color: var(--dark); margin: 4px 0 2px; }

/* ========================= */
/* DECARBONIZATION MATRIX    */
/* ========================= */
.matrix-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
.matrix-table th {
    background: linear-gradient(135deg, var(--teal), var(--teal-light));
    color: white;
    padding: 8px 10px;
    text-align: left;
    font-weight: 500;
    font-size: 0.75rem;
}
.matrix-table th:first-child { border-radius: var(--radius-sm) 0 0 0; }
.matrix-table th:last-child { border-radius: 0 var(--radius-sm) 0 0; }
.matrix-table td { padding: 7px 10px; border-bottom: 1px solid var(--border); }
.matrix-table tbody tr:nth-child(even) { background: #f0fdfa; }
.matrix-table tbody tr:hover { background: #ccfbf1; }
.matrix-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 0.68rem;
    font-weight: 600;
}
.matrix-badge.recommended { background: #dcfce7; color: #166534; }
.matrix-badge.secondary { background: #fef3c7; color: #92400e; }
.matrix-badge.constrained { background: #fee2e2; color: #991b1b; }
.matrix-badge.supplementary { background: #dbeafe; color: #1e40af; }

/* ==================== */
/* PATHWAY CARDS        */
/* ==================== */
.pathway-card {
    padding: 14px;
    border-radius: var(--radius);
    border: 1px solid var(--border);
    background: var(--card-bg);
    box-shadow: var(--shadow);
}
.pathway-card-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
    font-weight: 600;
    font-size: 0.88rem;
    padding-bottom: 6px;
    border-bottom: 2px solid var(--border);
}
.pathway-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 5px 0;
    font-size: 0.8rem;
    border-bottom: 1px dotted var(--border);
}
.pathway-item:last-child { border-bottom: none; }

/* ======================== */
/* BENCHMARK TABLE          */
/* ======================== */
.benchmark-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
.benchmark-table td, .benchmark-table th { padding: 5px 10px; border-bottom: 1px solid var(--border); }
.benchmark-table th { background: var(--bg); font-weight: 600; font-size: 0.75rem; color: var(--text-light); text-align: left; }
.benchmark-table .current-row { background: #fef3c7; font-weight: 600; }

/* Responsive */
@media (max-width: 1200px) {
    .grid-4 { grid-template-columns: repeat(2, 1fr); }
    .grid-2, .grid-2-1, .grid-1-2 { grid-template-columns: 1fr; }
    .grid-12 { grid-template-columns: repeat(6, 1fr); }
    .col-6, .col-4, .col-3 { grid-column: span 6; }
    .col-8, .col-12 { grid-column: span 6; }
}
@media (max-width: 768px) {
    .dashboard-container { padding: 8px; }
    .grid-4, .grid-3 { grid-template-columns: 1fr; }
    .grid-12 { grid-template-columns: 1fr; }
    .col-12, .col-8, .col-6, .col-4, .col-3 { grid-column: span 1; }
    .project-bar { flex-wrap: wrap; gap: 6px; }
    .project-bar .divider { display: none; }
    .tab-button { font-size: 0.75rem; padding: 8px 10px; }
    .tab-button i { display: none; }
    .kpi-value { font-size: 1.3rem; }
    .filter-inline { flex-direction: column; align-items: stretch; }
    .chart-container { height: 180px; }
    .sidebar-actions { display: none; }
    .slide-panel { width: 100%; right: -100%; }
}
@media (max-width: 480px) {
    .grid-4 { grid-template-columns: repeat(2, 1fr); }
    .kpi-value { font-size: 1.1rem; }
    .kpi-card { padding: 8px 10px; }
}
@media print {
    .tab-navigation { display: none; }
    .tab-content { display: block !important; }
    .collapse-body { display: block !important; }
    .card { box-shadow: none; border: 1px solid #ddd; page-break-inside: avoid; }
    .sidebar-actions, .slide-panel, .panel-overlay { display: none; }
}
</style>"""
