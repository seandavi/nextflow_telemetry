"""Drop the submissions -> metadata_source_snapshots foreign key (ADR-0014, #435).

Revision ID: 7c4d2e90
Revises: 6b3e9d21

The source catalog lives in a different database from the control plane, so
submissions keeps its three source_snapshot_* columns and the all-or-none check
but no longer references the catalog. e9fa0b1c still creates the (unused) catalog
tables wherever Alembic runs; this revision does not drop them, so `pg_main`
keeps its catalog. Downgrade restores the key, which fails if a submission
names a snapshot that is not in this database.
"""
from alembic import op

revision = "7c4d2e90"
down_revision = "6b3e9d21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("fk_submissions_source_snapshot", "submissions", type_="foreignkey")


def downgrade() -> None:
    op.create_foreign_key(
        "fk_submissions_source_snapshot", "submissions", "metadata_source_snapshots",
        ["source_snapshot_source_id", "source_snapshot_asset_id", "source_snapshot_version"],
        ["source_id", "asset_id", "version"],
    )
