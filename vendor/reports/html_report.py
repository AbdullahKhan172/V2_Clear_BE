import os
from templates import get_template


def generate_html_report(data, output_file, template_name='dashboard'):
    """
    Generate an HTML report using the specified template.

    Args:
        data: ProjectDataModel instance with calculated data
        output_file: Path to save the HTML file
        template_name: Template name ('dashboard' or 'advanced')

    Returns:
        str: Path to the generated file
    """
    output_dir = os.path.dirname(output_file)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)

    template_render = get_template(template_name)
    html_content = template_render(data)

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_content)

    print(f"HTML report generated: {output_file}")
    return output_file
