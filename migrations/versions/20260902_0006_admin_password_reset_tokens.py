"""add admin password reset tokens

Revision ID: 20260902_0006
Revises: 20260831_0005
Create Date: 2026-09-02 20:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = '20260902_0006'
down_revision = '20260831_0005'
branch_labels = None
depends_on = None


def _has_table(connection, table_name):
    if connection.dialect.name == 'mysql':
        return bool(connection.execute(text("""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
        """), {'table_name': table_name}).scalar())

    return table_name in inspect(connection).get_table_names()


def upgrade():
    connection = op.get_bind()
    if _has_table(connection, 'admin_password_reset_tokens'):
        return

    op.create_table(
        'admin_password_reset_tokens',
        sa.Column('token_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('admin_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('consumed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('user_agent', sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(['admin_id'], ['admin.admin_id']),
        sa.PrimaryKeyConstraint('token_id'),
    )
    op.create_index('ix_admin_password_reset_tokens_admin_id', 'admin_password_reset_tokens', ['admin_id'])
    op.create_index('ix_admin_password_reset_tokens_consumed_at', 'admin_password_reset_tokens', ['consumed_at'])
    op.create_index('ix_admin_password_reset_tokens_expires_at', 'admin_password_reset_tokens', ['expires_at'])
    op.create_index('ix_admin_password_reset_tokens_token_hash', 'admin_password_reset_tokens', ['token_hash'], unique=True)


def downgrade():
    connection = op.get_bind()
    if not _has_table(connection, 'admin_password_reset_tokens'):
        return

    op.drop_table('admin_password_reset_tokens')
