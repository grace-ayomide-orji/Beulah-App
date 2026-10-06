from datetime import datetime, timedelta, date as date_cls
from beulah_pkg.models import db, WorkingHours, BlockedDate, Booking, RecurringUnavailability

SESSION_DURATION_MINUTES = 60
SLOT_INTERVAL_MINUTES = 60

_BASE_DATE = date_cls(2000, 1, 1)  # arbitrary anchor date, only used for time-math


def _combine(t):
    return datetime.combine(_BASE_DATE, t)


def generate_slots(start_time, end_time):
    """
    start_time / end_time: datetime.time objects (from WorkingHours).
    Returns a list of datetime.time objects — each a bookable start time,
    SESSION_DURATION_MINUTES long, spaced SLOT_INTERVAL_MINUTES apart,
    that fully fits before end_time.
    """
    current = _combine(start_time)
    end = _combine(end_time)
    slots = []

    while current <= end:
        slot_end = current + timedelta(minutes=SESSION_DURATION_MINUTES)
        if slot_end <= end:
            slots.append(current.time())
        current += timedelta(minutes=SLOT_INTERVAL_MINUTES)

    return slots


def _day_of_week_sunday_zero(target_date):
    # Python's date.weekday() is Monday=0 ... Sunday=6.
    # WorkingHours.wh_day_of_week follows the Sunday=0 ... Saturday=6
    # convention (matching the original DAYS array), so convert.
    return (target_date.weekday() + 1) % 7


def get_available_slots(target_date, exclude_booking_id=None):
    """
    target_date: a datetime.date.
    Returns a list of dicts: {'start_time': 'HH:MM', 'end_time': 'HH:MM', 'available': bool}
    """
    blocked = db.session.query(BlockedDate).filter_by(bd_date=target_date).first()
    if blocked:
        return []

    day_of_week = _day_of_week_sunday_zero(target_date)
    working_hours = db.session.query(WorkingHours).filter_by(wh_day_of_week=day_of_week).first()
    if not working_hours or not working_hours.wh_is_active:
        return []

    all_slots = generate_slots(working_hours.wh_start_time, working_hours.wh_end_time)

    existing_query = db.session.query(Booking.booking_start_time).filter(
        Booking.booking_date == target_date,
        Booking.booking_status != 'cancelled'
    )
    if exclude_booking_id is not None:
        existing_query = existing_query.filter(Booking.booking_id != exclude_booking_id)

    existing = existing_query.all()
    booked_times = {row[0] for row in existing}

    # Recurring weekly exceptions for this day (e.g. "every Wednesday 5-6pm")
    recurring_blocks = db.session.query(RecurringUnavailability).filter_by(
        ru_day_of_week=day_of_week
    ).all()

    result = []
    for slot_start in all_slots:
        slot_end = (_combine(slot_start) + timedelta(minutes=SESSION_DURATION_MINUTES)).time()

        # A slot is blocked if it overlaps ANY recurring block at all —
        # even partially. Standard interval-overlap check.
        is_recurring_blocked = any(
            _combine(slot_start) < _combine(rb.ru_end_time) and _combine(slot_end) > _combine(rb.ru_start_time)
            for rb in recurring_blocks
        )

        result.append({
            'start_time': slot_start.strftime('%H:%M'),
            'end_time': slot_end.strftime('%H:%M'),
            'available': (slot_start not in booked_times) and not is_recurring_blocked,
        })

    return result

def is_slot_available(target_date, start_time, exclude_booking_id=None):
    """start_time: datetime.time"""
    for slot in get_available_slots(target_date, exclude_booking_id=exclude_booking_id):
        if slot['start_time'] == start_time.strftime('%H:%M') and slot['available']:
            return True
    return False
