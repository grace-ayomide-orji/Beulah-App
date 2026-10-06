import os
import re
import hmac
import hashlib
import logging
import requests
import stripe
from datetime import datetime, timedelta, date as date_cls
from flask import render_template, request, jsonify
from markupsafe import escape
from beulah_pkg import app, csrf, limiter
from beulah_pkg.spam_defense import is_honeypot_triggered, verify_turnstile
from beulah_pkg.availability_helpers import get_available_slots, is_slot_available
from beulah_pkg.models import db, Booker, Booking, RecurringSeries, Donation
from beulah_pkg.booking_emails import send_booking_confirmation, send_booking_rescheduled_confirmation, send_counselor_notification, send_donation_receipt
from beulah_pkg.google_calendar import create_calendar_event, delete_calendar_event, update_calendar_event


EMAIL_REGEX = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
SESSION_DURATION_MINUTES = 60

# ── Payment gateway config (set these in your .env) ──────────────────────
APP_URL = os.environ.get('APP_URL', 'http://localhost:8060')
PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY')
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')
stripe.api_key = STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# STEP 1 — the tabbed page (Book / Manage / Donate)
# ─────────────────────────────────────────────────────────────────────────
@app.route('/book_counselling/')
def book_session():
    prefill_token = request.args.get('token', '').strip()
    return render_template(
        'user/book_counselling.html',
        prefill_token=prefill_token,
        today=date_cls.today().isoformat()
    )


# ─────────────────────────────────────────────────────────────────────────
# STEP 2 — available time slots for a given date (AJAX)
# ─────────────────────────────────────────────────────────────────────────
@app.route('/book/available-slots/')
def available_slots():
    date_str = request.args.get('date', '').strip()
    if not date_str:
        return jsonify({'success': False, 'message': 'A date is required.'}), 400

    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid date format.'}), 400

    if target_date < date_cls.today():
        return jsonify({'success': True, 'slots': []})

    slots = get_available_slots(target_date)
    return jsonify({'success': True, 'slots': slots})


# ─────────────────────────────────────────────────────────────────────────
# STEP 3 — booking submit (single + recurring) + manage (lookup / reschedule / cancel)
# ─────────────────────────────────────────────────────────────────────────
def _add_months(d, months):
    """Add calendar months to a date, clamping the day if the target month
    is shorter (e.g. Jan 31 + 1 month -> Feb 28/29)."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    days_in_month = [
        31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31, 30, 31, 30, 31, 31, 30, 31, 30, 31
    ]
    day = min(d.day, days_in_month[month - 1])
    return d.replace(year=year, month=month, day=day)


def _get_recurring_dates(start_date, end_date, frequency):
    dates = []
    current = start_date
    while current <= end_date:
        dates.append(current)
        if frequency == 'weekly':
            current = current + timedelta(days=7)
        elif frequency == 'biweekly':
            current = current + timedelta(days=14)
        else:  # monthly
            current = _add_months(current, 1)
    return dates


def _serialize_booking(booking):
    return {
        'booking_id': booking.booking_id,
        'booker_name': booking.booker.booker_name,
        'booker_email': booking.booker.booker_email,
        'date': booking.booking_date.isoformat(),
        'start_time': booking.booking_start_time.strftime('%H:%M'),
        'end_time': booking.booking_end_time.strftime('%H:%M'),
        'session_type': booking.booking_session_type,
        'status': booking.booking_status,
        'meet_link': booking.booking_meet_link,
        'manage_token': booking.booking_manage_token,
    }


def _booking_end_datetime(booking):
    return datetime.combine(booking.booking_date, booking.booking_end_time)


def _now_wat_naive():
    return datetime.utcnow() + timedelta(hours=1)


def _booking_manage_token_expired(booking):
    closed_statuses = ('cancelled', 'completed', 'no_show')
    if booking.booking_status in closed_statuses:
        return True

    now_wat = _now_wat_naive()
    if booking.series_id:
        active_future_count = db.session.query(Booking).filter(
            Booking.series_id == booking.series_id,
            Booking.booking_status.notin_(closed_statuses),
            Booking.booking_date >= now_wat.date(),
        ).all()
        return all(_booking_end_datetime(item) < now_wat for item in active_future_count)

    return _booking_end_datetime(booking) < now_wat


@app.route('/book/submit/', methods=['POST'])
@limiter.limit("5 per hour")
def submit_booking():
    data = request.get_json(silent=True) or {}

    if is_honeypot_triggered(data):
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again.'}), 400

    if not verify_turnstile(data.get('turnstile_token'), request.remote_addr):
        return jsonify({'success': False, 'message': 'CAPTCHA verification failed. Please try again.'}), 403

    name = str(escape((data.get('name') or '').strip()))
    email = str(escape((data.get('email') or '').strip()))
    phone = str(escape((data.get('phone') or '').strip())) or None
    date_str = (data.get('date') or '').strip()
    start_time_str = (data.get('start_time') or '').strip()
    session_type = (data.get('session_type') or 'video').strip().lower()
    reason = str(escape((data.get('reason') or '').strip())) or None
    is_recurring = bool(data.get('is_recurring'))
    frequency = (data.get('frequency') or '').strip().lower()
    end_date_str = (data.get('end_date') or '').strip()

    if len(name) < 2:
        return jsonify({'success': False, 'message': 'Full name is required.'}), 400

    if not re.match(EMAIL_REGEX, email):
        return jsonify({'success': False, 'message': 'A valid email is required.'}), 400

    if session_type not in ('video', 'audio'):
        return jsonify({'success': False, 'message': 'Invalid session type.'}), 400

    try:
        booking_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        start_time = datetime.strptime(start_time_str, '%H:%M').time()
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid date or time.'}), 400

    end_time = (datetime.combine(booking_date, start_time) + timedelta(minutes=SESSION_DURATION_MINUTES)).time()

    series_end_date = None
    if is_recurring:
        if frequency not in ('weekly', 'biweekly', 'monthly'):
            return jsonify({'success': False, 'message': 'Invalid frequency.'}), 400
        try:
            series_end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'success': False, 'message': 'Invalid end date.'}), 400
        if series_end_date < booking_date:
            return jsonify({'success': False, 'message': 'End date must be after the start date.'}), 400

    booker = db.session.query(Booker).filter_by(booker_email=email).first()
    if not booker:
        booker = Booker(booker_name=name, booker_email=email, booker_phone=phone)
        db.session.add(booker)
        db.session.flush()  # assigns booker.booker_id without committing yet

    created_bookings = []

    try:
        if not is_recurring:
            if not is_slot_available(booking_date, start_time):
                return jsonify({'success': False, 'message': 'This slot is no longer available.'}), 409
 
            calendar_event_id = None
            meet_link = None
            calendar_error = None
            try:
                cal_result = create_calendar_event(
                    date_str=booking_date.isoformat(),
                    start_time_str=start_time.strftime('%H:%M'),
                    end_time_str=end_time.strftime('%H:%M'),
                    client_email=email,
                    client_name=name,
                    description=reason or ''
                )
                calendar_event_id = cal_result['event_id']
                meet_link = cal_result['meet_link']
                if not meet_link:
                    calendar_error = 'Google Calendar did not return a Meet link.'
            except Exception as exc:
                calendar_error = str(exc)
                logger.exception('Failed to create Google Calendar event for booking.')
                # Booking still proceeds without a calendar event — better than
                # losing the booking entirely over a Google API hiccup. Notes
                # this needs manual follow-up rather than failing silently.
                pass
 
            booking = Booking(
                booker_id=booker.booker_id,
                booking_date=booking_date,
                booking_start_time=start_time,
                booking_end_time=end_time,
                booking_session_type=session_type,
                booking_session_format='single',
                booking_status='confirmed',
                booking_reason=reason,
                booking_meet_link=meet_link,
                booking_calendar_event_id=calendar_event_id,
                booking_notes=None if meet_link else f'Calendar event failed to create Meet link: {calendar_error or "Unknown error"}',
            )
            db.session.add(booking)
            created_bookings.append(booking)

        else:
            series = RecurringSeries(
                series_frequency=frequency,
                series_start_date=booking_date,
                series_end_date=series_end_date
            )
            db.session.add(series)
            db.session.flush()  # assigns series.series_id

            for d in _get_recurring_dates(booking_date, series_end_date, frequency):
                if not is_slot_available(d, start_time):
                    continue  # skip unavailable dates silently, matching the reference app
 
                calendar_event_id = None
                meet_link = None
                calendar_error = None
                try:
                    cal_result = create_calendar_event(
                        date_str=d.isoformat(),
                        start_time_str=start_time.strftime('%H:%M'),
                        end_time_str=end_time.strftime('%H:%M'),
                        client_email=email,
                        client_name=name,
                        description=reason or ''
                    )
                    calendar_event_id = cal_result['event_id']
                    meet_link = cal_result['meet_link']
                    if not meet_link:
                        calendar_error = 'Google Calendar did not return a Meet link.'
                except Exception as exc:
                    calendar_error = str(exc)
                    logger.exception('Failed to create Google Calendar event for recurring booking.')
 
                booking = Booking(
                    booker_id=booker.booker_id,
                    series_id=series.series_id,
                    booking_date=d,
                    booking_start_time=start_time,
                    booking_end_time=end_time,
                    booking_session_type=session_type,
                    booking_session_format='recurring',
                    booking_status='confirmed',
                    booking_reason=reason,
                    booking_meet_link=meet_link,
                    booking_calendar_event_id=calendar_event_id,
                    booking_notes=None if meet_link else f'Calendar event failed to create Meet link: {calendar_error or "Unknown error"}',
                )
                db.session.add(booking)
                created_bookings.append(booking)

        db.session.commit()

    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again.'}), 500

    if not created_bookings:
        return jsonify({'success': False, 'message': 'No available slots were found in that date range.'}), 409

    first = created_bookings[0]

    try:
        send_booking_confirmation(first)
        send_counselor_notification('created', first)
    except Exception:
        logger.exception(
            'Failed to send booking confirmation emails for booking %s.',
            first.booking_id
        )

    return jsonify({
        'success': True,
        'message': 'Booking confirmed!',
        'bookings_created': len(created_bookings),
        'manage_token': first.booking_manage_token,
        'manage_url': f'{APP_URL}/book_counselling/?token={first.booking_manage_token}',
        'meet_link': first.booking_meet_link,
        'calendar_warning': None if first.booking_meet_link else 'The booking was saved, but the Google Meet link was not generated automatically.',
    })


@app.route('/book/manage/lookup/', methods=['POST'])
def manage_lookup():
    data = request.get_json(silent=True) or {}
    token = (data.get('token') or '').strip()
    if not token:
        return jsonify({'success': False, 'message': 'A manage token is required.'}), 400

    booking = db.session.query(Booking).filter_by(booking_manage_token=token).first()
    if not booking:
        return jsonify({'success': False, 'message': 'Booking not found. Please check your token.'}), 404
    if _booking_manage_token_expired(booking):
        return jsonify({'success': False, 'message': 'This booking management link has expired.'}), 410

    return jsonify({'success': True, 'booking': _serialize_booking(booking)})


@app.route('/book/manage/reschedule/', methods=['POST'])
def manage_reschedule():
    data = request.get_json(silent=True) or {}
    token = (data.get('token') or '').strip()

    booking = db.session.query(Booking).filter_by(booking_manage_token=token).first()
    if not booking:
        return jsonify({'success': False, 'message': 'Booking not found.'}), 404
    if _booking_manage_token_expired(booking):
        return jsonify({'success': False, 'message': 'This booking management link has expired.'}), 410

    if booking.booking_status in ('cancelled', 'completed'):
        return jsonify({'success': False, 'message': 'This booking can no longer be rescheduled.'}), 400

    try:
        new_date = datetime.strptime(data.get('date', ''), '%Y-%m-%d').date()
        new_start = datetime.strptime(data.get('start_time', ''), '%H:%M').time()
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid date or time.'}), 400

    now_wat = _now_wat_naive()
    if datetime.combine(new_date, new_start) <= now_wat:
        return jsonify({'success': False, 'message': 'Please choose a future appointment time.'}), 400

    if not is_slot_available(new_date, new_start, exclude_booking_id=booking.booking_id):
        return jsonify({'success': False, 'message': 'That slot is no longer available.'}), 409
 
    new_end = (datetime.combine(new_date, new_start) + timedelta(minutes=SESSION_DURATION_MINUTES)).time()
    old_date = booking.booking_date
    old_start = booking.booking_start_time
    old_end = booking.booking_end_time
    calendar_event_id = booking.booking_calendar_event_id
    meet_link = booking.booking_meet_link
 
    try:
        if calendar_event_id:
            update_calendar_event(
                calendar_event_id,
                date_str=new_date.isoformat(),
                start_time_str=new_start.strftime('%H:%M'),
                end_time_str=new_end.strftime('%H:%M')
            )
        else:
            calendar_result = create_calendar_event(
                date_str=new_date.isoformat(),
                start_time_str=new_start.strftime('%H:%M'),
                end_time_str=new_end.strftime('%H:%M'),
                client_email=booking.booker.booker_email,
                client_name=booking.booker.booker_name,
                description=booking.booking_reason or ''
            )
            calendar_event_id = calendar_result.get('event_id')
            meet_link = calendar_result.get('meet_link')
            if not calendar_event_id:
                raise RuntimeError('Google Calendar did not return an event ID.')
    except Exception:
        db.session.rollback()
        logger.exception(
            'Failed to update Google Calendar event while rescheduling booking %s.',
            booking.booking_id
        )
        return jsonify({'success': False, 'message': 'Could not update Google Calendar. Your booking was not rescheduled.'}), 502
 
    try:
        booking.booking_date = new_date
        booking.booking_start_time = new_start
        booking.booking_end_time = new_end
        booking.booking_status = 'rescheduled'
        booking.booking_calendar_event_id = calendar_event_id
        booking.booking_meet_link = meet_link
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again.'}), 500

    try:
        send_booking_rescheduled_confirmation(
            booking,
            old_date=old_date,
            old_start=old_start,
            old_end=old_end
        )
    except Exception:
        logger.exception(
            'Failed to send client reschedule email for booking %s.',
            booking.booking_id
        )

    try:
        send_counselor_notification('rescheduled', booking)
    except Exception:
        logger.exception(
            'Failed to send counselor reschedule email for booking %s.',
            booking.booking_id
        )

    return jsonify({'success': True, 'message': 'Booking rescheduled successfully.', 'booking': _serialize_booking(booking)})


@app.route('/book/manage/cancel/', methods=['POST'])
def manage_cancel():
    data = request.get_json(silent=True) or {}
    token = (data.get('token') or '').strip()

    booking = db.session.query(Booking).filter_by(booking_manage_token=token).first()
    if not booking:
        return jsonify({'success': False, 'message': 'Booking not found.'}), 404
    if _booking_manage_token_expired(booking):
        return jsonify({'success': False, 'message': 'This booking management link has expired.'}), 410

    if booking.booking_status in ('cancelled', 'completed'):
        return jsonify({'success': False, 'message': 'This booking can no longer be cancelled.'}), 400

    session_datetime = datetime.combine(booking.booking_date, booking.booking_start_time)
    now_wat = _now_wat_naive()  # WAT is a fixed UTC+1, no DST
    hours_left = (session_datetime - now_wat).total_seconds() / 3600

    if hours_left < 24:
        return jsonify({'success': False, 'message': 'Cancellations must be made at least 24 hours before the session.'}), 400
 
    try:
        delete_calendar_event(booking.booking_calendar_event_id)
        booking.booking_calendar_event_id = None
        booking.booking_meet_link = None
    except Exception:
        logger.exception(
            'Failed to delete Google Calendar event while cancelling booking %s.',
            booking.booking_id
        )

    try:
        booking.booking_status = 'cancelled'
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again.'}), 500

    try:
        send_counselor_notification('cancelled', booking)
    except Exception:
        logger.exception(
            'Failed to send cancellation notification for booking %s.',
            booking.booking_id
        )

    return jsonify({'success': True, 'message': 'Booking cancelled.'})


# ─────────────────────────────────────────────────────────────────────────
# STEP 4 — donation submit (Paystack + Stripe) + webhooks
# ─────────────────────────────────────────────────────────────────────────
def _init_paystack_transaction(donation, amount, currency, email):
    payload = {
        'email': email or 'anonymous@beufoundation.org',
        'amount': int(round(amount * 100)),  # Paystack expects the smallest currency unit (kobo)
        'currency': currency,
        'callback_url': f'{APP_URL}/book/donate/success/',
        'metadata': {'donation_id': donation.donation_id},
    }
    resp = requests.post(
        'https://api.paystack.co/transaction/initialize',
        json=payload,
        headers={'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}'},
        timeout=10
    )
    resp.raise_for_status()
    result = resp.json()
    if not result.get('status'):
        raise RuntimeError(result.get('message', 'Paystack initialization failed.'))

    donation.donation_transaction_reference = result['data']['reference']
    db.session.commit()
    return result['data']['authorization_url']


def _init_stripe_session(donation, amount, currency, name, email):
    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        mode='payment',
        line_items=[{
            'price_data': {
                'currency': currency.lower(),
                'unit_amount': int(round(amount * 100)),
                'product_data': {
                    'name': 'Donation — Beulah Foundation for Christ',
                    'description': 'Support the ministry',
                },
            },
            'quantity': 1,
        }],
        metadata={'donation_id': str(donation.donation_id)},
        customer_email=email or None,
        success_url=f'{APP_URL}/book/donate/success/?session_id={{CHECKOUT_SESSION_ID}}',
        cancel_url=f'{APP_URL}/book_counselling/',
    )
    donation.donation_transaction_reference = session.id
    db.session.commit()
    return session.url


@app.route('/book/donate/init/', methods=['POST'])
def donate_init():
    data = request.get_json(silent=True) or {}

    if is_honeypot_triggered(data):
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again.'}), 400
 
    if not verify_turnstile(data.get('turnstile_token'), request.remote_addr):
        return jsonify({'success': False, 'message': 'CAPTCHA verification failed. Please try again.'}), 403

    gateway = (data.get('gateway') or '').strip().lower()
    is_anonymous = bool(data.get('is_anonymous'))
    name = str(escape((data.get('name') or '').strip())) or None
    email = (data.get('email') or '').strip() or None
    currency = (data.get('currency') or 'NGN').strip().upper()

    try:
        amount = float(data.get('amount'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'A valid amount is required.'}), 400

    if amount < 1:
        return jsonify({'success': False, 'message': 'Minimum donation is 1.'}), 400

    if gateway not in ('paystack', 'stripe'):
        return jsonify({'success': False, 'message': 'Invalid payment method.'}), 400

    if is_anonymous:
        name, email = None, None
    elif not name or not email or not re.match(EMAIL_REGEX, email):
        return jsonify({'success': False, 'message': 'Please provide your name and a valid email, or donate anonymously.'}), 400

    donation = Donation(
        donation_name=name,
        donation_email=email,
        donation_amount=amount,
        donation_currency=currency,
        donation_gateway=gateway,
        donation_status='pending',
        donation_is_anonymous=is_anonymous,
    )
    db.session.add(donation)
    db.session.commit()

    try:
        if gateway == 'paystack':
            checkout_url = _init_paystack_transaction(donation, amount, currency, email)
        else:
            checkout_url = _init_stripe_session(donation, amount, currency, name, email)
    except Exception:
        donation.donation_status = 'failed'
        db.session.commit()
        return jsonify({'success': False, 'message': 'Could not start payment. Please try again.'}), 502

    return jsonify({'success': True, 'checkout_url': checkout_url})


def _verify_paystack_transaction(donation):
    """Best-effort immediate check so the success page can show 'completed'
    right away — the webhook below remains the source of truth."""
    try:
        resp = requests.get(
            f'https://api.paystack.co/transaction/verify/{donation.donation_transaction_reference}',
            headers={'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}'},
            timeout=10
        )
        resp.raise_for_status()
        if resp.json().get('data', {}).get('status') == 'success':
            donation.donation_status = 'completed'
            db.session.commit()
            try:
                send_donation_receipt(donation)
            except Exception:
                logger.exception(
                    'Failed to send Paystack donation receipt for donation %s.',
                    donation.donation_id
                )
    except Exception:
        logger.exception(
            'Failed to verify Paystack transaction for donation %s.',
            donation.donation_id
        )


def _verify_stripe_session(donation):
    try:
        session = stripe.checkout.Session.retrieve(donation.donation_transaction_reference)
        if session.payment_status == 'paid':
            donation.donation_status = 'completed'
            db.session.commit()
            try:
                send_donation_receipt(donation)
            except Exception:
                logger.exception(
                    'Failed to send Stripe donation receipt for donation %s.',
                    donation.donation_id
                )
    except Exception:
        logger.exception(
            'Failed to verify Stripe session for donation %s.',
            donation.donation_id
        )


@app.route('/book/donate/success/')
def donate_success():
    reference = request.args.get('reference') or request.args.get('session_id')
    donation = None

    if reference:
        donation = db.session.query(Donation).filter_by(donation_transaction_reference=reference).first()
        if donation and donation.donation_status == 'pending':
            if donation.donation_gateway == 'paystack':
                _verify_paystack_transaction(donation)
            else:
                _verify_stripe_session(donation)

    return render_template('user/donate_success.html', donation=donation)


@app.route('/webhooks/paystack/', methods=['POST'])
@csrf.exempt
def paystack_webhook():
    signature = request.headers.get('x-paystack-signature', '')
    computed = hmac.new(
        (PAYSTACK_SECRET_KEY or '').encode('utf-8'),
        request.get_data(),
        hashlib.sha512
    ).hexdigest()

    if not PAYSTACK_SECRET_KEY or not hmac.compare_digest(computed, signature):
        return jsonify({'error': 'Invalid signature'}), 401

    event = request.get_json(silent=True) or {}

    if event.get('event') == 'charge.success':
        reference = event.get('data', {}).get('reference')
        donation = db.session.query(Donation).filter_by(donation_transaction_reference=reference).first()
        if donation and donation.donation_status != 'completed':
            donation.donation_status = 'completed'
            db.session.commit()
            try:
                send_donation_receipt(donation)
            except Exception:
                logger.exception(
                    'Failed to send Paystack webhook donation receipt for donation %s.',
                    donation.donation_id
                )

    return jsonify({'received': True})


@app.route('/webhooks/stripe/', methods=['POST'])
@csrf.exempt
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get('stripe-signature', '')

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({'error': 'Invalid signature'}), 400

    if event['type'] == 'checkout.session.completed':
        session_obj = event['data']['object']
        donation = db.session.query(Donation).filter_by(donation_transaction_reference=session_obj['id']).first()
        if donation and donation.donation_status != 'completed':
            donation.donation_status = 'completed'
            db.session.commit()
            try:
                send_donation_receipt(donation)
            except Exception:
                logger.exception(
                    'Failed to send Stripe webhook donation receipt for donation %s.',
                    donation.donation_id
                )

    if event['type'] == 'checkout.session.expired':
        session_obj = event['data']['object']
        donation = db.session.query(Donation).filter_by(donation_transaction_reference=session_obj['id']).first()
        if donation:
            donation.donation_status = 'failed'
            db.session.commit()

    return jsonify({'received': True})

