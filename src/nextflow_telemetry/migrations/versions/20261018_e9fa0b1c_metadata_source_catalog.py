"""Metadata sources, assets, immutable snapshots and descriptor audit.

Revision ID: e9fa0b1c
Revises: d8e9fa0b

No source records, remote downloads or raw data are created by this migration.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e9fa0b1c"
down_revision = "d8e9fa0b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metadata_sources",
        sa.Column("source_id", sa.String(80), primary_key=True),
        sa.Column("descriptor", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "metadata_source_assets",
        sa.Column("source_id", sa.String(80), sa.ForeignKey("metadata_sources.source_id"), primary_key=True),
        sa.Column("asset_id", sa.String(80), primary_key=True),
        sa.Column("descriptor", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "metadata_source_snapshots",
        sa.Column("source_id", sa.String(80), primary_key=True),
        sa.Column("asset_id", sa.String(80), primary_key=True),
        sa.Column("version", sa.String(300), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("source_descriptor", postgresql.JSONB(), nullable=False),
        sa.Column("asset_descriptor", postgresql.JSONB(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_by", sa.String(300), nullable=False),
        sa.ForeignKeyConstraint(["source_id", "asset_id"],
                                ["metadata_source_assets.source_id", "metadata_source_assets.asset_id"]),
        sa.CheckConstraint("size_bytes >= 0", name="ck_metadata_snapshot_size"),
    )
    op.create_table(
        "metadata_source_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.String(80), sa.ForeignKey("metadata_sources.source_id"), nullable=False),
        sa.Column("asset_id", sa.String(80), nullable=True),
        sa.Column("actor", sa.String(300), nullable=False),
        sa.Column("before", postgresql.JSONB(), nullable=True),
        sa.Column("after", postgresql.JSONB(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_metadata_source_events_source", "metadata_source_events", ["source_id"])


def downgrade() -> None:
    # Explicit downgrade deletes the inventory; retained source blobs are external
    # and are intentionally not deleted by database migrations.
    op.drop_table("metadata_source_events")
    op.drop_table("metadata_source_snapshots")
    op.drop_table("metadata_source_assets")
    op.drop_table("metadata_sources")
