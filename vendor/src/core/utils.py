import pandas as pd
import base64


def safe_float_convert(value, default=0.0):
    try:
        if pd.isna(value) or value == '' or value is None:
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def encode_file_to_base64(file_path):
    try:
        with open(file_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')
    except (OSError, IOError) as e:
        print(f"[encode_file_to_base64] ERROR: Could not read {file_path}: {e}")
        return ""


def round_floats(obj, decimals=3):
    """Recursively round all floats in nested dicts/lists, replacing NaN/Inf with 0."""
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return 0.0
        return round(obj, decimals)
    # Handle numpy scalar types (numpy.float64, numpy.int64, etc.)
    try:
        import numpy as _np
        if isinstance(obj, _np.floating):
            v = float(obj)
            if math.isnan(v) or math.isinf(v):
                return 0.0
            return round(v, decimals)
        if isinstance(obj, _np.integer):
            return int(obj)
    except ImportError:
        pass
    if isinstance(obj, dict):
        return {k: round_floats(v, decimals) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [round_floats(v, decimals) for v in obj]
    return obj
