"""
Decarbonisation: are the savings honest?
========================================
This tab tells a design team where to spend effort, so an overstated saving is
worse than no advice. Two things can go wrong quietly:

  1. Double counting. 50% and 70% GGBS are alternatives for the SAME concrete,
     and summing both roughly doubles the apparent opportunity. That bug was
     real - it reported 554 t (42%) where the true figure is 388 t (29.5%).

  2. Advice that cannot be taken. Proposing a structural-system swap at Tender
     is noise; a locked lever must never count toward the opportunity.

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_decarb.py
"""

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]
# These harnesses never write a blob, but they DO import `app`, which loads
# webapp/backend/.env - and that file may hold real production credentials.
# Pinning the backend makes reaching a live bucket impossible rather than
# merely unlikely. run_suite.py --s3 is the one way to opt in.
os.environ.setdefault('STORAGE_BACKEND', 'local')

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PROJECT))

from app.services.decarb import (EXCLUSIVE_GROUPS, SCENARIOS,  # noqa: E402
                                 STAGE_LEVERS, build_decarbonisation)

SENSITIVITY = {
    'slab_thickness_30pct': {'delta': 98.22, 'n_rows': 1, 'note': 'RC only'},
    'ggbs_50pct': {'delta': 166.45, 'n_rows': 5},
    'ggbs_70pct': {'delta': 254.77, 'n_rows': 5},
    'rebar_slab_15pct': {'delta': 34.69, 'n_rows': 3},
    'rebar_col_10pct': {'delta': 2.64, 'n_rows': 1},
    'rebar_found_10pct': {'delta': 2.07, 'n_rows': 1},
    'rebar_wall_10pct': {'delta': 2.22, 'n_rows': 1},
}
TOTAL, AREA, PER_SQM = 1314.07, 5200.0, 252.7

# Full precision on purpose: the breakdown carries more decimals than the tab
# should ever print, which is what the rounding check below is guarding.
CATS = [{'key': 'Slab/Floor', 'total_ton': 621.2194},
        {'key': 'Beam', 'total_ton': 268.2812},
        {'key': 'Wall', 'total_ton': 182.4391},
        {'key': 'Column', 'total_ton': 69.6035}]


def build(stage, sensitivity=SENSITIVITY):
    return build_decarbonisation(
        stage_label=stage, sensitivity=sensitivity, total_ton=TOTAL,
        per_sqm=PER_SQM, area=AREA, structural_system='Steel + Hollowcore',
        category_breakdown=CATS)


def main() -> int:
    problems: list[str] = []

    # ── 1. Mutually exclusive scenarios are never both counted ──────────
    d = build('Concept / Schematic Design')
    for group in EXCLUSIVE_GROUPS:
        counted = [a['key'] for a in d['actions'] if a['key'] in group]
        if len(counted) > 1:
            problems.append(f'both {counted} counted — they are alternatives')
    # And the one kept must be the BETTER of the pair.
    ggbs = [a for a in d['actions'] if a['key'].startswith('ggbs')]
    if ggbs and ggbs[0]['key'] != 'ggbs_70pct':
        problems.append(f'kept the weaker GGBS option: {ggbs[0]["key"]}')
    naive = sum(SENSITIVITY[k]['delta'] for k in
                ('ggbs_50pct', 'ggbs_70pct', 'slab_thickness_30pct',
                 'rebar_slab_15pct'))
    if d['combined_ton'] >= naive:
        problems.append(f'combined {d["combined_ton"]} did not exclude the '
                        f'duplicate GGBS (naive sum {naive:.2f})')
    print(f'  + GGBS counted once ({ggbs[0]["key"] if ggbs else "n/a"}); '
          f'combined {d["combined_ton"]} t, not the naive {naive:.2f} t')

    # ── 2. The combined figure is only ever open levers ─────────────────
    for stage in STAGE_LEVERS:
        s = build(stage)
        if any(not a['open'] for a in s['actions']):
            problems.append(f'{stage}: a locked lever was ranked as an action')
        recomputed = round(sum(a['delta_ton'] for a in s['actions']), 2)
        if abs(recomputed - s['combined_ton']) > 0.01:
            problems.append(f'{stage}: combined {s["combined_ton"]} != sum of '
                            f'actions {recomputed}')
    print('  + every stage: the combined figure is exactly its open actions')

    # ── 3. Stages differ, and close levers in the right order ───────────
    concept = build('Concept / Schematic Design')
    detailed = build('Detailed Design')
    tender = build('Tender')
    n_open = lambda x: sum(1 for s in x['scenarios'] if s['open'])  # noqa: E731

    if not (n_open(concept) > n_open(detailed) > n_open(tender)):
        problems.append(f'levers should close as the stage advances: '
                        f'{n_open(concept)} / {n_open(detailed)} / {n_open(tender)}')
    if any(s['open'] for s in tender['scenarios']):
        problems.append('a design lever is still open at Tender')
    if tender['epd_opportunity'] is None:
        problems.append('Tender offers no EPD opportunity')
    if concept['epd_opportunity'] is not None:
        problems.append('Concept offers EPDs, which do not exist that early')
    print(f'  + levers close in order: Concept {n_open(concept)} → '
          f'Detailed {n_open(detailed)} → Tender {n_open(tender)}; '
          f'EPDs offered only at Tender')

    # ── 4. Every locked lever explains itself ───────────────────────────
    for stage in STAGE_LEVERS:
        for s in build(stage)['scenarios']:
            if not s['open'] and not s['locked_reason']:
                problems.append(f'{stage}: {s["key"]} locked with no reason')
    print('  + every closed lever states why, rather than just vanishing')

    # ── 5. Nothing is invented when the engine produced nothing ─────────
    empty = build('Concept / Schematic Design', sensitivity={})
    if empty['combined_ton'] != 0 or empty['actions']:
        problems.append(f'invented {empty["combined_ton"]} t from no scenarios')
    if len(empty['scenarios']) != len(SCENARIOS):
        problems.append('scenario list changed when sensitivity was empty')
    print('  + no sensitivity results → no actions and a zero opportunity')

    # ── 6. Benchmarks include this run and rank correctly ───────────────
    rows = d['benchmarks']['rows']
    if not any(r['is_current'] for r in rows):
        problems.append('this run is missing from the comparison')
    if rows != sorted(rows, key=lambda r: r['per_sqm']):
        problems.append('benchmark rows are not ordered by intensity')
    if rows[0]['vs_best_pct'] != 0:
        problems.append(f'best row shows +{rows[0]["vs_best_pct"]}% against itself')
    current = next(r for r in rows if r['is_current'])
    if current['per_sqm'] != round(PER_SQM, 1):
        problems.append(f'this run shows {current["per_sqm"]}, expected {PER_SQM}')
    if current['cost'] is not None:
        problems.append('a cost was invented for the user\'s own project')
    if current['system'] != 'Steel + Hollowcore':
        problems.append(f'this run lost its system name: {current["system"]}')

    # Structural system is optional at upload. When it is absent the row must
    # carry None, not the words "This run" - the row already shows that marker,
    # and filling it in made the cell read "This run This run".
    anon = build_decarbonisation(
        stage_label='Detailed Design', sensitivity=SENSITIVITY, total_ton=TOTAL,
        per_sqm=PER_SQM, area=AREA, structural_system='',
        category_breakdown=CATS)
    anon_row = next(r for r in anon['benchmarks']['rows'] if r['is_current'])
    if anon_row['system'] is not None:
        problems.append(f'unnamed system became {anon_row["system"]!r}')
    print('  + an unnamed structural system stays None, not "This run"')

    # Every figure the tab shows is rounded like the headline it sits beside.
    for h in d['hotspots']:
        if round(h['total_ton'], 2) != h['total_ton']:
            problems.append(f'hotspot {h["category"]} shows {h["total_ton"]}, '
                            f'more precision than the rest of the dashboard')
    print(f'  + comparison: {len(rows)} systems, this run at '
          f'{current["per_sqm"]} kgCO₂e/m² ranked '
          f'{rows.index(current) + 1}/{len(rows)}, no invented cost')

    # ── 7. Hotspots reflect the real breakdown ──────────────────────────
    hs = d['hotspots']
    if [h['category'] for h in hs] != [c['key'] for c in CATS[:3]]:
        problems.append(f'hotspots do not follow the breakdown: {hs}')
    if abs(hs[0]['pct'] - round(CATS[0]['total_ton'] / TOTAL * 100, 1)) > 0.05:
        problems.append('hotspot percentage does not match the breakdown')
    print(f'  + hotspots: {", ".join(f"{h[chr(39)+chr(39)] if False else h["category"]} {h["pct"]}%" for h in hs)}')

    if problems:
        print('\nPROBLEMS:')
        for p in problems[:20]:
            print('   ', p)
        print(f'\nDECARBONISATION FAILED ({len(problems)})')
        return 1

    print('\nDECARBONISATION PASSED - savings are not double counted, locked '
          'levers never count, and nothing is invented')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
