"""track attendee counselling reminder delivery

Revision ID: 20261103_0010
Revises: 20261024_0009
Create Date: 2026-11-03 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '20261103_0010'
down_revision = '20261024_0009'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    columns = {
        column['name']
        for column in inspect(connection).get_columns('bookings')
    }
    if 'booking_attendee_reminder_sent_for_start' in columns:
        return

    with op.batch_alter_table('bookings') as batch_op:
        batch_op.add_column(sa.Column(
            'booking_attendee_reminder_sent_for_start',
            sa.DateTime(),
            nullable=True,
        ))


def downgrade():
    connection = op.get_bind()
    columns = {
        column['name']
        for column in inspect(connection).get_columns('bookings')
    }
    if 'booking_attendee_reminder_sent_for_start' not in columns:
        return

    with op.batch_alter_table('bookings') as batch_op:
        batch_op.drop_column('booking_attendee_reminder_sent_for_start')
