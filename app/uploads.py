"""
Getting an uploaded file onto a real path.
==========================================
The parsers need a filesystem path and cannot be given bytes:

    ifcopenshell.open(self.ifc_path)        ifc_processor.py:472

So an upload stored in R2 has to be materialised before it can be read. That is
all this module does — fetch the object to a temp file, hand over the path, and
remove it afterwards whether the parse succeeded or raised.

On the local backend there is nothing to fetch, but the file is copied anyway
rather than the real path being handed back. A caller that received the live
object and deleted it would be deleting the only copy, and the difference
between the two backends would be a bug that only appears in deployment.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path

from app import storage


class UploadMissing(RuntimeError):
    """The stored upload is gone — evicted, deleted, or never written."""


@contextmanager
def local_upload(run):
    """Yield a real path to this run's uploaded file, and clean it up after.

        with local_upload(run) as path:
            process(str(path))

    Raises UploadMissing rather than returning a path to nothing, so a caller
    cannot hand a non-existent file to a parser and get an obscure error from
    deep inside it.
    """
    key = getattr(run, 'upload_key', '') or ''
    if not key:
        raise UploadMissing('This run has no stored upload.')

    # Suffix preserved: the parsers dispatch on it, and a temp file called
    # `.tmp` would be routed to the wrong reader.
    suffix = Path(key).suffix or getattr(run, 'ext', '') or ''
    handle, tmp_name = tempfile.mkstemp(prefix='clear_upload_', suffix=suffix)
    tmp = Path(tmp_name)
    import os
    os.close(handle)

    try:
        if not storage.download_to(key, tmp):
            raise UploadMissing(
                f'The uploaded file for this run is no longer available '
                f'({key}). Please re-upload it.')
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)
