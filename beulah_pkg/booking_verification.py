import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from flask import current_app

from beulah_pkg.models import (
    Booking,
    BookingEmailVerification,
    Booker,
    RecurringSeries,
    db,
)


VERIFICATION_LIFETIME_MINUTES = 10
VERIFICATION_RESEND_SECONDS = 60
VERIFICATION_MAX_ATTEMPTS = 5
VERIFICATION_MAX_SENDS = 5

COMMON_EMAIL_DOMAIN_TYPOS = {
    'gamil.com': 'gmail.com',
    'gmial.com': 'gmail.com',
    'gmai.com': 'gmail.com',
    'gmail.co': 'gmail.com',
    'gmail.con': 'gmail.com',
    'gmail.cm': 'gmail.com',
    'hotmai.com': 'hotmail.com',
    'hotmal.com': 'hotmail.com',
    'hotmial.com': 'hotmail.com',
    'outlok.com': 'outlook.com',
    'outllook.com': 'outlook.com',
    'yaho.com': 'yahoo.com',
    'yahoo.co': 'yahoo.com',
}


def suggest_email_correction(email):
    local_part, separator, domain = email.rpartition('@')
    corrected_domain = COMMON_EMAIL_DOMAIN_TYPOS.get(domain.lower())
    if not separator or not local_part or not corrected_domain:
        return None
    return f'{local_part}@{corrected_domain}'


def generate_verification_code():
    return f'{secrets.randbelow(1_000_000):06d}'


def generate_verification_token():
    return secrets.token_urlsafe(32)


def hash_verification_code(token, code):
    secret = current_app.config['SECRET_KEY'].encode('utf-8')
    value = f'{token}:{code}'.encode('utf-8')
    return hmac.new(secret, value, hashlib.sha256).hexdigest()


def verification_code_matches(verification, code):
    supplied_hash = hash_verification_code(
        verification.verification_token,
        code,
    )
    return hmac.compare_digest(
        verification.verification_code_hash,
        supplied_hash,
    )


def pending_bookings_for_verification(verification):
    booking = verification.booking
    if not booking:
        return []
    if booking.series_id:
        return db.session.query(Booking).filter(
            Booking.series_id == booking.series_id,
            Booking.booking_status == 'pending',
        ).order_by(Booking.booking_date.asc()).all()
    if booking.booking_status == 'pending':
        return [booking]
    return []


def delete_pending_verification(verification, commit=False):
    booking = verification.booking
    booker = booking.booker if booking else None
    series = booking.recurring_series if booking and booking.series_id else None

    db.session.delete(verification)
    if series:
        for pending_booking in db.session.query(Booking).filter(
            Booking.series_id == series.series_id,
            Booking.booking_status == 'pending',
        ).all():
            db.session.delete(pending_booking)
        db.session.flush()
        if not db.session.query(Booking.booking_id).filter_by(
            series_id=series.series_id
        ).first():
            db.session.delete(series)
    elif booking and booking.booking_status == 'pending':
        db.session.delete(booking)

    db.session.flush()
    if booker and not db.session.query(Booking.booking_id).filter_by(
        booker_id=booker.booker_id
    ).first():
        db.session.delete(booker)

    if commit:
        db.session.commit()


def purge_expired_booking_verifications(commit=True):
    expired = db.session.query(BookingEmailVerification).filter(
        BookingEmailVerification.verification_verified_at.is_(None),
        BookingEmailVerification.verification_expires_at <= datetime.utcnow(),
    ).all()
    for verification in expired:
        delete_pending_verification(verification)
    if expired and commit:
        db.session.commit()
    return len(expired)


def delete_expired_booking_verifications(app):
    with app.app_context():
        return purge_expired_booking_verifications(commit=True)


def new_verification_deadlines(now=None):
    now = now or datetime.utcnow()
    return (
        now + timedelta(minutes=VERIFICATION_LIFETIME_MINUTES),
        now + timedelta(seconds=VERIFICATION_RESEND_SECONDS),
    )
