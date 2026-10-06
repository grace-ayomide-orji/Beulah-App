"""add suspended resource status

Revision ID: 20260830_0003
Revises: 20260819_0002
Create Date: 2026-08-30 20:45:00.000000

"""
from alembic import op


revision = '20260830_0003'
down_revision = '20260819_0002'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE resources "
        "MODIFY resource_status ENUM('draft','published','suspended','unpublished') "
        "NOT NULL DEFAULT 'draft'"
    )
    op.execute(
        "UPDATE resources "
        "SET resource_status = 'published' "
        "WHERE resource_type IN ('text','audio','slide') "
        "AND resource_is_deleted = 0 "
        "AND resource_status IN ('draft','unpublished')"
    )


def downgrade():
    op.execute(
        "UPDATE resources "
        "SET resource_status = 'unpublished' "
        "WHERE resource_status = 'suspended'"
    )
    op.execute(
        "ALTER TABLE resources "
        "MODIFY resource_status ENUM('draft','published','unpublished') "
        "NOT NULL DEFAULT 'draft'"
    )
