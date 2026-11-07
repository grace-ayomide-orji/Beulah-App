"""repair missing counselling tables

Revision ID: 20261104_0011
Revises: 20261103_0010
Create Date: 2026-11-04 16:00:00.000000

Some databases were stamped at the migration head after being copied without
the counselling tables from the original baseline. This migration repairs that
partial schema without replacing or clearing tables that already exist.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '20261104_0011'
down_revision = '20261103_0010'
branch_labels = None
depends_on = None


def _has_table(connection, table_name):
    return table_name in inspect(connection).get_table_names()


def _column_names(connection, table_name):
    if not _has_table(connection, table_name):
        return set()
    return {
        column['name']
        for column in inspect(connection).get_columns(table_name)
    }


def _has_index_for_columns(connection, table_name, columns, unique=None):
    expected = tuple(columns)
    inspector = inspect(connection)

    for index in inspector.get_indexes(table_name):
        if tuple(index.get('column_names') or ()) != expected:
            continue
        if unique is None or bool(index.get('unique')) == unique:
            return True

    for constraint in inspector.get_unique_constraints(table_name):
        if tuple(constraint.get('column_names') or ()) == expected:
            return unique is not False

    return False


def _create_index_if_missing(
    connection,
    name,
    table_name,
    columns,
    unique=False,
):
    if not _has_index_for_columns(
        connection,
        table_name,
        columns,
        unique=unique,
    ):
        op.create_index(name, table_name, columns, unique=unique)


def upgrade():
    connection = op.get_bind()

    if not _has_table(connection, 'blocked_dates'):
        op.create_table(
            'blocked_dates',
            sa.Column('bd_id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('bd_date', sa.Date(), nullable=False),
            sa.Column('bd_reason', sa.String(length=255), nullable=True),
            sa.PrimaryKeyConstraint('bd_id'),
        )
    _create_index_if_missing(
        connection,
        'ix_blocked_dates_bd_date',
        'blocked_dates',
        ['bd_date'],
        unique=True,
    )

    if not _has_table(connection, 'bookers'):
        op.create_table(
            'bookers',
            sa.Column('booker_id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('booker_name', sa.String(length=100), nullable=False),
            sa.Column('booker_email', sa.String(length=120), nullable=False),
            sa.Column('booker_phone', sa.String(length=30), nullable=True),
            sa.Column('booker_date_added', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('booker_id'),
        )
    _create_index_if_missing(
        connection,
        'ix_bookers_booker_email',
        'bookers',
        ['booker_email'],
    )

    if not _has_table(connection, 'recurring_series'):
        op.create_table(
            'recurring_series',
            sa.Column('series_id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column(
                'series_frequency',
                sa.Enum('weekly', 'biweekly', 'monthly'),
                nullable=False,
            ),
            sa.Column('series_start_date', sa.Date(), nullable=False),
            sa.Column('series_end_date', sa.Date(), nullable=False),
            sa.Column('series_date_added', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('series_id'),
        )

    if not _has_table(connection, 'recurring_unavailability'):
        op.create_table(
            'recurring_unavailability',
            sa.Column('ru_id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('ru_day_of_week', sa.Integer(), nullable=False),
            sa.Column('ru_start_time', sa.Time(), nullable=False),
            sa.Column('ru_end_time', sa.Time(), nullable=False),
            sa.Column('ru_reason', sa.String(length=255), nullable=True),
            sa.Column('ru_date_added', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('ru_id'),
        )

    if not _has_table(connection, 'working_hours'):
        op.create_table(
            'working_hours',
            sa.Column('wh_id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('wh_day_of_week', sa.Integer(), nullable=False),
            sa.Column('wh_start_time', sa.Time(), nullable=False),
            sa.Column('wh_end_time', sa.Time(), nullable=False),
            sa.Column('wh_is_active', sa.Boolean(), nullable=True),
            sa.Column('wh_updated_date', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('wh_id'),
            sa.UniqueConstraint(
                'wh_day_of_week',
                name='uq_working_hours_wh_day_of_week',
            ),
        )

    if not _has_table(connection, 'bookings'):
        op.create_table(
            'bookings',
            sa.Column('booking_id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('booker_id', sa.Integer(), nullable=False),
            sa.Column('series_id', sa.Integer(), nullable=True),
            sa.Column('booking_date', sa.Date(), nullable=False),
            sa.Column('booking_start_time', sa.Time(), nullable=False),
            sa.Column('booking_end_time', sa.Time(), nullable=False),
            sa.Column(
                'booking_session_type',
                sa.Enum('video', 'audio'),
                nullable=False,
            ),
            sa.Column(
                'booking_session_format',
                sa.Enum('single', 'recurring'),
                nullable=False,
            ),
            sa.Column(
                'booking_status',
                sa.Enum(
                    'pending',
                    'confirmed',
                    'cancelled',
                    'completed',
                    'no_show',
                    'rescheduled',
                ),
                nullable=False,
            ),
            sa.Column('booking_manage_token', sa.String(length=36), nullable=False),
            sa.Column('booking_meet_link', sa.String(length=255), nullable=True),
            sa.Column(
                'booking_calendar_event_id',
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column('booking_reason', sa.Text(), nullable=True),
            sa.Column('booking_notes', sa.Text(), nullable=True),
            sa.Column('booking_date_added', sa.DateTime(), nullable=True),
            sa.Column('booking_updated_date', sa.DateTime(), nullable=True),
            sa.Column(
                'booking_attendee_reminder_sent_for_start',
                sa.DateTime(),
                nullable=True,
            ),
            sa.ForeignKeyConstraint(['booker_id'], ['bookers.booker_id']),
            sa.ForeignKeyConstraint(['series_id'], ['recurring_series.series_id']),
            sa.PrimaryKeyConstraint('booking_id'),
        )
    elif 'booking_attendee_reminder_sent_for_start' not in _column_names(
        connection,
        'bookings',
    ):
        with op.batch_alter_table('bookings') as batch_op:
            batch_op.add_column(sa.Column(
                'booking_attendee_reminder_sent_for_start',
                sa.DateTime(),
                nullable=True,
            ))

    _create_index_if_missing(
        connection,
        'ix_bookings_booker_id',
        'bookings',
        ['booker_id'],
    )
    _create_index_if_missing(
        connection,
        'ix_bookings_series_id',
        'bookings',
        ['series_id'],
    )
    _create_index_if_missing(
        connection,
        'ix_bookings_booking_date',
        'bookings',
        ['booking_date'],
    )
    _create_index_if_missing(
        connection,
        'ix_bookings_booking_manage_token',
        'bookings',
        ['booking_manage_token'],
        unique=True,
    )

    if not _has_table(connection, 'booking_email_verifications'):
        op.create_table(
            'booking_email_verifications',
            sa.Column(
                'verification_id',
                sa.Integer(),
                autoincrement=True,
                nullable=False,
            ),
            sa.Column('verification_token', sa.String(length=64), nullable=False),
            sa.Column('booking_id', sa.Integer(), nullable=False),
            sa.Column('verification_code_hash', sa.String(length=64), nullable=False),
            sa.Column('verification_expires_at', sa.DateTime(), nullable=False),
            sa.Column('verification_resend_at', sa.DateTime(), nullable=False),
            sa.Column(
                'verification_attempts',
                sa.Integer(),
                nullable=False,
                server_default='0',
            ),
            sa.Column(
                'verification_send_count',
                sa.Integer(),
                nullable=False,
                server_default='1',
            ),
            sa.Column('verification_verified_at', sa.DateTime(), nullable=True),
            sa.Column('verification_date_added', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ['booking_id'],
                ['bookings.booking_id'],
                ondelete='CASCADE',
            ),
            sa.PrimaryKeyConstraint('verification_id'),
            sa.UniqueConstraint(
                'booking_id',
                name='uq_booking_email_verifications_booking_id',
            ),
            sa.UniqueConstraint(
                'verification_token',
                name='uq_booking_email_verifications_token',
            ),
        )
    _create_index_if_missing(
        connection,
        'ix_booking_email_verifications_expires_at',
        'booking_email_verifications',
        ['verification_expires_at'],
    )


def downgrade():
    # This is a schema-repair migration. Automatically dropping tables here
    # could remove real counselling data from databases where they pre-existed.
    pass
