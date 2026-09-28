"""
Shared test helpers.
====================
Small things every harness needs, in one place so a change to the application's
contract is a change to one file rather than eleven.
"""

from __future__ import annotations

from pathlib import Path

from app import blobs, store


def run_with_upload(*, filename: str, ext: str, source: Path, **fields):
    """Register a run whose upload is really IN storage.

    Harnesses used to hand `store.create()` a filesystem path and leave the file
    wherever the test wrote it. That stopped being true when uploads moved into
    the storage layer: a run now references its upload by KEY, and anything that
    reads it back (the preview endpoint, the parse job) goes through storage
    rather than opening a path.

    So this writes the bytes where the application will look for them. A test
    that skipped this step would pass while the real upload path was broken -
    which is the opposite of what these harnesses are for.
    """
    run = store.create(filename=filename, ext=ext, **fields)
    key = blobs.put_upload(run.id, ext, [Path(source).read_bytes()])
    return store.update(run.id, upload_key=key)
