import os
import unittest
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

# This module exercises destructive schema setup/teardown. Configure the
# application before importing it so those operations can only target the
# disposable in-memory test database.
os.environ['FLASK_ENV'] = 'testing'
os.environ['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
os.environ['RATELIMIT_STORAGE_URI'] = 'memory://'

from email_validator import EmailUndeliverableError
from flask import Flask

from beulah_pkg import app as beulah_app, limiter
from beulah_pkg.availability_helpers import is_slot_available
from beulah_pkg.booking_verification import (
    delete_pending_verification,
    hash_verification_code,
    suggest_email_correction,
    verification_code_matches,
)
from beulah_pkg.models import (
    BlockedDate,
    Booker,
    Booking,
    BookingEmailVerification,
    RecurringSeries,
    RecurringUnavailability,
    WorkingHours,
    db,
)


class BookingEmailVerificationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SECRET_KEY='test-booking-verification-secret',
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.metadata.create_all(
            bind=db.engine,
            tables=[
                Booker.__table__,
                RecurringSeries.__table__,
                Booking.__table__,
                BookingEmailVerification.__table__,
                WorkingHours.__table__,
                BlockedDate.__table__,
                RecurringUnavailability.__table__,
            ],
        )

    def tearDown(self):
        db.session.remove()
        db.metadata.drop_all(
            bind=db.engine,
            tables=[
                BookingEmailVerification.__table__,
                Booking.__table__,
                RecurringSeries.__table__,
                Booker.__table__,
                RecurringUnavailability.__table__,
                BlockedDate.__table__,
                WorkingHours.__table__,
            ],
        )
        self.context.pop()

    def _pending_booking(self, expires_at):
        target_date = date(2030, 1, 7)
        day_of_week = (target_date.weekday() + 1) % 7
        db.session.add(WorkingHours(
            wh_day_of_week=day_of_week,
            wh_start_time=time(9, 0),
            wh_end_time=time(12, 0),
            wh_is_active=True,
        ))
        booker = Booker(
            booker_name='Test User',
            booker_email='test@example.com',
        )
        db.session.add(booker)
        db.session.flush()
        booking = Booking(
            booker_id=booker.booker_id,
            booking_date=target_date,
            booking_start_time=time(10, 0),
            booking_end_time=time(11, 0),
            booking_session_type='video',
            booking_session_format='single',
            booking_status='pending',
        )
        db.session.add(booking)
        db.session.flush()
        token = 'verification-token'
        verification = BookingEmailVerification(
            verification_token=token,
            booking_id=booking.booking_id,
            verification_code_hash=hash_verification_code(token, '123456'),
            verification_expires_at=expires_at,
            verification_resend_at=datetime.utcnow(),
        )
        db.session.add(verification)
        db.session.commit()
        return target_date, booking, verification

    def test_common_gmail_typo_is_suggested(self):
        self.assertEqual(
            suggest_email_correction('person@gamil.com'),
            'person@gmail.com',
        )
        self.assertIsNone(suggest_email_correction('person@gmail.com'))

    def test_code_hash_is_token_bound_and_constant_time_checked(self):
        target_date, booking, verification = self._pending_booking(
            datetime.utcnow() + timedelta(minutes=10)
        )
        self.assertTrue(verification_code_matches(verification, '123456'))
        self.assertFalse(verification_code_matches(verification, '654321'))
        self.assertNotEqual(
            verification.verification_code_hash,
            hash_verification_code('different-token', '123456'),
        )

    def test_active_hold_blocks_slot_and_expired_hold_releases_it(self):
        target_date, booking, verification = self._pending_booking(
            datetime.utcnow() + timedelta(minutes=10)
        )
        self.assertFalse(is_slot_available(target_date, time(10, 0)))

        verification.verification_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertTrue(is_slot_available(target_date, time(10, 0)))

    def test_releasing_verification_removes_pending_booking(self):
        target_date, booking, verification = self._pending_booking(
            datetime.utcnow() + timedelta(minutes=10)
        )
        booking_id = booking.booking_id
        verification_id = verification.verification_id

        delete_pending_verification(verification, commit=True)

        self.assertIsNone(db.session.get(Booking, booking_id))
        self.assertIsNone(
            db.session.get(BookingEmailVerification, verification_id)
        )


class BookingEmailVerificationFlowTests(unittest.TestCase):
    def setUp(self):
        beulah_app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            RATELIMIT_ENABLED=False,
        )
        self.context = beulah_app.app_context()
        self.context.push()
        if db.engine.dialect.name != 'sqlite':
            self.context.pop()
            self.fail(
                'Refusing to run booking schema tests outside SQLite.'
            )
        self.limiter_was_enabled = limiter.enabled
        limiter.enabled = False
        self.tables = [
            Booker.__table__,
            RecurringSeries.__table__,
            Booking.__table__,
            BookingEmailVerification.__table__,
            WorkingHours.__table__,
            BlockedDate.__table__,
            RecurringUnavailability.__table__,
        ]
        db.metadata.create_all(bind=db.engine, tables=self.tables)
        self.target_date = date.today() + timedelta(days=14)
        db.session.add(WorkingHours(
            wh_day_of_week=(self.target_date.weekday() + 1) % 7,
            wh_start_time=time(9, 0),
            wh_end_time=time(12, 0),
            wh_is_active=True,
        ))
        db.session.commit()
        self.client = beulah_app.test_client()

    def tearDown(self):
        db.session.remove()
        db.metadata.drop_all(bind=db.engine, tables=list(reversed(self.tables)))
        limiter.enabled = self.limiter_was_enabled
        self.context.pop()

    @patch('beulah_pkg.counselling_route.send_counselor_notification')
    @patch('beulah_pkg.counselling_route.send_booking_confirmation')
    @patch('beulah_pkg.counselling_route.create_calendar_event')
    @patch('beulah_pkg.counselling_route.send_booking_verification_code')
    @patch('beulah_pkg.counselling_route.verify_turnstile', return_value=True)
    def test_calendar_and_confirmation_wait_for_successful_verification(
        self,
        verify_turnstile,
        send_code,
        create_calendar,
        send_confirmation,
        send_counselor,
    ):
        create_calendar.return_value = {
            'event_id': 'calendar-event-1',
            'meet_link': 'https://meet.google.com/test-link',
        }
        payload = {
            'name': 'Grace Test',
            'email': 'grace@example.com',
            'phone': '',
            'session_type': 'video',
            'date': self.target_date.isoformat(),
            'start_time': '10:00',
            'reason': 'Test booking',
            'is_recurring': False,
            'frequency': 'weekly',
            'end_date': '',
            'website': '',
            'turnstile_token': 'test-token',
        }

        submit_response = self.client.post('/book/submit/', json=payload)

        self.assertEqual(submit_response.status_code, 202)
        submit_data = submit_response.get_json()
        self.assertTrue(submit_data['verification_required'])
        booking = db.session.query(Booking).one()
        self.assertEqual(booking.booking_status, 'pending')
        create_calendar.assert_not_called()
        send_confirmation.assert_not_called()

        sent_code = send_code.call_args.args[2]
        verify_response = self.client.post('/book/verify-email/', json={
            'verification_token': submit_data['verification_token'],
            'code': sent_code,
        })

        self.assertEqual(verify_response.status_code, 200)
        verify_data = verify_response.get_json()
        self.assertTrue(verify_data['success'])
        db.session.refresh(booking)
        self.assertEqual(booking.booking_status, 'confirmed')
        self.assertEqual(booking.booking_calendar_event_id, 'calendar-event-1')
        create_calendar.assert_called_once()
        send_confirmation.assert_called_once()
        send_counselor.assert_called_once()

    @patch('beulah_pkg.counselling_route.verify_turnstile', return_value=True)
    def test_common_domain_typo_is_rejected_before_hold_is_created(self, verify_turnstile):
        response = self.client.post('/book/submit/', json={
            'name': 'Grace Test',
            'email': 'grace@gamil.com',
            'session_type': 'audio',
            'date': self.target_date.isoformat(),
            'start_time': '10:00',
            'is_recurring': False,
            'website': '',
            'turnstile_token': 'test-token',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['suggested_email'], 'grace@gmail.com')
        self.assertEqual(db.session.query(Booking).count(), 0)

    @patch('beulah_pkg.counselling_route.validate_email')
    @patch('beulah_pkg.counselling_route.verify_turnstile', return_value=True)
    def test_domain_without_mail_delivery_is_rejected_before_hold_is_created(
        self,
        verify_turnstile,
        validate_email_mock,
    ):
        validate_email_mock.side_effect = [
            SimpleNamespace(normalized='grace@invalid-domain.test'),
            EmailUndeliverableError('The domain does not accept email.'),
        ]
        setting_name = 'BOOKING_EMAIL_CHECK_DELIVERABILITY'
        had_setting = setting_name in beulah_app.config
        previous_setting = beulah_app.config.get(setting_name)
        beulah_app.config[setting_name] = True

        try:
            response = self.client.post('/book/submit/', json={
                'name': 'Grace Test',
                'email': 'grace@invalid-domain.test',
                'session_type': 'audio',
                'date': self.target_date.isoformat(),
                'start_time': '10:00',
                'is_recurring': False,
                'website': '',
                'turnstile_token': 'test-token',
            })
        finally:
            if had_setting:
                beulah_app.config[setting_name] = previous_setting
            else:
                beulah_app.config.pop(setting_name, None)

        self.assertEqual(response.status_code, 400)
        self.assertIn('does not appear able to receive email', response.get_json()['message'])
        self.assertEqual(db.session.query(Booking).count(), 0)


if __name__ == '__main__':
    unittest.main()
