"""add counselling booking email verification

Revision ID: 20261024_0009
Revises: 20261006_0008
Create Date: 2026-10-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '20261024_0009'
down_revision = '20261006_0008'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if 'booking_email_verifications' in inspect(connection).get_table_names():
        return

    op.create_table(
        'booking_email_verifications',
        sa.Column('verification_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('verification_token', sa.String(length=64), nullable=False),
        sa.Column('booking_id', sa.Integer(), nullable=False),
        sa.Column('verification_code_hash', sa.String(length=64), nullable=False),
        sa.Column('verification_expires_at', sa.DateTime(), nullable=False),
        sa.Column('verification_resend_at', sa.DateTime(), nullable=False),
        sa.Column('verification_attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('verification_send_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('verification_verified_at', sa.DateTime(), nullable=True),
        sa.Column('verification_date_added', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['booking_id'],
            ['bookings.booking_id'],
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('verification_id'),
        sa.UniqueConstraint('booking_id'),
        sa.UniqueConstraint('verification_token'),
    )
    op.create_index(
        'ix_booking_email_verifications_expires_at',
        'booking_email_verifications',
        ['verification_expires_at'],
        unique=False,
    )


def downgrade():
    connection = op.get_bind()
    if 'booking_email_verifications' not in inspect(connection).get_table_names():
        return
    op.drop_table('booking_email_verifications')
