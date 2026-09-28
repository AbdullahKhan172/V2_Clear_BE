"""
Where the files live.
=====================
Everything a run produces that is not a database row — the uploaded model, the
calculation DataFrame, the 3D meshes, the generated reports — goes through here,
addressed by an opaque `key` like `3f2a…/source.ifc`.

TWO BACKENDS, one interface:

    local   a directory on disk. The default, and what a developer runs.
    s3      any S3-compatible service: Cloudflare R2, AWS S3, MinIO.

Chosen by STORAGE_BACKEND. Nothing above this file knows which is in use, which
is the point: a separate Celery worker cannot see the web server's disk, so the
move to object storage is what makes a worker possible at all — and it has to be
a config change rather than a rewrite.

CONFIGURATION
    STORAGE_BACKEND        'local' | 's3'. Optional: complete S3 credentials
                           select 's3' on their own.
    BLOB_DIR               local only: where to put files

    S3_BUCKET              bucket name          (or R2_BUCKET_NAME)
    S3_ACCESS_KEY_ID       credentials          (or R2_ACCESS_KEY_ID)
    S3_SECRET_ACCESS_KEY                        (or R2_SECRET_ACCESS_KEY)
    S3_ENDPOINT_URL        R2: https://<account-id>.r2.cloudflarestorage.com
                           AWS: omit entirely
    R2_ACCOUNT_ID          builds that endpoint for you - Cloudflare gives you
                           an account id, not a URL
    S3_REGION              R2 wants 'auto' (the default here)

    R2_PUBLIC_URL is NOT used. Files are served through the API, which checks
    who owns a run before handing anything over; a public bucket URL would
    bypass that.

WHY KEYS AND NOT URLS
    A URL bakes in the host and bucket, and a signed one expires — so a link
    stored in January is dead in February. A key is permanent; a URL is minted
    from it when one is needed.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import BinaryIO, Iterable

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _setting(*names: str) -> str:
    """First of these environment variables that has a value."""
    for name in names:
        value = (os.environ.get(name) or '').strip()
        if value:
            return value
    return ''


# Cloudflare hands you an ACCOUNT ID, not an endpoint URL, and its docs name the
# variables R2_*. Both spellings are accepted so the values can be pasted in as
# Cloudflare gives them; S3_* wins if both are set.
_r2_account = _setting('R2_ACCOUNT_ID')

S3_BUCKET = _setting('S3_BUCKET', 'R2_BUCKET_NAME')
S3_ACCESS_KEY_ID = _setting('S3_ACCESS_KEY_ID', 'R2_ACCESS_KEY_ID')
S3_SECRET_ACCESS_KEY = _setting('S3_SECRET_ACCESS_KEY', 'R2_SECRET_ACCESS_KEY')
S3_ENDPOINT_URL = _setting('S3_ENDPOINT_URL') or (
    f'https://{_r2_account}.r2.cloudflarestorage.com' if _r2_account else '')
# R2 has no regions, but the SDK insists on one. 'auto' is what Cloudflare
# documents; on AWS set a real region.
S3_REGION = _setting('S3_REGION') or 'auto'

# Complete credentials are the signal that object storage is wanted. Requiring
# STORAGE_BACKEND on top of them would make "I filled in my R2 keys and the
# files still went to local disk" the default first experience - and on a host
# with an ephemeral disk, that failure is silent until the next deploy wipes it.
_has_s3_config = bool(S3_BUCKET and S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY)

BACKEND = (_setting('STORAGE_BACKEND').lower()
           or ('s3' if _has_s3_config else 'local'))

# Every key is validated against this: they are server-generated today, but this
# is the code an attacker-controlled key would reach, so it checks rather than
# trusts. Applies to BOTH backends - `..` in an S3 key is just as wrong.
def _clean_key(key: str) -> str:
    k = str(key or '').strip().lstrip('/')
    if not k or '\\' in k or any(part in ('.', '..') for part in k.split('/')):
        raise ValueError(f'Refusing unsafe storage key: {key!r}')
    return k


# ══════════════════════════════════════════════════════════════════════
#  LOCAL
# ══════════════════════════════════════════════════════════════════════

BLOB_ROOT = Path(os.environ.get('BLOB_DIR', BACKEND_ROOT / 'var' / 'blobs'))


class LocalStorage:
    """A directory. What a developer runs, and what a single-host deploy uses."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / _clean_key(key)).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise ValueError(f'Refusing key outside the store: {key!r}')
        return p

    def put_bytes(self, key: str, data: bytes) -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return key

    def put_stream(self, key: str, chunks: Iterable[bytes]) -> str:
        """Write without holding the whole thing in memory — uploads are big."""
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('wb') as fh:
            for chunk in chunks:
                fh.write(chunk)
        return key

    def get_bytes(self, key: str) -> bytes | None:
        p = self._path(key)
        return p.read_bytes() if p.exists() else None

    def download_to(self, key: str, dest: Path) -> bool:
        p = self._path(key)
        if not p.exists():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Copy rather than hand back the path: callers delete what they are
        # given, and on the local backend that would delete the only copy.
        shutil.copyfile(p, dest)
        return True

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete_prefix(self, prefix: str) -> None:
        target = self._path(prefix)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink(missing_ok=True)

    def local_path(self, key: str) -> Path | None:
        """The real path, when there is one. None on S3.

        Only for callers that genuinely need a filesystem path and can avoid a
        copy when the file is already local.
        """
        p = self._path(key)
        return p if p.exists() else None


# ══════════════════════════════════════════════════════════════════════
#  S3 / R2
# ══════════════════════════════════════════════════════════════════════

class S3Storage:
    """Cloudflare R2, AWS S3, or MinIO — the same API for all three."""

    def __init__(self):
        import boto3
        from botocore.config import Config

        if not S3_BUCKET:
            raise RuntimeError(
                'STORAGE_BACKEND is "s3" but no bucket is configured. Set '
                'S3_BUCKET (or R2_BUCKET_NAME), along with the access key, '
                'secret and endpoint.')
        self.bucket = S3_BUCKET
        self.client = boto3.client(
            's3',
            endpoint_url=S3_ENDPOINT_URL or None,
            aws_access_key_id=S3_ACCESS_KEY_ID,
            aws_secret_access_key=S3_SECRET_ACCESS_KEY,
            region_name=S3_REGION,
            config=Config(**_client_options()),
        )

    def put_bytes(self, key: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=_clean_key(key), Body=data)
        return key

    def put_stream(self, key: str, chunks: Iterable[bytes]) -> str:
        """Multipart upload, so a 200 MB model never sits in memory whole."""
        import io
        buffer = _ChunkReader(chunks)
        self.client.upload_fileobj(buffer, self.bucket, _clean_key(key))
        del io
        return key

    def get_bytes(self, key: str) -> bytes | None:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=_clean_key(key))
            return obj['Body'].read()
        except self.client.exceptions.NoSuchKey:
            return None
        except Exception as exc:                    # 404 arrives in several shapes
            if _is_missing(exc):
                return None
            raise

    def download_to(self, key: str, dest: Path) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.download_file(self.bucket, _clean_key(key), str(dest))
            return True
        except Exception as exc:
            if _is_missing(exc):
                return False
            raise

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=_clean_key(key))
            return True
        except Exception as exc:
            if _is_missing(exc):
                return False
            raise

    def delete_prefix(self, prefix: str) -> None:
        """Delete everything under a prefix, 1000 keys at a time.

        Tolerant by design: unlike a filesystem, a remote delete can half-fail
        or find nothing there, and neither should raise when the caller's intent
        is "make sure this is gone".
        """
        prefix = _clean_key(prefix).rstrip('/') + '/'
        paginator = self.client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [{'Key': o['Key']} for o in page.get('Contents', [])]
            if keys:
                self.client.delete_objects(Bucket=self.bucket,
                                           Delete={'Objects': keys})

    def local_path(self, key: str) -> Path | None:
        return None                                  # never local


def _client_options() -> dict:
    """botocore Config options, skipping any this version does not know.

    boto3 1.36 began sending streaming checksum headers by default, which
    Cloudflare R2 rejects - so those two options matter there. They do not exist
    in older botocore, and passing an unknown option raises. Rather than pinning
    a version and having the pin quietly decide whether R2 works, the options
    are offered and dropped if unsupported.
    """
    from botocore.config import Config

    options = {'retries': {'max_attempts': 3, 'mode': 'standard'}}
    for name in ('request_checksum_calculation', 'response_checksum_validation'):
        try:
            Config(**{name: 'when_required'})
        except TypeError:
            continue                        # botocore too old to need it
        options[name] = 'when_required'
    return options


class _ChunkReader:
    """Adapts an iterator of chunks to the file-like object boto3 wants."""

    def __init__(self, chunks: Iterable[bytes]):
        self._chunks = iter(chunks)
        self._buf = b''
        self._done = False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            rest = b''.join(self._chunks)
            out, self._buf, self._done = self._buf + rest, b'', True
            return out
        while len(self._buf) < size and not self._done:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                self._done = True
        out, self._buf = self._buf[:size], self._buf[size:]
        return out


def _is_missing(exc: Exception) -> bool:
    """Is this exception "the object is not there" rather than a real failure?

    S3 clients report a missing key as NoSuchKey, 404, or ClientError depending
    on the call, so this normalises them rather than each call site guessing.
    """
    code = getattr(getattr(exc, 'response', None), 'get', lambda *_: None)('Error')
    status = (getattr(exc, 'response', {}) or {}).get(
        'ResponseMetadata', {}).get('HTTPStatusCode')
    name = type(exc).__name__
    return (name in ('NoSuchKey', '404')
            or status == 404
            or (isinstance(code, dict) and code.get('Code') in ('404', 'NoSuchKey')))


# ══════════════════════════════════════════════════════════════════════

def _make():
    if BACKEND == 's3':
        return S3Storage()
    return LocalStorage(BLOB_ROOT)


store = _make()

# Module-level shorthands, so callers read as `storage.put_bytes(...)` rather
# than reaching through an object.
put_bytes = store.put_bytes
put_stream = store.put_stream
get_bytes = store.get_bytes
download_to = store.download_to
exists = store.exists
delete_prefix = store.delete_prefix
local_path = store.local_path

__all__ = ['BACKEND', 'BLOB_ROOT', 'store', 'put_bytes', 'put_stream',
           'get_bytes', 'download_to', 'exists', 'delete_prefix', 'local_path']
