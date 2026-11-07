import logging
import os
from datetime import datetime, timedelta

import pytz

from beulah_pkg.booking_emails import send_booking_attendee_reminder
from beulah_pkg.models import Booking, db


TIMEZONE = pytz.timezone('Africa/Lagos')
ACTIVE_REMINDER_STATUSES = ('confirmed', 'rescheduled')
DEFAULT_LOOKBACK_MINUTES = 10
logger = logging.getLogger(__name__)


def _lagos_naive(value=None):
    if value is None:
        value = datetime.now(TIMEZONE)
    elif value.tzinfo is not None:
        value = value.astimezone(TIMEZONE)
    return value.replace(tzinfo=None)


def _booking_start(booking):
    return datetime.combine(booking.booking_date, booking.booking_start_time)


def _is_due(booking, now, lookback_minutes):
    session_start = _booking_start(booking)
    reminder_due = session_start - timedelta(hours=1)
    window_start = now - timedelta(minutes=lookback_minutes)
    return window_start <= reminder_due <= now < session_start


def process_due_attendee_reminders(app, now=None, lookback_minutes=None):
    """Send due one-hour attendee reminders from a cron-safe process."""
    now = _lagos_naive(now)
    if lookback_minutes is None:
        raw_lookback = app.config.get(
            'COUNSELLING_REMINDER_LOOKBACK_MINUTES',
            os.getenv(
                'COUNSELLING_REMINDER_LOOKBACK_MINUTES',
                str(DEFAULT_LOOKBACK_MINUTES),
            ),
        )
        try:
            lookback_minutes = max(1, int(raw_lookback))
        except (TypeError, ValueError):
            lookback_minutes = DEFAULT_LOOKBACK_MINUTES

    earliest_start = now + timedelta(hours=1, minutes=-lookback_minutes)
    latest_start = now + timedelta(hours=1)
    counselor_name = (
        app.config.get('COUNSELOR_NAME')
        or os.getenv('COUNSELOR_NAME')
        or None
    )
    sent_count = 0

    with app.app_context():
        candidate_ids = [row[0] for row in db.session.query(Booking.booking_id).filter(
            Booking.booking_status.in_(ACTIVE_REMINDER_STATUSES),
            Booking.booking_date >= earliest_start.date(),
            Booking.booking_date <= latest_start.date(),
        ).all()]

        for booking_id in candidate_ids:
            try:
                booking = db.session.query(Booking).filter(
                    Booking.booking_id == booking_id,
                ).with_for_update().one_or_none()

                if not booking or booking.booking_status not in ACTIVE_REMINDER_STATUSES:
                    db.session.rollback()
                    continue

                session_start = _booking_start(booking)
                if (
                    booking.booking_attendee_reminder_sent_for_start == session_start
                    or not _is_due(booking, now, lookback_minutes)
                ):
                    db.session.rollback()
                    continue

                send_booking_attendee_reminder(
                    booking,
                    counselor_name=counselor_name,
                )
                booking.booking_attendee_reminder_sent_for_start = session_start
                db.session.commit()
                sent_count += 1
                logger.info(
                    'Sent attendee reminder for booking %s scheduled at %s WAT.',
                    booking.booking_id,
                    session_start.isoformat(),
                )
            except Exception:
                db.session.rollback()
                logger.exception(
                    'Failed to process attendee reminder for booking %s.',
                    booking_id,
                )

    return sent_count
