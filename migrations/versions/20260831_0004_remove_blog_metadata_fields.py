"""remove blog author excerpt and seo fields

Revision ID: 20260831_0004
Revises: 20260830_0003
Create Date: 2026-08-31 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = '20260831_0004'
down_revision = '20260830_0003'
branch_labels = None
depends_on = None


REMOVED_COLUMNS = (
    'resource_author',
    'resource_excerpt',
    'resource_seo_title',
    'resource_seo_description',
)


RESTORE_COLUMNS = {
    'resource_excerpt': "ADD COLUMN resource_excerpt TEXT NULL",
    'resource_author': "ADD COLUMN resource_author VARCHAR(120) NULL",
    'resource_seo_title': "ADD COLUMN resource_seo_title VARCHAR(255) NULL",
    'resource_seo_description': "ADD COLUMN resource_seo_description VARCHAR(300) NULL",
}


def _existing_columns(connection):
    if connection.dialect.name == 'mysql':
        return {
            row[0]
            for row in connection.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name = 'resources'
            """))
        }

    inspector = inspect(connection)
    return {column['name'] for column in inspector.get_columns('resources')}


def upgrade():
    connection = op.get_bind()
    existing_columns = _existing_columns(connection)

    if connection.dialect.name == 'mysql':
        for column_name in REMOVED_COLUMNS:
            if column_name in existing_columns:
                op.execute(f"ALTER TABLE resources DROP COLUMN {column_name}")
        return

    with op.batch_alter_table('resources', schema=None) as batch_op:
        for column_name in REMOVED_COLUMNS:
            if column_name in existing_columns:
                batch_op.drop_column(column_name)


def downgrade():
    connection = op.get_bind()
    existing_columns = _existing_columns(connection)

    if connection.dialect.name == 'mysql':
        for column_name, alter_sql in RESTORE_COLUMNS.items():
            if column_name not in existing_columns:
                op.execute(f"ALTER TABLE resources {alter_sql}")
        return

    with op.batch_alter_table('resources', schema=None) as batch_op:
        if 'resource_excerpt' not in existing_columns:
            batch_op.add_column(sa.Column('resource_excerpt', sa.Text(), nullable=True))
        if 'resource_author' not in existing_columns:
            batch_op.add_column(sa.Column('resource_author', sa.String(length=120), nullable=True))
        if 'resource_seo_title' not in existing_columns:
            batch_op.add_column(sa.Column('resource_seo_title', sa.String(length=255), nullable=True))
        if 'resource_seo_description' not in existing_columns:
            batch_op.add_column(sa.Column('resource_seo_description', sa.String(length=300), nullable=True))
