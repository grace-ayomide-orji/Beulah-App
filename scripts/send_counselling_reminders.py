import os
import sys


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('ENABLE_CLEANUP_SCHEDULER', 'false')

from beulah_pkg import app
from beulah_pkg.booking_reminders import process_due_attendee_reminders


if __name__ == '__main__':
    try:
        sent_count = process_due_attendee_reminders(app)
    except Exception as exc:
        print(f'Counselling reminder processing failed: {exc}', file=sys.stderr)
        raise SystemExit(1)

    print(f'Sent {sent_count} counselling attendee reminder(s).')
