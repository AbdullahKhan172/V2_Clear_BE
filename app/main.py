"""
C.L.E.A.R. API - FastAPI application entry point.
=================================================
Run (from the project root):
    .venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000 \
        --app-dir webapp/backend

Interactive docs: http://localhost:8000/docs
OpenAPI schema:   http://localhost:8000/openapi.json  (source of the frontend types)

The legacy Flask app (web_app.py, port 8080) is untouched and can run alongside;
that is what lets the parity harness diff the two implementations.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.catalogue import router as catalogue_router
from app.api.runs import router as runs_router
from app.db import DATABASE_URL, init_db


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Prepare a local database on boot.

    On SQLite this creates any missing tables, so a developer can start the app
    and use it. On Postgres it does nothing: the schema belongs to Alembic, and
    a deployment runs `alembic upgrade head` before this process starts.

    The app deliberately does NOT migrate itself at startup. With more than one
    instance they would race each other, and a failed migration would take the
    web process down with it rather than failing the deploy.
    """
    init_db()
    backend = DATABASE_URL.split('://')[0]
    print(f'[db] {backend} ready'
          + ('' if 'sqlite' in backend else ' (schema managed by Alembic)'))

    # Say which store is live. "I set my R2 keys but the files went to local
    # disk" is invisible until a deploy wipes the disk, so it is worth one line.
    from app import storage
    if storage.BACKEND == 's3':
        print(f'[storage] s3 -> {storage.S3_ENDPOINT_URL or "aws"} '
              f'bucket={storage.S3_BUCKET}')
    else:
        print(f'[storage] local -> {storage.BLOB_ROOT}')
    yield

app = FastAPI(
    title='C.L.E.A.R. API',
    version='0.1.0',
    description=('Structural embodied carbon assessment (A1-A5). '
                 'Wraps the verified calculation engine in src/core and '
                 'src/ingest - no numerical logic lives in this layer.'),
    lifespan=lifespan,
)

# ── Who may call this API from a browser ────────────────────────────────
# In development the Vite dev server proxies /api, so the browser sees one
# origin and CORS never applies. It applies the moment the frontend is deployed
# somewhere else - Vercel, say - and then the exact origin has to be listed.
#
#   ALLOWED_ORIGINS        comma-separated, e.g. https://clear.vercel.app
#   ALLOWED_ORIGIN_REGEX   optional, for hosts that mint a URL per deployment.
#                          Vercel preview builds look like
#                          https://<project>-<hash>-<team>.vercel.app
#
# "*" is deliberately NOT accepted. With allow_credentials the browser rejects
# it anyway, so it would fail confusingly at runtime rather than here.

DEV_ORIGINS = ['http://localhost:5173', 'http://127.0.0.1:5173']


def _allowed_origins() -> list[str]:
    configured = [o.strip().rstrip('/')
                  for o in os.environ.get('ALLOWED_ORIGINS', '').split(',')
                  if o.strip()]
    if '*' in configured:
        raise RuntimeError(
            'ALLOWED_ORIGINS cannot be "*": this API sends credentials, and a '
            'browser refuses a wildcard origin on a credentialed request. List '
            'the exact origin, or use ALLOWED_ORIGIN_REGEX for per-deployment '
            'URLs.')
    # The dev origins stay allowed alongside anything configured, so running the
    # frontend locally against a deployed API just works.
    return configured + DEV_ORIGINS


_origin_regex = os.environ.get('ALLOWED_ORIGIN_REGEX') or None

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_origin_regex=_origin_regex,
    allow_credentials=True,
    allow_methods=['*'],
    # X-Client-Id is a custom header, so every cross-origin request is
    # preflighted. Narrowing this list means remembering to keep it in it.
    allow_headers=['*'],
)

# The 3D viewer payload is megabytes of highly repetitive JSON - vertex arrays
# and repeated keys - so it compresses by roughly an order of magnitude. The
# minimum size keeps small responses from paying for a compressor they do not
# need; already-compressed downloads (xlsx, docx) are served by FileResponse and
# gain nothing, but lose nothing either.
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.include_router(runs_router)
app.include_router(catalogue_router)


@app.get('/api/health', tags=['meta'])
async def health():
    """Liveness probe; also confirms the engine and catalogue actually load."""
    from app import core_bridge  # noqa: F401
    from catalogue import CATALOGUE_VERSION, EmissionCatalogue

    return {
        'status': 'ok',
        'catalogue_version': CATALOGUE_VERSION,
        'catalogue_rows': len(EmissionCatalogue().df),
    }
