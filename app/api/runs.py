"""
Run endpoints - upload, poll, fetch elements.
=============================================
Deliberately thin: validate, delegate to the job/service layer, serialise.
No calculation logic lives here, which is what keeps the engine testable
independently of the transport.

The upload -> poll -> fetch shape (rather than one blocking call) exists because
IFC parsing takes minutes. The legacy app blocked its /extract request in a sleep
loop for up to 120 s; that is fine on localhost and impossible over a network.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     Request, UploadFile)
from pydantic import BaseModel

from app import blobs, store
from app.owner import current_owner
from app.uploads import local_upload
from app.jobs.calculate import enqueue_calculate
from app.jobs.parse import enqueue_parse
from app.jobs.report import enqueue_reports
from app.services.extraction import SUPPORTED_EXTENSIONS

router = APIRouter(prefix='/api', tags=['runs'])

# Chunked read so a large IFC never has to sit in memory in one piece.
_CHUNK = 1024 * 1024


def _chunks(upload: UploadFile):
    """The upload as a stream of chunks, for storage.put_stream.

    Synchronous: both backends write synchronously, and boto3's uploader pulls
    from a file-like object. FastAPI runs this endpoint in a worker thread, so
    blocking here does not block the event loop.
    """
    while True:
        chunk = upload.file.read(_CHUNK)
        if not chunk:
            return
        yield chunk


class UploadResponse(BaseModel):
    run_id: str
    filename: str
    ext: str
    status: str


class StatusResponse(BaseModel):
    run_id: str
    status: str
    error: str | None = None
    n_elements: int | None = None


@router.post('/uploads', response_model=UploadResponse)
async def create_upload(
    file: UploadFile = File(...),
    gemini_api_key: str | None = Form(default=None),
    project_name: str | None = Form(default=None),
    area: float | None = Form(default=None),
    stage: str | None = Form(default=None),
    structural_system: str | None = Form(default=None),
    location: str | None = Form(default=None),
    client: str | None = Form(default=None),
    owner: str = Depends(current_owner),
):
    """Accept a model/BOQ file, register a run, and start parsing in background.

    Returns immediately with a run_id - the client then polls /status.
    Project details are stored alongside so the run is self-contained and can be
    resumed later from its id alone.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail='Empty filename.')

    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(f'File type "{ext}" is not supported. '
                    f'Use IFC, CSV or Excel (.xlsx/.xls).'))

    project = {
        'name': (project_name or '').strip() or 'Untitled',
        'area': area,
        'stage': stage,
        'structural_system': structural_system,
        'location': location,
        'client': client,
    }

    # Register first so the run id names the storage prefix. The client
    # filename is never used as a path component - only its validated suffix.
    run = store.create(filename=file.filename, ext=ext,
                       project=project, owner_id=owner)

    try:
        # Streamed in chunks all the way into storage: a real IFC runs to
        # hundreds of megabytes and must never sit in this process whole.
        key = blobs.put_upload(run.id, ext, _chunks(file))
    except Exception as exc:
        # A half-written object is worse than no run at all - the parse would
        # then fail later with a confusing error instead of here with a clear
        # one.
        store.delete(run.id)
        raise HTTPException(status_code=500,
                            detail=f'Could not save upload: {exc}') from exc
    finally:
        await file.close()

    store.update(run.id, upload_key=key)
    # The key is handed to the job in memory, never written to the run row.
    enqueue_parse(run.id, gemini_key=(gemini_api_key or '').strip() or None)

    return UploadResponse(run_id=run.id, filename=run.filename,
                          ext=ext, status='parsing')


@router.get('/runs')
async def list_runs(limit: int = Query(default=50, ge=1, le=200),
                    owner: str = Depends(current_owner)):
    """Every run this caller owns, newest first — the "your projects" screen.

    Deliberately a summary per run, not the full payload: this is the one
    endpoint whose cost grows with how much work someone has done.
    """
    return {'runs': store.list_for_owner(owner, limit=limit)}


class Progress(BaseModel):
    """A step the user has now reached.

    Only the high-water mark is kept. The CURRENT step lives in the URL, which
    already survives a refresh and drives browser back/forward - recording it
    here as well meant a write on every Back and every click in the step rail,
    for a number nothing read back.
    """
    step: int


@router.get('/runs/{run_id}/progress')
async def get_progress(run_id: str, owner: str = Depends(current_owner)):
    """How far this run got. Read on load, so resuming from another device
    unlocks the steps already reached instead of starting at Step 1."""
    _require_run_any(run_id, owner)
    return {'run_id': run_id, **store.get_progress(run_id)}


@router.put('/runs/{run_id}/progress')
async def put_progress(run_id: str, payload: Progress,
                       owner: str = Depends(current_owner)):
    """Raise the high-water mark.

    Called ONLY when the user opens a step they have not opened before - at most
    five times in a run's life, rather than once per click.
    """
    _require_run_any(run_id, owner)
    # A run being CHANGED is claimed by whoever is changing it; reading
    # one never was. No-op once it has an owner.
    store.adopt_if_unowned(run_id, owner)
    return {'run_id': run_id, **store.save_progress(run_id, payload.step)}


@router.get('/runs/{run_id}/status', response_model=StatusResponse)
async def get_status(run_id: str, owner: str = Depends(current_owner)):
    """Poll parse progress. The client advances when status == 'parsed'."""
    run = store.get_for_owner(run_id, owner)
    if run is None:
        raise HTTPException(status_code=404,
                            detail='Run not found. Please re-upload.')
    return StatusResponse(run_id=run.id, status=run.status,
                          error=run.error, n_elements=run.n_elements)


@router.get('/runs/{run_id}/elements')
async def get_elements(
    run_id: str,
    cat: list[str] | None = Query(default=None,
                                  description='Filter to these categories'),
    level: list[str] | None = Query(default=None,
                                    description='Filter to these levels'),
    steel_only: bool | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    owner: str = Depends(current_owner),
):
    """Extracted quantities, filtered and paged in SQL.

    Identical element contract for every ingest path (IFC, ready BOQ, Revit
    takeoff, scratch spreadsheet) - verified by tests/test_extraction_parity.py.
    Omit `limit` to get every row, which is what Step 1's summary does.
    """
    run = _require_parsed(run_id, owner)

    rows, total = store.get_elements(
        run_id, categories=cat, levels=level,
        steel_only=steel_only, limit=limit, offset=offset)

    payload = run.summary()
    payload['elements'] = rows
    payload['total'] = total
    payload['offset'] = offset
    payload['limit'] = limit
    return payload


@router.get('/runs/{run_id}/categories')
async def get_categories(run_id: str, owner: str = Depends(current_owner)):
    """Per-category counts and totals - what Step 2's filter chips are built on.

    Aggregated in SQL so the browser never downloads every row just to sum them.
    """
    _require_parsed(run_id, owner)
    return {'run_id': run_id, 'categories': store.category_totals(run_id)}


@router.get('/runs/{run_id}/groups')
async def get_groups(run_id: str, owner: str = Depends(current_owner)):
    """Everything Step 2's table needs, aggregated once.

    One row per (category, name, material, level) - which is exactly what the
    table displays. No element row numbers travel to the browser: level is part
    of the group key, so the client can switch categories and levels on and off
    and recompute every total locally, without another request.
    """
    _require_parsed(run_id, owner)
    return {
        'run_id': run_id,
        'groups': store.element_groups(run_id),
        'categories': store.category_totals(run_id),
        'levels': store.level_totals(run_id),
        'selection': store.get_selection(run_id),
    }


class Selection(BaseModel):
    """What the user has switched OFF in Step 2.

    Exclusions rather than inclusions, deliberately: a new category appearing
    after a re-parse should default to counted, not silently dropped.
    """
    excluded_categories: list[str] = []
    excluded_levels: list[str] = []
    excluded_groups: list[str] = []


@router.put('/runs/{run_id}/selection')
async def put_selection(run_id: str, selection: Selection,
                        owner: str = Depends(current_owner)):
    """Save the Step-2 selection. Called on every change, so a refresh or a
    closed tab loses nothing. Idempotent - it replaces, never merges."""
    _require_parsed(run_id, owner)
    # A run being CHANGED is claimed by whoever is changing it; reading
    # one never was. No-op once it has an owner.
    store.adopt_if_unowned(run_id, owner)
    saved = store.save_selection(run_id, selection.model_dump())
    return {'run_id': run_id, 'selection': saved}


class MaterialRow(BaseModel):
    """Per-element overrides. Every field optional - a row the user never touched
    stores nothing and inherits the global defaults."""
    grade: str | None = None
    ggbs: int | None = None
    rebarRate: float | None = None
    ptRate: float | None = None
    steelEid: str | None = None          # steel rows: chosen factor
    fullEid: str | None = None           # unknown-material rows: any factor
    concFactorEid: str | None = None     # concrete row assigned a named factor


class EpdOverride(BaseModel):
    """A supplier's certified A1-A3 value, replacing the catalogue factor.

    Concrete is entered per m3 (how EPDs are published); everything else per kg.
    The conversion happens at calculation time, matching the legacy engine.
    """
    a1_a3: float
    source: str = ''
    mat_type: str = ''
    grade: str | None = None
    ggbs: int | None = None
    e_id: str | None = None


class MaterialsPayload(BaseModel):
    defaults: dict = {}
    rows: dict[str, MaterialRow] = {}
    epd_overrides: dict[str, EpdOverride] = {}


@router.get('/runs/{run_id}/materials')
async def get_materials(run_id: str, owner: str = Depends(current_owner)):
    """The Step-3 table plus whatever has been saved for it.

    Rows are the elements that survived Step 2, merged across levels: a grade is
    a property of the element type, not of the storey it sits on.
    """
    _require_parsed(run_id, owner)
    return {
        'run_id': run_id,
        'rows': store.material_rows(run_id),
        'materials': store.get_materials(run_id),
    }


@router.put('/runs/{run_id}/materials')
async def put_materials(run_id: str, payload: MaterialsPayload,
                        owner: str = Depends(current_owner)):
    """Save Step-3 settings. Called as the user edits, like the selection."""
    _require_parsed(run_id, owner)
    # A run being CHANGED is claimed by whoever is changing it; reading
    # one never was. No-op once it has an owner.
    store.adopt_if_unowned(run_id, owner)
    saved = store.save_materials(run_id, {
        'defaults': payload.defaults,
        'rows': {k: v.model_dump(exclude_none=True)
                 for k, v in payload.rows.items()},
        'epd_overrides': {k: v.model_dump()
                          for k, v in payload.epd_overrides.items()},
    })
    return {'run_id': run_id, 'materials': saved}


class TransportLegs(BaseModel):
    sea: float = 0
    road: float = 0


class TransportPayload(BaseModel):
    """Step-4 values.

    `waste_pct` is a PERCENT (5 = 5%), matching what the user typed. The two
    conversions the engine needs - to a multiplier and then to a fraction - are
    done in app/store.py so they exist in exactly one place.

    `a5a_factor` of None means "not overridden": the engine applies the SEAI
    default. That is distinct from 0, which is a real choice meaning site
    activity is deliberately excluded.
    """
    distances: dict[str, TransportLegs] = {}
    waste_pct: dict[str, float] = {}
    a5a_factor: float | None = None


@router.get('/runs/{run_id}/transport')
async def get_transport(run_id: str, owner: str = Depends(current_owner)):
    """Saved transport distances, waste allowances and site-activity factor."""
    _require_parsed(run_id, owner)
    return {'run_id': run_id, 'transport': store.get_transport(run_id)}


@router.put('/runs/{run_id}/transport')
async def put_transport(run_id: str, payload: TransportPayload,
                        owner: str = Depends(current_owner)):
    """Save Step-4 values.

    A5a is validated HERE as well as at calculation time. A value that is present
    but out of range is rejected rather than quietly replaced with the default:
    the whole A5a term is factor x floor area, so silently substituting a number
    the user never chose would change the result with nothing to show for it.
    """
    _require_parsed(run_id, owner)

    if payload.a5a_factor is not None:
        from calculations import (A5A_EMISSION_FACTOR_KGCO2E_PER_SQM,
                                  A5A_MAX_KGCO2E_PER_SQM,
                                  A5A_MIN_KGCO2E_PER_SQM)
        if not (A5A_MIN_KGCO2E_PER_SQM <= payload.a5a_factor
                <= A5A_MAX_KGCO2E_PER_SQM):
            raise HTTPException(
                status_code=422,
                detail=(f'Site activity (A5a) must be between '
                        f'{A5A_MIN_KGCO2E_PER_SQM:g} and '
                        f'{A5A_MAX_KGCO2E_PER_SQM:g} kgCO2e/m². Clear the field '
                        f'to use the SEAI default of '
                        f'{A5A_EMISSION_FACTOR_KGCO2E_PER_SQM:g}.'))

    for material, pct in payload.waste_pct.items():
        if pct < 0 or pct > 50:
            raise HTTPException(
                status_code=422,
                detail=(f'Waste for {material} must be between 0% and 50% — '
                        f'got {pct:g}%.'))

    # A run being CHANGED is claimed by whoever is changing it; reading
    # one never was. No-op once it has an owner.
    store.adopt_if_unowned(run_id, owner)
    saved = store.save_transport(run_id, {
        'distances': {k: v.model_dump() for k, v in payload.distances.items()},
        'waste_pct': payload.waste_pct,
        'a5a_factor': payload.a5a_factor,
    })
    return {'run_id': run_id, 'transport': saved}


@router.get('/runs/{run_id}/data-check')
async def get_data_check(run_id: str, owner: str = Depends(current_owner)):
    """Read-only review of every factor this run will apply.

    Derived from the catalogue CSV at request time. The legacy wizard hardcoded
    these tables in HTML and two of its 41 concrete values had already drifted
    from the CSV, so the review screen disagreed with the calculation.
    """
    run = _require_parsed(run_id, owner)
    from app.services.data_check import build_data_check
    return {
        'run_id': run_id,
        **build_data_check(
            run,
            store.material_rows(run_id),
            store.get_materials(run_id),
            store.get_transport(run_id),
        ),
    }


@router.post('/runs/{run_id}/calculate', status_code=202)
async def post_calculate(run_id: str, sensitivity: bool = Query(default=True),
                         owner: str = Depends(current_owner)):
    """Start the calculation. Returns immediately; poll /status.

    202 rather than 200: accepted, not finished. Sensitivity re-runs the engine
    up to nine times, so this cannot be a synchronous request.
    """
    run = store.get_for_owner(run_id, owner)
    if run is None:
        raise HTTPException(status_code=404, detail='Run not found.')
    if run.status not in ('parsed', 'complete', 'failed'):
        raise HTTPException(
            status_code=409,
            detail=f'Cannot calculate while the run is "{run.status}".')

    area = (run.project or {}).get('area')
    if not area or float(area) <= 0:
        # Caught here rather than inside the job so the user is told at once.
        # Without GIA the per-m2 intensity collapses to 0 and the SCORS rating
        # would falsely read A++.
        raise HTTPException(
            status_code=422,
            detail=('Gross Internal Area (m²) is required and must be greater '
                    'than 0. Set it in Step 1.'))

    # A run being CHANGED is claimed by whoever is changing it; reading
    # one never was. No-op once it has an owner.
    store.adopt_if_unowned(run_id, owner)
    enqueue_calculate(run_id, sensitivity=sensitivity)
    return {'run_id': run_id, 'status': 'calculating'}


@router.get('/runs/{run_id}/results')
async def get_results(run_id: str, owner: str = Depends(current_owner)):
    """Headline figures, stage and material splits, sensitivity and assumptions."""
    run = _require_run(run_id, owner)
    results = store.get_results(run_id)
    if results is None:
        raise HTTPException(
            status_code=409,
            detail=('Not calculated yet. POST to /calculate, then poll /status '
                    'until it reads "complete".'))
    return {'run_id': run_id, 'project': run.project or {}, **results}


@router.get('/runs/{run_id}/results/breakdown')
async def get_breakdown(run_id: str,
                        by: str = Query(default='material',
                                        pattern='^(material|category|level)$'),
                        owner: str = Depends(current_owner)):
    """Emissions grouped for charting. One query per chart, no re-calculation."""
    _require_run(run_id, owner)
    if store.get_results(run_id) is None:
        raise HTTPException(status_code=409, detail='Not calculated yet.')
    return {'run_id': run_id, 'by': by,
            'breakdown': store.result_breakdown(run_id, by)}


@router.get('/runs/{run_id}/results/rows')
async def get_result_rows(run_id: str,
                          limit: int | None = Query(default=None, ge=1, le=5000),
                          offset: int = Query(default=0, ge=0),
                          owner: str = Depends(current_owner)):
    """Priced BOQ lines, biggest contributor first."""
    _require_run(run_id, owner)
    if store.get_results(run_id) is None:
        raise HTTPException(status_code=409, detail='Not calculated yet.')
    rows, total = store.result_rows(run_id, limit=limit, offset=offset)
    return {'run_id': run_id, 'rows': rows, 'total': total,
            'offset': offset, 'limit': limit}


@router.get('/runs/{run_id}/dashboard')
async def get_dashboard(run_id: str, owner: str = Depends(current_owner)):
    """Everything the dashboard needs except geometry, in one call.

    Geometry is a separate endpoint because it can be tens of megabytes on a
    real model, and the charts and KPIs should render without waiting for it.
    """
    from app.services import dashboard as dash
    from app.services import data_check as dc
    from app.services import decarb

    run = _require_run(run_id, owner)
    results = store.get_results(run_id)
    if results is None:
        raise HTTPException(
            status_code=409,
            detail='Not calculated yet. POST to /calculate first.')

    by_category = store.result_breakdown(run_id, 'category')
    by_material = store.result_breakdown(run_id, 'material')
    by_level = store.result_breakdown(run_id, 'level')
    rows, _ = store.result_rows(run_id)
    area = float((run.project or {}).get('area') or 0)

    return {
        'run_id': run_id,
        'project': run.project or {},
        'filename': run.filename,
        'source_type': run.source_type,
        **results,
        'breakdown': {'material': by_material, 'category': by_category,
                      'level': by_level},
        'hotspots': dash.hotspots(rows, limit=5),
        'substructure': dash.substructure_split(by_category),
        'member_efficiency': dash.member_efficiency(by_category, area),
        'social_cost': dash.social_cost(results['total_ton']),
        'budget': dash.budget_vs_scors_b(results['per_sqm'], area),
        'benchmark': dash.benchmark_comparison(results['per_sqm']),
        'scors_bands': dash.scors_bands(),
        # The constants behind every figure, read from calculations.py so the
        # methodology note cannot drift from the arithmetic.
        'methodology': dash.methodology(),
        # What this design stage should foreground, and what it should retire.
        'stage_focus': decarb.stage_focus(results.get('stage_label') or ''),
        # Transport and waste AS APPLIED, for the assumptions panel. Reused from
        # the Data Check service rather than recomputed, so the pre-run review
        # and the post-run report can never disagree about what was used.
        'transport': dc._transport_summary(store.get_transport(run_id)),
        'waste': dc._waste_summary(store.get_transport(run_id)),
        'reports': store.get_reports(run_id),
        'decarbonisation': decarb.build_decarbonisation(
            stage_label=results.get('stage_label') or '',
            sensitivity=results.get('sensitivity') or {},
            total_ton=results['total_ton'], per_sqm=results['per_sqm'],
            area=area,
            structural_system=str((run.project or {}).get('structural_system') or ''),
            category_breakdown=by_category),
    }


@router.get('/runs/{run_id}/geometry')
async def get_geometry(request: Request, run_id: str,
                       owner: str = Depends(current_owner)):
    """The 3D viewer's element list, with meshes where the model provided them.

    Separate from /dashboard and potentially large. An element excluded in
    Step 2 is present but reads zero everywhere - deliberately, so the cost of a
    filtering decision stays visible in the model rather than being hidden by a
    category average.
    """
    import gzip

    from fastapi import Response

    from app import blobs

    _require_run(run_id, owner)

    # Precomputed when the run was calculated. Served as raw bytes: the payload
    # runs to megabytes, and parsing it here only to have FastAPI re-serialise
    # it is the cost this endpoint used to pay on every single load.
    raw = blobs.get_viewer_gzip(run_id)

    if raw is None:
        # Calculated before the payload was precomputed. Build it once and store
        # it, so every load after this one is a read - rather than telling the
        # user to recalculate just to get a dashboard that opens quickly.
        raw = _build_and_cache_viewer(run_id)

    if raw is None:
        return {'run_id': run_id, 'elements': [], 'has_meshes': False,
                'note': 'This run has no 3D geometry — it came from a '
                        'spreadsheet rather than an IFC model.'}

    # Already gzipped on disk. Handed straight to the browser, which is the
    # only reason this is fast: nothing here decompresses 8 MB to recompress it.
    # A client that cannot take gzip gets it expanded, which no browser needs.
    if 'gzip' in request.headers.get('accept-encoding', '').lower():
        return Response(content=raw, media_type='application/json',
                        headers={'Content-Encoding': 'gzip'})
    return Response(content=gzip.decompress(raw), media_type='application/json')


def _build_and_cache_viewer(run_id: str) -> bytes | None:
    """The old per-request build, kept only for runs that predate the blob.

    Rebuilds the priced-row frame the geometry matcher expects. It matches on
    the legacy column names (Description, Total Emission(tCO2e), A1-A3
    Emission(kgCO2e), Total Volume(m3), e_id, Material), so the stored result
    rows are mapped back onto those names rather than the matcher being changed
    - keeping one matching implementation, not two.

    Volume comes from the stored column, never from mass / an assumed density:
    2400 and 7850 are wrong for timber, blockwork and every other material, and
    the viewer colours elements by kgCO2e per m3.
    """
    import pandas as pd

    from app import blobs
    from app.services import dashboard as dash

    geometry = store.get_geometry(run_id)
    if not geometry:
        return None

    rows, _ = store.result_rows(run_id)
    detailed_df = pd.DataFrame([{
        'Description': r['description'],
        'Total Emission(tCO2e)': r['total_kg'] / 1000.0,
        'A1-A3 Emission(kgCO2e)': r['a1_a3'],
        'Total Volume(m3)': r['volume'],
        'e_id': r['e_id'],
        'Material': r['material'],
    } for r in rows]) if rows else None

    elements = dash.build_geometry_payload(geometry, detailed_df)
    payload = {
        'run_id': run_id,
        'elements': elements,
        'has_meshes': any('v' in e for e in elements),
        'count': len(elements),
    }
    blobs.put_viewer(run_id, payload)
    return blobs.get_viewer_gzip(run_id)


@router.get('/runs/{run_id}/reports')
async def list_reports(run_id: str, owner: str = Depends(current_owner)):
    """What has been generated, and the state of the last generation."""
    _require_run(run_id, owner)
    from app.services.reports import REPORT_KINDS
    reports = store.get_reports(run_id)
    return {
        'run_id': run_id,
        'available': [{'kind': k, 'label': label, 'ext': ext}
                      for k, (ext, _mime, label) in REPORT_KINDS.items()],
        **reports,
    }


@router.post('/runs/{run_id}/reports', status_code=202)
async def post_reports(run_id: str,
                       kinds: list[str] = Query(default=['html']),
                       owner: str = Depends(current_owner)):
    """Generate the named reports in the background. Poll GET /reports."""
    from app.services.reports import REPORT_KINDS

    _require_run(run_id, owner)
    unknown = [k for k in kinds if k not in REPORT_KINDS]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(f'Unknown report type(s): {", ".join(unknown)}. '
                    f'Choose from {", ".join(sorted(REPORT_KINDS))}.'))
    if store.get_results(run_id) is None:
        raise HTTPException(
            status_code=409,
            detail='Calculate the run before generating reports.')

    # A run being CHANGED is claimed by whoever is changing it; reading
    # one never was. No-op once it has an owner.
    store.adopt_if_unowned(run_id, owner)
    enqueue_reports(run_id, kinds)
    return {'run_id': run_id, 'status': 'generating', 'requested': kinds}


@router.get('/runs/{run_id}/reports/{kind}/download')
async def download_report(run_id: str, kind: str,
                          owner: str = Depends(current_owner)):
    """Serve a generated report as an attachment."""
    from fastapi.responses import FileResponse

    from app import blobs

    _require_run(run_id, owner)
    entry = (store.get_reports(run_id).get('files') or {}).get(kind)
    if not entry:
        raise HTTPException(
            status_code=404,
            detail=f'No {kind} report has been generated for this run yet.')

    path = blobs.BLOB_ROOT / entry['key']
    if not path.exists():
        raise HTTPException(status_code=410,
                            detail='That report file is no longer available. '
                                   'Generate it again.')
    # The filename is sanitised at generation time, so a project name can never
    # smuggle a path separator or CRLF into the Content-Disposition header.
    return FileResponse(path, media_type=entry['mime'],
                        filename=entry['filename'])


@router.delete('/runs/{run_id}/reports/{kind}', status_code=204)
async def remove_report(run_id: str, kind: str,
                        owner: str = Depends(current_owner)):
    """Delete one generated report."""
    from app.jobs.report import delete_report
    _require_run(run_id, owner)
    delete_report(run_id, kind)


@router.get('/runs/{run_id}/preview')
async def get_preview(run_id: str, owner: str = Depends(current_owner)):
    """The uploaded spreadsheet as the parser understood it ("Review input file").

    There is no equivalent for IFC - a model is not a table, and Step 2's element
    list is itself the review surface.
    """
    run = store.get_for_owner(run_id, owner)
    if run is None:
        raise HTTPException(status_code=404, detail='Run not found.')
    if (run.ext or '').lower() == '.ifc':
        return {'kind': 'ifc',
                'message': ('IFC model - review the extracted elements in the '
                            'table below.')}
    try:
        import pandas as pd

        from app import core_bridge  # noqa: F401
        from boq_parser import _clean_excel_layout

        # The parser wants a real path, and the object may live in R2, so it
        # is materialised into a temp file and removed again.
        with local_upload(run) as path:
            df = _clean_excel_layout(str(path)).head(50)
        return {
            'kind': 'table',
            'columns': [str(c) for c in df.columns],
            'rows': [[('' if pd.isna(v) else str(v)) for v in row]
                     for row in df.itertuples(index=False, name=None)],
            'truncated': len(df) >= 50,
        }
    except Exception as exc:
        raise HTTPException(status_code=422,
                            detail=f'Could not preview this file: {exc}') from exc


@router.delete('/runs/{run_id}', status_code=204)
async def delete_run(run_id: str, owner: str = Depends(current_owner)):
    """Remove a run, its element rows, its upload and its blobs."""
    if store.get_for_owner(run_id, owner) is None:
        raise HTTPException(status_code=404, detail='Run not found.')
    store.delete(run_id)


def _require_run_any(run_id: str, owner: str):
    """Resolve a run in ANY state.

    Distinct from _require_run: progress is meaningful while a run is still
    parsing, and after it has failed. Refusing it in those states is what would
    strand a user on a reload mid-parse — the exact case this endpoint exists
    for.
    """
    run = store.get_for_owner(run_id, owner)
    if run is None:
        raise HTTPException(status_code=404,
                            detail='Run not found. Please re-upload.')
    return run


def _require_run(run_id: str, owner: str):
    """Resolve a run that has been parsed at some point.

    Distinct from _require_parsed: once a calculation has started, status moves
    past 'parsed' to 'calculating'/'complete', and the results endpoints must
    still work in those states.
    """
    run = store.get_for_owner(run_id, owner)
    if run is None:
        raise HTTPException(status_code=404,
                            detail='Run not found. Please re-upload.')
    if run.calculated_at is None and run.status not in (
            'parsed', 'calculating', 'complete'):
        raise HTTPException(status_code=409,
                            detail=f'Run is "{run.status}".')
    return run


def _require_parsed(run_id: str, owner: str):
    """Resolve a run, or raise the right status for why it is not usable yet."""
    run = store.get_for_owner(run_id, owner)
    if run is None:
        raise HTTPException(status_code=404,
                            detail='Run not found. Please re-upload.')
    if run.status == 'failed':
        raise HTTPException(status_code=422,
                            detail=f'Could not parse this file: {run.error}')
    if run.status not in ('parsed', 'calculating', 'complete'):
        # 409: valid request, resource not ready. The client keeps polling
        # /status rather than treating this as an error. 'calculating' and
        # 'complete' are accepted so the wizard steps stay editable after a run.
        raise HTTPException(status_code=409,
                            detail='Still parsing. Poll /status and retry.')
    return run
