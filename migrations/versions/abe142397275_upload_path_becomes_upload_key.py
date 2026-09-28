"""upload_path becomes upload_key

Revision ID: abe142397275
Revises: ff24758a14f0
Create Date: 2026-09-15 01:48:12.851803

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'abe142397275'
down_revision: Union[str, None] = 'ff24758a14f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename runs.upload_path to runs.upload_key, keeping the values.

    Autogenerate proposed add_column + drop_column, which is NOT a rename - it
    would have silently discarded every existing upload reference. Alembic
    cannot tell a rename from a delete-and-create, so this one is written by
    hand. That is the whole reason its output is reviewed rather than applied.

    The column now holds a storage KEY rather than a filesystem path, because it
    has to resolve from a worker that shares no disk with the web process. The
    two are not interchangeable, so the old absolute paths are rewritten to keys
    as part of the same migration: `<run_id>/source<ext>`, which is where
    blobs.upload_key() puts them.
    """
    op.alter_column('runs', 'upload_path', new_column_name='upload_key')

    # Existing rows hold an absolute path like
    #   .../var/uploads/<run_id>/source.ifc
    # and the file itself still sits there. Rewrite the VALUE to the key it will
    # be looked up by; moving the file is a separate, backend-specific step (see
    # scripts/migrate_uploads.py), because on S3 there is no file to move.
    op.execute(
        """
        UPDATE runs
           SET upload_key = id || '/source' || ext
         WHERE upload_key IS NOT NULL
           AND upload_key <> ''
           AND upload_key NOT LIKE '%' || id || '/source%'
        """
    )


def downgrade() -> None:
    """Rename back. The values stay keys - they cannot become paths again
    without knowing which machine's disk was meant, and guessing would be worse
    than leaving them."""
    op.alter_column('runs', 'upload_key', new_column_name='upload_path')
