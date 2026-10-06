"""create runtime security and limiter tables

Revision ID: 20260903_0007
Revises: 20260902_0006
Create Date: 2026-09-03 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = '20260903_0007'
down_revision = '20260902_0006'
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


def _has_index(connection, table_name, index_name):
    if connection.dialect.name == 'mysql':
        return bool(connection.execute(text("""
            SELECT COUNT(*)
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
              AND index_name = :index_name
        """), {'table_name': table_name, 'index_name': index_name}).scalar())

    return index_name in {index['name'] for index in inspect(connection).get_indexes(table_name)}


def _create_index_if_missing(connection, name, table, columns, unique=False):
    if not _has_index(connection, table, name):
        op.create_index(name, table, columns, unique=unique)


def upgrade():
    connection = op.get_bind()

    if not _has_table(connection, 'rate_limit_counters'):
        op.create_table(
            'rate_limit_counters',
            sa.Column('key_hash', sa.String(length=64), nullable=False),
            sa.Column('counter_key', sa.String(length=255), nullable=False),
            sa.Column('amount', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('key_hash'),
        )
    _create_index_if_missing(connection, 'ix_rate_limit_counters_expires_at', 'rate_limit_counters', ['expires_at'])

    if not _has_table(connection, 'admin_security_states'):
        op.create_table(
            'admin_security_states',
            sa.Column('state_id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('admin_id', sa.Integer(), nullable=False),
            sa.Column('failed_login_attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('locked_until', sa.DateTime(), nullable=True),
            sa.Column('last_failed_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['admin_id'], ['admin.admin_id']),
            sa.PrimaryKeyConstraint('state_id'),
        )
    _create_index_if_missing(connection, 'ix_admin_security_states_admin_id', 'admin_security_states', ['admin_id'], unique=True)

    if not _has_table(connection, 'admin_mfa_challenges'):
        op.create_table(
            'admin_mfa_challenges',
            sa.Column('challenge_id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('admin_id', sa.Integer(), nullable=False),
            sa.Column('code_hash', sa.String(length=255), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('consumed_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['admin_id'], ['admin.admin_id']),
            sa.PrimaryKeyConstraint('challenge_id'),
        )
    _create_index_if_missing(connection, 'ix_admin_mfa_challenges_admin_id', 'admin_mfa_challenges', ['admin_id'])
    _create_index_if_missing(connection, 'ix_admin_mfa_challenges_expires_at', 'admin_mfa_challenges', ['expires_at'])

    if not _has_table(connection, 'admin_session_tokens'):
        op.create_table(
            'admin_session_tokens',
            sa.Column('token_id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('admin_id', sa.Integer(), nullable=False),
            sa.Column('session_token_hash', sa.String(length=64), nullable=False),
            sa.Column('rotate_after', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('last_seen_at', sa.DateTime(), nullable=True),
            sa.Column('ip_address', sa.String(length=45), nullable=True),
            sa.Column('user_agent', sa.String(length=255), nullable=True),
            sa.ForeignKeyConstraint(['admin_id'], ['admin.admin_id']),
            sa.PrimaryKeyConstraint('token_id'),
        )
    _create_index_if_missing(connection, 'ix_admin_session_tokens_admin_id', 'admin_session_tokens', ['admin_id'])
    _create_index_if_missing(connection, 'ix_admin_session_tokens_expires_at', 'admin_session_tokens', ['expires_at'])
    _create_index_if_missing(connection, 'ix_admin_session_tokens_revoked_at', 'admin_session_tokens', ['revoked_at'])
    _create_index_if_missing(connection, 'ix_admin_session_tokens_rotate_after', 'admin_session_tokens', ['rotate_after'])
    _create_index_if_missing(connection, 'ix_admin_session_tokens_session_token_hash', 'admin_session_tokens', ['session_token_hash'], unique=True)

    if not _has_table(connection, 'admin_audit_logs'):
        op.create_table(
            'admin_audit_logs',
            sa.Column('audit_id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('admin_id', sa.Integer(), nullable=True),
            sa.Column('action', sa.String(length=80), nullable=False),
            sa.Column('details', sa.Text(), nullable=True),
            sa.Column('ip_address', sa.String(length=45), nullable=True),
            sa.Column('user_agent', sa.String(length=255), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['admin_id'], ['admin.admin_id']),
            sa.PrimaryKeyConstraint('audit_id'),
        )
    _create_index_if_missing(connection, 'ix_admin_audit_logs_action', 'admin_audit_logs', ['action'])
    _create_index_if_missing(connection, 'ix_admin_audit_logs_admin_id', 'admin_audit_logs', ['admin_id'])
    _create_index_if_missing(connection, 'ix_admin_audit_logs_created_at', 'admin_audit_logs', ['created_at'])


def downgrade():
    connection = op.get_bind()
    for table_name in (
        'admin_audit_logs',
        'admin_session_tokens',
        'admin_mfa_challenges',
        'admin_security_states',
        'rate_limit_counters',
    ):
        if _has_table(connection, table_name):
            op.drop_table(table_name)
