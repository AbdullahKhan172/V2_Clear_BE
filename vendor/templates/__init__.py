# The single served dashboard is html_template_2 (robust: base64 logo, ECharts,
# 3D viewer). The retired html_template_1 has been removed; its one still-used
# helper (_prepare_geometry_json_mesh) now lives inside html_template_2.
from .html_template_2 import render as render_template_2

TEMPLATES = {
    # Both keys map to template 2 — 'dashboard' is kept for backward compatibility
    # with any client that still sends the old value.
    'dashboard': render_template_2,
    'advanced': render_template_2,
}


def get_template(template_name):
    """Get a standard template by name (always the consolidated dashboard)."""
    return TEMPLATES.get(template_name, render_template_2)
