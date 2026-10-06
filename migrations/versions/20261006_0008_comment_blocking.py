"""add blocked flag to comments

Revision ID: 20261006_0008
Revises: 20260903_0007
Create Date: 2026-10-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = '20261006_0008'
down_revision = '20260903_0007'
branch_labels = None
depends_on = None


def _has_column(connection, table_name, column_name):
    if connection.dialect.name == 'mysql':
        return bool(connection.execute(text("""
            SELECT COUNT(*)
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
              AND column_name = :column_name
        """), {'table_name': table_name, 'column_name': column_name}).scalar())

    return column_name in {
        column['name']
        for column in inspect(connection).get_columns(table_name)
    }


def upgrade():
    connection = op.get_bind()
    if not _has_column(connection, 'comments', 'comment_is_blocked'):
        op.add_column(
            'comments',
            sa.Column('comment_is_blocked', sa.Boolean(), nullable=False, server_default=sa.false())
        )

    op.execute("UPDATE comments SET comment_is_blocked = 0 WHERE comment_is_blocked IS NULL")


def downgrade():
    connection = op.get_bind()
    if _has_column(connection, 'comments', 'comment_is_blocked'):
        op.drop_column('comments', 'comment_is_blocked')
