import unittest
from datetime import date, datetime, time
from unittest.mock import patch

from flask import Flask

from beulah_pkg.booking_emails import send_booking_attendee_reminder
from beulah_pkg.booking_reminders import process_due_attendee_reminders
from beulah_pkg.models import Booker, Booking, RecurringSeries, db


class BookingAttendeeReminderTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SECRET_KEY='test-reminder-secret',
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            COUNSELOR_NAME='Test Counsellor',
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        self.tables = [
            Booker.__table__,
            RecurringSeries.__table__,
            Booking.__table__,
        ]
        db.metadata.create_all(bind=db.engine, tables=self.tables)

    def tearDown(self):
        db.session.remove()
        db.metadata.drop_all(bind=db.engine, tables=list(reversed(self.tables)))
        self.context.pop()

    def _booking(self, status='confirmed', start=time(10, 0)):
        booker = Booker(
            booker_name='Reminder Client',
            booker_email='client@example.com',
        )
        db.session.add(booker)
        db.session.flush()
        booking = Booking(
            booker_id=booker.booker_id,
            booking_date=date(2030, 1, 7),
            booking_start_time=start,
            booking_end_time=time(start.hour + 1, start.minute),
            booking_session_type='video',
            booking_session_format='single',
            booking_status=status,
        )
        db.session.add(booking)
        db.session.commit()
        return booking

    @patch('beulah_pkg.booking_reminders.send_booking_attendee_reminder')
    def test_reminder_sent_when_confirmed_session_is_one_hour_away(self, send):
        booking = self._booking()

        sent = process_due_attendee_reminders(
            self.app,
            now=datetime(2030, 1, 7, 9, 0),
        )

        self.assertEqual(sent, 1)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0].booking_id, booking.booking_id)
        self.assertEqual(
            send.call_args.kwargs['counselor_name'],
            'Test Counsellor',
        )
        db.session.refresh(booking)
        self.assertEqual(
            booking.booking_attendee_reminder_sent_for_start,
            datetime(2030, 1, 7, 10, 0),
        )

    @patch('beulah_pkg.booking_reminders.send_booking_attendee_reminder')
    def test_cancelled_session_does_not_receive_reminder(self, send):
        self._booking(status='cancelled')

        sent = process_due_attendee_reminders(
            self.app,
            now=datetime(2030, 1, 7, 9, 0),
        )

        self.assertEqual(sent, 0)
        send.assert_not_called()

    @patch('beulah_pkg.booking_reminders.send_booking_attendee_reminder')
    def test_repeated_runs_do_not_send_duplicate_reminders(self, send):
        self._booking()
        now = datetime(2030, 1, 7, 9, 0)

        first_count = process_due_attendee_reminders(self.app, now=now)
        second_count = process_due_attendee_reminders(self.app, now=now)

        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 0)
        send.assert_called_once()

    @patch('beulah_pkg.booking_reminders.send_booking_attendee_reminder')
    def test_rescheduled_session_uses_new_time_and_not_old_time(self, send):
        booking = self._booking()
        booking.booking_start_time = time(12, 0)
        booking.booking_end_time = time(13, 0)
        booking.booking_status = 'rescheduled'
        db.session.commit()

        old_due_count = process_due_attendee_reminders(
            self.app,
            now=datetime(2030, 1, 7, 9, 0),
        )
        new_due_count = process_due_attendee_reminders(
            self.app,
            now=datetime(2030, 1, 7, 11, 0),
        )

        self.assertEqual(old_due_count, 0)
        self.assertEqual(new_due_count, 1)
        send.assert_called_once()
        db.session.refresh(booking)
        self.assertEqual(
            booking.booking_attendee_reminder_sent_for_start,
            datetime(2030, 1, 7, 12, 0),
        )

    @patch('beulah_pkg.booking_reminders.send_booking_attendee_reminder')
    def test_reminder_is_not_sent_too_early(self, send):
        self._booking()

        sent = process_due_attendee_reminders(
            self.app,
            now=datetime(2030, 1, 7, 8, 55),
        )

        self.assertEqual(sent, 0)
        send.assert_not_called()

    @patch('beulah_pkg.booking_reminders.send_booking_attendee_reminder')
    def test_recently_due_reminder_is_sent_when_cron_runs_late(self, send):
        self._booking()

        sent = process_due_attendee_reminders(
            self.app,
            now=datetime(2030, 1, 7, 9, 5),
        )

        self.assertEqual(sent, 1)
        send.assert_called_once()

    @patch('beulah_pkg.booking_reminders.send_booking_attendee_reminder')
    def test_marked_session_is_not_sent_again(self, send):
        booking = self._booking()
        booking.booking_attendee_reminder_sent_for_start = datetime(
            2030, 1, 7, 10, 0
        )
        db.session.commit()

        sent = process_due_attendee_reminders(
            self.app,
            now=datetime(2030, 1, 7, 9, 0),
        )

        self.assertEqual(sent, 0)
        send.assert_not_called()

    @patch('beulah_pkg.booking_emails.mail.send')
    def test_reminder_email_contains_time_date_and_available_counselor(self, send):
        booking = self._booking()

        send_booking_attendee_reminder(
            booking,
            counselor_name='Test Counsellor',
        )

        message = send.call_args.args[0]
        self.assertEqual(message.recipients, ['client@example.com'])
        self.assertIn('starts in about 1 hour', message.subject)
        self.assertIn('Monday, January 7th, 2030', message.body)
        self.assertIn('10:00 - 11:00 (WAT)', message.body)
        self.assertIn('Counsellor: Test Counsellor', message.body)


if __name__ == '__main__':
    unittest.main()
