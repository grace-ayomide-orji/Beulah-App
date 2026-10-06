"""remove blog tags field

Revision ID: 20260831_0005
Revises: 20260831_0004
Create Date: 2026-08-31 19:20:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = '20260831_0005'
down_revision = '20260831_0004'
branch_labels = None
depends_on = None


def _has_resource_tags(connection):
    if connection.dialect.name == 'mysql':
        return bool(connection.execute(text("""
            SELECT COUNT(*)
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'resources'
              AND column_name = 'resource_tags'
        """)).scalar())

    inspector = inspect(connection)
    return any(column['name'] == 'resource_tags' for column in inspector.get_columns('resources'))


def upgrade():
    connection = op.get_bind()
    if not _has_resource_tags(connection):
        return

    if connection.dialect.name == 'mysql':
        op.execute("ALTER TABLE resources DROP COLUMN resource_tags")
        return

    with op.batch_alter_table('resources', schema=None) as batch_op:
        batch_op.drop_column('resource_tags')


def downgrade():
    connection = op.get_bind()
    if _has_resource_tags(connection):
        return

    if connection.dialect.name == 'mysql':
        op.execute("ALTER TABLE resources ADD COLUMN resource_tags VARCHAR(255) NULL")
        return

    with op.batch_alter_table('resources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('resource_tags', sa.String(length=255), nullable=True))
