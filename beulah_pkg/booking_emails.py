import os
from datetime import date as date_cls
from flask_mail import Message
from beulah_pkg import app, mail

APP_URL = os.environ.get('APP_URL', 'http://localhost:8060')
ADMIN_EMAIL = app.config.get('ADMIN_EMAIL', 'beufound@gmail.com')


def _format_date(d):
    """Matches the suffix style already used by format_date_with_suffix /
    format_event_date in user_route.py, kept local here to avoid a
    cross-import just for one helper."""
    day = d.day
    if 10 <= day % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
    return d.strftime(f'%A, %B {day}{suffix}, %Y')


def _base_template(inner_html):
    year = date_cls.today().year
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Beulah Foundation for Christ</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:'Nunito', Arial, sans-serif; background:#f2f2f2; color:#1a1a1a; }}
  .wrapper {{ max-width:600px; margin:40px auto; background:#fff; border-radius:16px; overflow:hidden; box-shadow:0 4px 24px rgba(109,15,21,0.1); }}
  .header {{ background:#6D0F15; padding:15px; text-align:center; }}
  .header h1 {{ color:#fff; font-size:20px; font-weight:800; }}
  .body {{ padding:36px 15px; }}
  .detail-card {{ background:#F4F4F4; border-radius:12px; padding:18px; margin:20px 0; }}
  .detail-row {{ display:flex; gap:4px; justify-content:space-between; padding:6px 0; font-size:14px; border-bottom:1px solid #e5e5e5; }}
  .detail-row:last-child {{ border:none; }}
  .detail-label {{ color:#666; font-weight:600; }}
  .detail-value {{ color:#1a1a1a; font-weight:700; }}
  .btn {{ display:inline-block; background:#6D0F15; color:#fff !important; text-decoration:none; padding:13px 20px; border-radius:10px; font-weight:700; font-size:14px; margin:6px 4px; }}
  .btn-outline {{ background:transparent; border:2px solid #6D0F15; color:#6D0F15 !important; }}
  .footer {{ background:#F2F2F2; padding:22px 20px; text-align:center; font-size:12px; color:#999; }}
  .footer a {{ color:#6D0F15; }}
  p {{ font-size:15px; line-height:1.7; color:#444; margin-bottom:12px; }}
</style>
</head>
<body>
  <div class="wrapper">
    <div class="header"><h1>Beulah Foundation for Christ</h1></div>
    <div class="body">{inner_html}</div>
    <div class="footer">
      <p style="margin-top:4px">&copy; {year} Beulah Foundation for Christ. All rights reserved.</p>
    </div>
  </div>
</body>
</html>"""


# ── Booking confirmation to client ────────────────────────────────────────
def send_booking_confirmation(booking):
    manage_url = f"{APP_URL}/book_counselling/?token={booking.booking_manage_token}"
    date_formatted = _format_date(booking.booking_date)
    start = booking.booking_start_time.strftime('%H:%M')
    end = booking.booking_end_time.strftime('%H:%M')

    meet_row = ''
    join_btn = ''
    meet_note = '<p>Your meeting link will be sent by email before the session.</p>'
    if booking.booking_meet_link:
        meet_row = (
            '<div class="detail-row"><span class="detail-label">Meeting Link:</span>'
            f'<span class="detail-value"><a href="{booking.booking_meet_link}" style="color:#6D0F15">'
            f'{booking.booking_meet_link}</a></span></div>'
        )
        join_btn = f'<a href="{booking.booking_meet_link}" class="btn">Join Meeting</a>'
        meet_note = ''

    inner = f"""
    <p>Hi <strong>{booking.booker.booker_name}</strong>,</p>
    <p>Your counselling session has been successfully booked. We look forward to speaking with you.</p>
    <div class="detail-card">
      <div class="detail-row"><span class="detail-label">Date:</span><span class="detail-value">{date_formatted}</span></div>
      <div class="detail-row"><span class="detail-label">Time:</span><span class="detail-value">{start} - {end} (WAT)</span></div>
      <div class="detail-row"><span class="detail-label">Format:</span><span class="detail-value">{booking.booking_session_type.capitalize()}</span></div>
      {meet_row}
    </div>
    <p style="text-align:center; margin-top:24px">
      {join_btn}
      <a href="{manage_url}" class="btn btn-outline">Manage Appointment</a>
    </p>
    {meet_note}
    <p style="margin-top:22px">To reschedule or cancel your appointment, use your secure management link above. Please note cancellations must be made at least 24 hours before your session.</p>
    <p>God bless,<br/><strong>Beulah Foundation for Christ</strong></p>
    """

    msg = Message(
        subject=f"Session Confirmed — {date_formatted}",
        sender=app.config['MAIL_DEFAULT_SENDER'],
        recipients=[booking.booker.booker_email]
    )
    msg.html = _base_template(inner)
    mail.send(msg)


# ── Admin/counselor notification ──────────────────────────────────────────
def send_booking_rescheduled_confirmation(booking, old_date=None, old_start=None, old_end=None):
    manage_url = f"{APP_URL}/book_counselling/?token={booking.booking_manage_token}"
    date_formatted = _format_date(booking.booking_date)
    start = booking.booking_start_time.strftime('%H:%M')
    end = booking.booking_end_time.strftime('%H:%M')

    previous_row = ''
    if old_date and old_start and old_end:
        previous_row = (
            '<div class="detail-row"><span class="detail-label">Previous Time:</span>'
            f'<span class="detail-value">{_format_date(old_date)} '
            f'{old_start.strftime("%H:%M")} - {old_end.strftime("%H:%M")} (WAT)</span></div>'
        )

    meet_row = ''
    join_btn = ''
    meet_note = '<p>Your meeting link will be sent by email before the session.</p>'
    if booking.booking_meet_link:
        meet_row = (
            '<div class="detail-row"><span class="detail-label">Meeting Link:</span>'
            f'<span class="detail-value"><a href="{booking.booking_meet_link}" style="color:#6D0F15">'
            f'{booking.booking_meet_link}</a></span></div>'
        )
        join_btn = f'<a href="{booking.booking_meet_link}" class="btn">Join Meeting</a>'
        meet_note = ''

    inner = f"""
    <p>Hi <strong>{booking.booker.booker_name}</strong>,</p>
    <p>Your counselling session has been rescheduled. Please use the updated date and time below.</p>
    <div class="detail-card">
      {previous_row}
      <div class="detail-row"><span class="detail-label">New Date:</span><span class="detail-value">{date_formatted}</span></div>
      <div class="detail-row"><span class="detail-label">New Time:</span><span class="detail-value">{start} - {end} (WAT)</span></div>
      <div class="detail-row"><span class="detail-label">Format:</span><span class="detail-value">{booking.booking_session_type.capitalize()}</span></div>
      {meet_row}
    </div>
    <p style="text-align:center; margin-top:24px">
      {join_btn}
      <a href="{manage_url}" class="btn btn-outline">Manage Appointment</a>
    </p>
    {meet_note}
    <p>God bless,<br/><strong>Beulah Foundation for Christ</strong></p>
    """

    msg = Message(
        subject=f"Session Rescheduled - {date_formatted}",
        sender=app.config['MAIL_DEFAULT_SENDER'],
        recipients=[booking.booker.booker_email]
    )
    msg.html = _base_template(inner)
    mail.send(msg)


def send_counselor_notification(event_type, booking, cancel_reason=None):
    """event_type: 'created' | 'cancelled' | 'rescheduled'"""
    labels = {
        'created': 'New Booking',
        'cancelled': 'Booking Cancelled',
        'rescheduled': 'Booking Rescheduled',
    }
    label = labels.get(event_type, 'Booking Update')
    date_formatted = _format_date(booking.booking_date)
    start = booking.booking_start_time.strftime('%H:%M')
    end = booking.booking_end_time.strftime('%H:%M')

    reason_row = ''
    if cancel_reason:
        reason_row = (
            '<div class="detail-row"><span class="detail-label">Reason</span>'
            f'<span class="detail-value">{cancel_reason}</span></div>'
        )
    meet_row = ''
    join_btn = ''

    if booking.booking_meet_link:
        meet_row = (
            '<div class="detail-row"><span class="detail-label">Meeting Link</span>'
            f'<span class="detail-value"><a href="{booking.booking_meet_link}" '
            'style="color:#6D0F15">'
            f'{booking.booking_meet_link}</a></span></div>'
        )

        join_btn = (
            f'<a href="{booking.booking_meet_link}" class="btn">'
            'Join Meeting'
            '</a>'
        )
    inner = f"""
    <p><strong>{label}:</strong> {booking.booker.booker_name}</p>

    <div class="detail-card">
    <div class="detail-row">
        <span class="detail-label">Client</span>
        <span class="detail-value">{booking.booker.booker_name}</span>
    </div>

    <div class="detail-row">
        <span class="detail-label">Email</span>
        <span class="detail-value">{booking.booker.booker_email}</span>
    </div>

    <div class="detail-row">
        <span class="detail-label">Date</span>
        <span class="detail-value">{date_formatted}</span>
    </div>

    <div class="detail-row">
        <span class="detail-label">Time</span>
        <span class="detail-value">{start} &ndash; {end} (WAT)</span>
    </div>

    {meet_row}
    {reason_row}
    </div>

    <p style="text-align:center">
    {join_btn}

    <a href="{APP_URL}/admin/appointments/" class="btn btn-outline">
        View in Dashboard
    </a>
    </p>
    """

    msg = Message(
        subject=f"[Admin] {label} — {booking.booker.booker_name}",
        sender=app.config['MAIL_DEFAULT_SENDER'],
        recipients=[ADMIN_EMAIL]
    )
    msg.html = _base_template(inner)
    mail.send(msg)


# ── Donation receipt ───────────────────────────────────────────────────────
def send_donation_receipt(donation):
    if not donation.donation_email:
        return  # anonymous donor — nothing to send to

    inner = f"""
    <p>Dear <strong>{donation.donation_name or 'Friend'}</strong>,</p>
    <p>Thank you so much for your generous donation. Your support means the world and helps us continue the ministry of evangelism and discipleship.</p>
    <div class="detail-card">
      <div class="detail-row"><span class="detail-label">Amount</span><span class="detail-value">{donation.donation_currency} {donation.donation_amount:.2f}</span></div>
      <div class="detail-row"><span class="detail-label">Reference</span><span class="detail-value">{donation.donation_transaction_reference}</span></div>
      <div class="detail-row"><span class="detail-label">Date</span><span class="detail-value">{_format_date(date_cls.today())}</span></div>
    </div>
    <p>Your kindness is deeply appreciated. May God richly bless you.</p>
    <p>With gratitude,<br/><strong>Beulah Foundation for Christ</strong></p>
    """

    msg = Message(
        subject="💙 Donation Received — Thank You!",
        sender=app.config['MAIL_DEFAULT_SENDER'],
        recipients=[donation.donation_email]
    )
    msg.html = _base_template(inner)
    mail.send(msg)
