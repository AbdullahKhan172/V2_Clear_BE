"""
The things too big or too typed for a database row.
===================================================
This module knows about the DOMAIN objects — a DataFrame, a mesh list, a viewer
payload, an uploaded model. It knows nothing about where they are kept; that is
app/storage.py, which is either a local directory or S3/R2 depending on one
environment variable.

The split matters because the two questions have different answers and change
for different reasons: *how do I serialise a DataFrame without losing dtypes*
never changes, while *where do the bytes go* changes the day a Celery worker
stops sharing a disk with the web server.

Everything for a run lives under one prefix — `<run_id>/…` — so deleting a run
is a single call, and no caller has to remember which of five files it produced.

  <run_id>/source.ifc        the upload, exactly as it arrived
  <run_id>/frame.parquet     the DataFrame the CALCULATION consumes. Parquet
                             because it round-trips dtypes exactly - verified
                             with assert_frame_equal(check_exact=True) on both
                             the IFC (28 col) and BOQ (17 col) frames. JSON
                             coerces NaN to null and ints to floats, which
                             changes carbon numbers silently rather than loudly.
  <run_id>/geometry.json     3D meshes as EXTRACTED
  <run_id>/viewer.json.gz    the finished /geometry response, pre-gzipped
  <run_id>/reports/*         generated xlsx / docx / html
"""

from __future__ import annotations

import gzip
import io
import json

import pandas as pd

from app import storage

# Re-exported: reports/ still writes generated files through a real path, and is
# the one caller that needs a directory rather than a key.
BLOB_ROOT = storage.BLOB_ROOT


def upload_key(run_id: str, ext: str) -> str:
    """Where an upload goes. Under the run's own prefix, beside the blobs.

    Sharing the prefix is deliberate: delete_run() is then one call that cannot
    miss a file, and the key stays valid whether the backend is a disk or R2.
    Only the validated extension is used - a client filename never becomes part
    of a path.
    """
    return f'{run_id}/source{ext}'


def put_upload(run_id: str, ext: str, chunks) -> str:
    """Stream an upload into storage. Returns its key.

    Streamed rather than read whole: a real IFC runs to hundreds of megabytes
    and must never sit in the web process's memory in one piece.
    """
    key = upload_key(run_id, ext)
    storage.put_stream(key, chunks)
    return key


def put_frame(run_id: str, df: pd.DataFrame) -> str:
    """Persist the calculation DataFrame. Returns its key."""
    buf = io.BytesIO()
    # index=True: the wizard's row exclusions are positional and the engine
    # indexes into this frame, so the original index must survive the round trip.
    df.to_parquet(buf, index=True)
    key = f'{run_id}/frame.parquet'
    storage.put_bytes(key, buf.getvalue())
    return key


def get_frame(key: str) -> pd.DataFrame | None:
    """Read back a DataFrame, dtypes and all. None if the object is gone."""
    raw = storage.get_bytes(key)
    return pd.read_parquet(io.BytesIO(raw)) if raw is not None else None


def put_geometry(run_id: str, geometry: list) -> str | None:
    """Persist 3D-viewer meshes. Returns None when there are none (every CSV)."""
    if not geometry:
        return None
    key = f'{run_id}/geometry.json'
    storage.put_bytes(key, json.dumps(geometry, separators=(',', ':')).encode())
    return key


def get_geometry(key: str | None) -> list:
    if not key:
        return []
    raw = storage.get_bytes(key)
    return json.loads(raw) if raw else []


def put_viewer(run_id: str, payload: dict) -> str | None:
    """Persist the FINISHED /geometry response, gzipped, ready to serve.

    Two decisions, both about not repeating work on every dashboard load:

      the WHOLE envelope, not just the element list, so the endpoint never
      parses or re-serialises it - on a 36-element model the join took 0.59 s
      but the request took 6.2 s, and the rest was JSON

      GZIPPED at rest, because the payload is vertex arrays and repeated keys:
      8.1 MB becomes 818 KB. Compressing per request cost 1.4 s of CPU to
      produce identical bytes every time. Compress once; serve forever.

    Both inputs are frozen once the calculation finishes, so this is as correct
    on the thousandth read as the first.
    """
    if not payload or not payload.get('elements'):
        return None
    raw = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    key = f'{run_id}/viewer.json.gz'
    # 6 is gzip's default: the bulk of the ratio for a fraction of the time of
    # 9, and this runs inside a calculation the user is already waiting on.
    storage.put_bytes(key, gzip.compress(raw, compresslevel=6))
    return key


def get_viewer_gzip(run_id: str) -> bytes | None:
    """The stored response, still gzipped. None if it was never built.

    Returned compressed because that is what goes on the wire - the endpoint
    hands these bytes to a browser with `Content-Encoding: gzip` and neither
    side ever materialises the 8 MB.

    None is not an error: a run calculated before this existed simply has no
    file, and the caller builds it. Keyed off run_id rather than a stored
    column, so there is no second source of truth about whether it is there.
    """
    return storage.get_bytes(f'{run_id}/viewer.json.gz')


def delete_run(run_id: str) -> None:
    """Remove every file for a run — upload, frame, meshes, viewer, reports.

    One call, because everything shares the run's prefix. Tolerant of things
    already being gone: unlike a filesystem, a remote delete can half-fail or
    find nothing there, and the caller's intent is "make sure this is gone" -
    a run should not become undeletable because its files already are.
    """
    try:
        storage.delete_prefix(run_id)
    except Exception as exc:
        print(f'[blobs] could not delete files for {run_id}: {exc}')
