"""
Data Check - the factors this run will actually use.
====================================================
Read-only. It resolves every choice made in steps 1-4 into the concrete numbers
the engine will apply, so they can be reviewed BEFORE anything is calculated.

Why this is a service and not a template detail: the legacy wizard hardcoded
these tables in HTML - 41 concrete values and 3 steel values copied by hand from
the catalogue CSV (templates/web_ui.html:1696, :896-898). Two of the concrete
values had already drifted:

    8/10 MPa @ 25% GGBS   HTML 0.103   CSV 0.076   (35% too high)
    8/10 MPa @ 50% GGBS   HTML 0.092   CSV 0.055   (67% too high)

so the screen headed "the emission factors your results will be based on" was
showing numbers the calculation did not use. Everything here is derived from
data/A1_A5_Emission_Catalogue.csv at request time, so it cannot drift again.

The hardcoded steel table had a second problem: it listed the DEFAULT factors
rather than the ones the user assigned in Materials. This resolves the actual
assignments.
"""

from __future__ import annotations

from app import core_bridge  # noqa: F401  - puts src/core on sys.path

from catalogue import (DEFAULT_PT_EID, DEFAULT_STEEL_EID,
                       DEFAULT_STEEL_SECTION_EID, EmissionCatalogue,
                       PT_TYPES, REBAR_TYPES, STEEL_SECTION_TYPES)
from stages import get_stage_info

# The engine prices concrete by volume x 2400 kg/m3, so the per-m3 figure shown
# here must use the same density or the review would not match the run.
CONCRETE_DENSITY = 2400.0


def _clean_grade(label: str) -> str:
    """'32/40 MPa (GEN 3)' -> '32/40'. Mirrors the legacy _cleanGrade()."""
    import re
    s = re.sub(r'\s*\(GEN\s*\d+\)', '', str(label or ''), flags=re.I)
    return re.sub(r'\s*MPa\s*', '', s, flags=re.I).strip()


def build_data_check(run, rows: list[dict], materials: dict,
                     transport: dict) -> dict:
    """Resolve steps 1-4 into the factor tables shown in Step 5.

    Args:
        run:        the Run row (project details, for stage and area)
        rows:       material rows (store.material_rows)
        materials:  saved Step-3 settings
        transport:  saved Step-4 settings
    """
    cat = EmissionCatalogue()
    defaults = materials.get('defaults') or {}
    row_overrides = materials.get('rows') or {}
    epd = materials.get('epd_overrides') or {}

    default_grade = _clean_grade(str(defaults.get('concrete_grade', '32/40')))
    default_ggbs = int(defaults.get('ggbs_pct', 0) or 0)

    concrete = _concrete_factors(cat, rows, row_overrides, epd,
                                 default_grade, default_ggbs)
    steel = _steel_factors(cat, rows, row_overrides, defaults, epd)

    project = run.project or {}
    stage = get_stage_info(str(project.get('stage') or ''))

    return {
        'stage': {
            'label': stage['label'],
            'badge': stage['badge'],
            'code': stage['code'],
            'description': stage['description'],
            'expectations': stage['expectations'],
            'uncertainty_pct': stage['uncertainty_pct'],
            'epd_expected': stage['epd_expected'],
        },
        'concrete': concrete,
        'steel': steel,
        'epd_overrides': [
            {'key': k, 'a1_a3': v.get('a1_a3'), 'source': v.get('source') or '',
             'mat_type': v.get('mat_type') or ''}
            for k, v in epd.items()
        ],
        'a5a': _a5a(project, transport),
        'transport': _transport_summary(transport),
        'waste': _waste_summary(transport),
        'coverage': _epd_coverage(concrete, steel),
        'catalogue_version': cat.version,
    }


def _concrete_factors(cat, rows, row_overrides, epd, default_grade,
                      default_ggbs) -> list[dict]:
    """One entry per distinct concrete factor actually in use.

    A row that was assigned a named catalogue factor is reported as THAT factor,
    not as its underlying grade - which is what the engine applies.
    """
    out: list[dict] = []
    seen: set[str] = set()

    for r in rows:
        if r['is_steel']:
            continue
        ov = row_overrides.get(r['key'], {})
        assigned = ov.get('concFactorEid') or ov.get('fullEid')

        if assigned:
            if assigned in seen:
                continue
            seen.add(assigned)
            f = cat.get_emission_factor(assigned) or {}
            kg = float(f.get('a1_a3', 0) or 0)
            override = epd.get(f'Concrete|||{assigned}', {}).get('a1_a3')
            out.append(_concrete_entry(
                label=str(f.get('mat_name') or assigned), ggbs=None,
                kg=kg, e_id=assigned,
                data_type=str(f.get('data_type') or 'Generic'),
                override_m3=override, saving_pct=None))
            continue

        grade = _clean_grade(str(ov.get('grade') or r.get('grade') or default_grade))
        ggbs = int(ov.get('ggbs') if ov.get('ggbs') is not None
                   else (r.get('ggbs') if r.get('ggbs') is not None
                         else default_ggbs))
        key = f'{grade}|{ggbs}'
        if key in seen:
            continue
        seen.add(key)

        eid = cat.get_concrete_eid(grade, ggbs)
        f = cat.get_emission_factor(eid) if eid else None
        if f is None:
            continue
        kg = float(f.get('a1_a3', 0) or 0)

        # Saving is quoted against the SAME grade at 0% GGBS - comparing across
        # grades would credit GGBS for a strength change it did not make.
        saving = None
        if ggbs > 0:
            base_eid = cat.get_concrete_eid(grade, 0)
            base = cat.get_emission_factor(base_eid) if base_eid else None
            base_kg = float((base or {}).get('a1_a3', 0) or 0)
            if base_kg > 0:
                saving = round((1 - kg / base_kg) * 100)

        out.append(_concrete_entry(
            label=f'C{grade}', ggbs=ggbs, kg=kg, e_id=eid or '',
            data_type=str(f.get('data_type') or 'Generic'),
            override_m3=epd.get(f'Concrete|||{grade}|||{ggbs}', {}).get('a1_a3'),
            saving_pct=saving))

    return out


def _concrete_entry(*, label, ggbs, kg, e_id, data_type, override_m3,
                    saving_pct) -> dict:
    """One concrete row. Concrete EPDs are quoted per m3, so an override replaces
    the per-m3 figure and the per-kg cell reads '(EPD)' - matching the legacy."""
    is_epd = override_m3 is not None
    return {
        'label': label,
        'ggbs': ggbs,
        'e_id': e_id,
        'a1_a3_kg': None if is_epd else round(kg, 4),
        'a1_a3_m3': (round(float(override_m3), 1) if is_epd
                     else round(kg * CONCRETE_DENSITY, 1)),
        'saving_pct': saving_pct,
        'source': 'EPD (supplier)' if is_epd else data_type,
        'is_epd': is_epd,
    }


def _steel_factors(cat, rows, row_overrides, defaults, epd) -> list[dict]:
    """One entry per distinct steel / PT factor actually in use.

    Resolves what was ASSIGNED, not the defaults. Rate-based rebar and PT on
    concrete members have no steel row of their own, so their factors are added
    from the Step-3 type dropdowns.
    """
    out: list[dict] = []
    seen: set[str] = set()

    def add(eid: str, bucket: str) -> None:
        if not eid or eid in seen:
            return
        seen.add(eid)
        f = cat.get_emission_factor(eid) or {}
        override = epd.get(f'{bucket}|||{eid}', {}).get('a1_a3')
        kg = float(f.get('a1_a3', 0) or 0)
        out.append({
            'label': str(f.get('mat_name') or eid),
            'bucket': bucket,
            'e_id': eid,
            'a1_a3_kg': round(float(override) if override is not None else kg, 4),
            'density': round(float(f.get('density', 0) or 0)),
            'source': ('EPD (supplier)' if override is not None
                       else str(f.get('data_type') or 'Generic')),
            'is_epd': override is not None,
        })

    for r in rows:
        if not r['is_steel']:
            continue
        ov = row_overrides.get(r['key'], {})
        add(ov.get('steelEid') or r.get('e_id') or DEFAULT_STEEL_EID,
            r.get('factor_type') or 'Rebar')

    # Concrete members carry rate-based rebar and PT with no row of their own.
    if any(not r['is_steel'] for r in rows):
        add(REBAR_TYPES.get(str(defaults.get('rebar_type', '')), DEFAULT_STEEL_EID),
            'Rebar')
        add(STEEL_SECTION_TYPES.get(str(defaults.get('section_type', '')),
                                    DEFAULT_STEEL_SECTION_EID), 'Steel Section')
        add(PT_TYPES.get(str(defaults.get('pt_type', '')), DEFAULT_PT_EID),
            'Post Tensioning')

    return out


def _a5a(project: dict, transport: dict) -> dict:
    """Site-activity factor and what it comes to for this floor area."""
    from calculations import A5A_EMISSION_FACTOR_KGCO2E_PER_SQM as A5A_DEFAULT

    override = transport.get('a5a_factor')
    factor = A5A_DEFAULT if override is None else float(override)
    try:
        area = float(project.get('area') or 0)
    except (TypeError, ValueError):
        area = 0.0
    return {
        'factor': factor,
        'default': A5A_DEFAULT,
        'is_override': override is not None and factor != A5A_DEFAULT,
        'area': area,
        'total_tonnes': round(factor * area / 1000.0, 2) if area > 0 else None,
    }


def _transport_summary(transport: dict) -> list[dict]:
    """Distances as they will be applied, flagging anything moved off default."""
    from catalogue import SEAI_TRANSPORT_TABLE

    saved = transport.get('distances') or {}
    out = []
    for key, meta in SEAI_TRANSPORT_TABLE.items():
        legs = saved.get(key) or {}
        road = float(legs.get('road', meta['road']))
        sea = float(legs.get('sea', meta['sea']))
        out.append({
            'key': key, 'label': meta['label'], 'scenario': meta['scenario'],
            'road': road, 'sea': sea,
            'is_override': road != meta['road'] or sea != meta['sea'],
        })
    return out


def _waste_summary(transport: dict) -> list[dict]:
    """Waste as percent and as the multiplier the engine receives."""
    from catalogue import DEFAULT_WASTE_FACTORS

    saved = transport.get('waste_pct') or {}
    out = []
    for material in ('Concrete', 'Rebar', 'Steel Section', 'Post Tensioning'):
        default_pct = round((DEFAULT_WASTE_FACTORS.get(material, 1.0) - 1) * 100, 3)
        pct = float(saved.get(material, default_pct))
        out.append({
            'material': material, 'pct': pct,
            'multiplier': round(1 + pct / 100, 4),
            'is_override': abs(pct - default_pct) > 1e-9,
        })
    return out


def _epd_coverage(concrete: list[dict], steel: list[dict]) -> dict:
    """Share of the factors in use that come from a supplier EPD.

    Tender stage expects supplier-specific data, so this is what the stage
    warning is judged against.
    """
    all_factors = concrete + steel
    total = len(all_factors)
    with_epd = sum(1 for f in all_factors if f.get('is_epd'))
    return {
        'total': total,
        'with_epd': with_epd,
        'pct': round(with_epd / total * 100) if total else 0,
    }
