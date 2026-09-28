"""
Move uploads from the old var/uploads tree into the storage layer.
==================================================================
    python webapp/backend/scripts/migrate_uploads.py --dry-run
    python webapp/backend/scripts/migrate_uploads.py

Uploads used to be written to `var/uploads/<run_id>/source.<ext>` and referenced
by an absolute path. They now live under the run's own storage prefix,
`<run_id>/source.<ext>`, beside the frame and the meshes — so one delete removes
a run, and the reference resolves from a worker that has never seen this disk.

The Alembic migration rewrites the database VALUES. It cannot move the files:
a migration runs against a database, and on S3 there is no file to move. That is
this script's job, and it is only needed once, on a machine whose old uploads
are still on disk.

Safe to re-run: a file already in place is left alone, and nothing is deleted
until the copy is verified.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app import storage  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Run  # noqa: E402

OLD_ROOT = BACKEND / 'var' / 'uploads'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true',
                    help='say what would move, change nothing')
    ap.add_argument('--delete-old', action='store_true',
                    help='remove var/uploads afterwards (only once you are '
                         'satisfied every run still opens)')
    args = ap.parse_args()

    if not OLD_ROOT.is_dir():
        print(f'Nothing to do: {OLD_ROOT} does not exist.')
        return 0

    with SessionLocal() as session:
        runs = session.query(Run).all()
        rows = [(r.id, r.ext or '', r.upload_key or '') for r in runs]

    moved = already = missing = 0
    for run_id, ext, key in rows:
        if not key:
            continue
        source = OLD_ROOT / run_id / f'source{ext}'

        if storage.exists(key):
            already += 1
            continue
        if not source.exists():
            # Either evicted long ago, or a run that never completed its
            # upload. Not an error - just nothing to carry over.
            missing += 1
            continue

        print(f'  {"would move" if args.dry_run else "moving"} '
              f'{source.name} -> {key}  ({source.stat().st_size / 1e6:.1f} MB)')
        if not args.dry_run:
            with source.open('rb') as fh:
                storage.put_stream(key, iter(lambda: fh.read(1024 * 1024), b''))
            if not storage.exists(key):
                print(f'    ! copy of {key} could not be verified, leaving the '
                      f'original in place')
                continue
        moved += 1

    print(f'\n{moved} moved, {already} already in storage, '
          f'{missing} with no file on disk'
          + ('   (dry run - nothing changed)' if args.dry_run else ''))

    if args.delete_old and not args.dry_run:
        # Only after the copies are verified above, and only when asked.
        shutil.rmtree(OLD_ROOT, ignore_errors=True)
        print(f'removed {OLD_ROOT}')
    elif moved and not args.dry_run:
        print(f'\n{OLD_ROOT} still holds the originals. Re-run with '
              f'--delete-old once you are satisfied every run still opens.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
