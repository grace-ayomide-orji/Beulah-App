import atexit
import logging
import os
import sys

from apscheduler.schedulers.background import BackgroundScheduler

from beulah_pkg.admin_log_cleanup import delete_old_admin_logs
from beulah_pkg.event_cleanup import delete_expired_events


logger = logging.getLogger(__name__)
_scheduler = None


def _enabled(app, name, default=True):
    value = app.config.get(name)
    if value is None:
        value = os.getenv(name, 'true' if default else 'false')
    if isinstance(value, bool):
        return value
    return str(value).lower() in ('true', '1', 'yes', 'on')


def _hour(app, name, default):
    try:
        hour = int(app.config.get(name, os.getenv(name, str(default))))
    except ValueError:
        logger.warning('Invalid %s value. Falling back to %s.', name, default)
        return default
    return max(0, min(23, hour))


def start_cleanup_scheduler(app):
    global _scheduler

    if app.config.get('TESTING'):
        return None

    if 'db' in sys.argv:
        return None

    if not _enabled(app, 'ENABLE_CLEANUP_SCHEDULER'):
        logger.info('Cleanup scheduler disabled by ENABLE_CLEANUP_SCHEDULER.')
        return None

    # Avoid duplicate jobs in the parent process created by Flask's debug reloader.
    if app.debug and os.getenv('FLASK_ENV') != 'production' and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return None

    if _scheduler and _scheduler.running:
        return _scheduler

    scheduler = BackgroundScheduler(
        timezone=app.config.get(
            'CLEANUP_SCHEDULER_TIMEZONE',
            os.getenv('CLEANUP_SCHEDULER_TIMEZONE', 'Africa/Lagos')
        )
    )

    if _enabled(app, 'ENABLE_EVENT_CLEANUP_SCHEDULER'):
        scheduler.add_job(
            delete_expired_events,
            trigger='cron',
            hour=_hour(app, 'EVENT_CLEANUP_HOUR', 2),
            minute=0,
            args=[app],
            id='delete_expired_events',
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    if _enabled(app, 'ENABLE_ADMIN_LOG_CLEANUP_SCHEDULER'):
        scheduler.add_job(
            delete_old_admin_logs,
            trigger='cron',
            hour=_hour(app, 'ADMIN_LOG_CLEANUP_HOUR', 3),
            minute=0,
            args=[app],
            id='delete_old_admin_logs',
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    if not scheduler.get_jobs():
        logger.info('Cleanup scheduler has no enabled jobs.')
        return None

    scheduler.start()
    _scheduler = scheduler
    atexit.register(lambda: scheduler.shutdown(wait=False))
    logger.info('Cleanup scheduler started with %s job(s).', len(scheduler.get_jobs()))
    return scheduler
