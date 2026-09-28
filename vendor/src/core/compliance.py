"""
Compliance checking module for LETI 2030 and SCORS standards.
Provides functions to check project emissions against industry benchmarks.
"""

# LETI 2030 Targets (whole building, A1-A5, kgCO2e/m2)
LETI_2030_TARGETS = {
    'residential': 300,
    'commercial': 350,
    'office': 350,
    'school': 350,
    'education': 350,
    'healthcare': 400,
    'hospital': 400,
    'industrial': 450,
    'warehouse': 450,
    'retail': 400,
    'mixed_use': 375
}

# SCORS Structural Carbon Rating Scheme bands (kgCO2e/m2)
# Based on IStructE Climate Emergency Task Group proposal
# Upper bound is inclusive: A++ means intensity <= 50, etc.
SCORS_BANDS = {
    'A++': (0, 50),
    'A+': (50, 100),
    'A': (100, 150),
    'B': (150, 200),
    'C': (200, 250),
    'D': (250, 300),
    'E': (300, 350),
    'F': (350, 400),
    'G': (400, float('inf'))
}

# Efficiency bands aligned with the IStructE SCORS structural carbon rating.
# Descriptions describe structural (A1-A5) carbon intensity only — they are NOT
# whole-building LETI/RIBA pass/fail statements.
EFFICIENCY_BANDS = {
    'A++': (0, 50, 'Outstanding structural carbon'),
    'A+': (50, 100, 'Excellent structural carbon'),
    'A': (100, 150, 'Very good structural carbon'),
    'B': (150, 200, 'Good - SCORS target band'),
    'C': (200, 250, 'Moderate - around industry average'),
    'D': (250, 300, 'Improvement needed'),
    'E': (300, 350, 'Below average'),
    'F': (350, 400, 'Poor performance'),
    'G': (400, float('inf'), 'Very poor - urgent action required')
}

# Rating colors for visualization
RATING_COLORS = {
    'A++': {'start': '#007a39', 'end': '#00a651'},
    'A+': {'start': '#2da84f', 'end': '#5cb85c'},
    'A': {'start': '#9ecf2f', 'end': '#c5e24a'},
    'B': {'start': '#e1da29', 'end': '#f7dc6f'},
    'C': {'start': '#f6d25b', 'end': '#f8c471'},
    'D': {'start': '#f08a2f', 'end': '#eb984e'},
    'E': {'start': '#e2482a', 'end': '#ec7063'},
    'F': {'start': '#c11f28', 'end': '#e74c3c'},
    'G': {'start': '#6f0d0f', 'end': '#922b21'}
}


def get_efficiency_rating(per_sqm):
    """
    Get SCORS efficiency rating based on structural emissions per square meter.
    Aligned with IStructE SCORS bands.

    Args:
        per_sqm: Emissions in kgCO2e/m2

    Returns:
        str: Rating from A++ to G
    """
    if per_sqm <= 50:
        return "A++"
    elif per_sqm <= 100:
        return "A+"
    elif per_sqm <= 150:
        return "A"
    elif per_sqm <= 200:
        return "B"
    elif per_sqm <= 250:
        return "C"
    elif per_sqm <= 300:
        return "D"
    elif per_sqm <= 350:
        return "E"
    elif per_sqm <= 400:
        return "F"
    return "G"


def get_rating_description(rating):
    """Get the description for a rating."""
    for r, (low, high, desc) in EFFICIENCY_BANDS.items():
        if r == rating:
            return desc
    return "Unknown"


def get_rating_row_index(rating):
    """Get the row index for positioning the rating indicator."""
    rating_map = {
        "A++": 0, "A+": 1, "A": 2, "B": 3,
        "C": 4, "D": 5, "E": 6, "F": 7, "G": 8
    }
    return rating_map.get(rating, 8)


def check_leti_compliance(per_sqm, building_type='commercial'):
    """
    Check compliance against LETI 2030 targets.

    Args:
        per_sqm: Emissions in kgCO2e/m2
        building_type: Type of building (residential, commercial, etc.)

    Returns:
        dict: Compliance status with target, actual, passed status, and margin
    """
    target = LETI_2030_TARGETS.get(building_type.lower(), 350)
    margin = target - per_sqm

    return {
        'standard': 'LETI 2030',
        'target': target,
        'actual': round(per_sqm, 1),
        'passed': per_sqm <= target,
        'margin': round(margin, 1),
        'building_type': building_type,
        'percentage_of_target': round((per_sqm / target) * 100, 1)
    }


def get_scors_rating(per_sqm):
    """
    Get SCORS (Structural Carbon Rating Scheme) rating.

    Args:
        per_sqm: Emissions in kgCO2e/m2

    Returns:
        dict: Rating info including rating, band, and distance to next band
    """
    for rating, (low, high) in SCORS_BANDS.items():
        if low < per_sqm <= high or (rating == 'A++' and per_sqm <= high):
            # Calculate distance to next better rating
            if rating == 'A++':
                to_next = 0
            else:
                to_next = round(per_sqm - low, 1)

            return {
                'rating': rating,
                'band_low': low,
                'band_high': high if high != float('inf') else '>500',
                'actual': round(per_sqm, 1),
                'to_next_band': to_next
            }

    # Default to G if nothing matches
    return {
        'rating': 'G',
        'band_low': 500,
        'band_high': '>500',
        'actual': round(per_sqm, 1),
        'to_next_band': round(per_sqm - 500, 1)
    }


