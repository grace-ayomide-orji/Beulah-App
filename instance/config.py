import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

APP_URL=os.getenv('APP_URL')
if APP_URL is None:
    raise ValueError("No APP_URL set for Flask app!")

ADMIN_EMAIL=os.getenv('ADMIN_EMAIL')
if ADMIN_EMAIL is None:
    raise ValueError("No ADMIN_EMAIL set for Flask app!")

# Flask settings
SECRET_KEY = os.getenv('SECRET_KEY')
if SECRET_KEY is None:
    raise ValueError("No SECRET_KEY set for Flask app!")

SQLALCHEMY_DATABASE_URI = os.getenv('SQLALCHEMY_DATABASE_URI')
if SQLALCHEMY_DATABASE_URI is None:
    raise ValueError("No SQLALCHEMY_DATABASE_URI set for Flask app!")

SQLALCHEMY_ENGINE_OPTIONS = {
    'pool_pre_ping': True,
    'pool_size': 3,
    'max_overflow': 5,
    'pool_timeout': 10,
    'pool_recycle': 300,
    'connect_args': {
        'connect_timeout': 5
    }
}

SQLALCHEMY_TRACK_MODIFICATIONS = False

# Session / cookie settings
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = os.getenv('FLASK_ENV') == 'production'
SESSION_COOKIE_SAMESITE = 'Lax'



# Flask-Mail settings
MAIL_SERVER = 'smtp.gmail.com'
MAIL_PORT = 465
MAIL_USE_TLS = False
MAIL_USE_SSL = True
MAIL_DEBUG = (
    os.getenv('MAIL_DEBUG', 'false').lower() in ('true', '1', 'yes', 'on')
    and os.getenv('FLASK_ENV') != 'production'
)

MAIL_USERNAME = os.getenv('MAIL_USERNAME')
if MAIL_USERNAME is None:
    raise ValueError("No MAIL_USERNAME set for Flask app!")

MAIL_PASSWORD = os.getenv('MAIL_PASSWORD')
if MAIL_PASSWORD is None:
    raise ValueError("No MAIL_PASSWORD set for Flask app!")

MAIL_DEFAULT_SENDER = os.getenv('MAIL_DEFAULT_SENDER')
if MAIL_DEFAULT_SENDER is None:
    raise ValueError("No MAIL_DEFAULT_SENDER set for Flask app!")


# Google Calendar / Google Meet
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
GOOGLE_REFRESH_TOKEN = os.getenv('GOOGLE_REFRESH_TOKEN')
COUNSELOR_EMAIL = os.getenv('COUNSELOR_EMAIL')
GOOGLE_CALENDAR_ID = os.getenv('GOOGLE_CALENDAR_ID', 'primary')



# Cloudflare Turnstile
TURNSTILE_SITE_KEY = os.getenv('TURNSTILE_SITE_KEY')
TURNSTILE_SECRET_KEY = os.getenv('TURNSTILE_SECRET_KEY')



# Admin session management
ADMIN_SESSION_ROTATION_MINUTES = int(os.getenv('ADMIN_SESSION_ROTATION_MINUTES', '15'))

ADMIN_SESSION_IDLE_MINUTES = int(os.getenv('ADMIN_SESSION_IDLE_MINUTES', '30'))

ADMIN_SESSION_MAX_HOURS = int(os.getenv('ADMIN_SESSION_MAX_HOURS', '5'))

ADMIN_MFA_CODE_MINUTES = int(os.getenv('ADMIN_MFA_CODE_MINUTES', '10'))

ADMIN_MFA_MAX_ATTEMPTS = int(os.getenv('ADMIN_MFA_MAX_ATTEMPTS', '5'))

ADMIN_LOCKOUT_THRESHOLD = int(os.getenv('ADMIN_LOCKOUT_THRESHOLD', '5'))

ADMIN_LOCKOUT_MINUTES = int(os.getenv('ADMIN_LOCKOUT_MINUTES', '30'))

ADMIN_PASSWORD_RESET_MINUTES = int(os.getenv('ADMIN_PASSWORD_RESET_MINUTES', '30'))



# Cleanup scheduler
ENABLE_CLEANUP_SCHEDULER = (
    os.getenv('ENABLE_CLEANUP_SCHEDULER', 'true').lower()
    in ('true', '1', 'yes', 'on')
)

ENABLE_ADMIN_LOG_CLEANUP_SCHEDULER = (
    os.getenv('ENABLE_ADMIN_LOG_CLEANUP_SCHEDULER', 'true').lower()
    in ('true', '1', 'yes', 'on')
)

ENABLE_EVENT_CLEANUP_SCHEDULER = (
    os.getenv('ENABLE_EVENT_CLEANUP_SCHEDULER', 'true').lower()
    in ('true', '1', 'yes', 'on')
)

CLEANUP_SCHEDULER_TIMEZONE = os.getenv('CLEANUP_SCHEDULER_TIMEZONE', 'Africa/Lagos')
ADMIN_LOG_CLEANUP_HOUR = int(os.getenv('ADMIN_LOG_CLEANUP_HOUR', '3'))
EVENT_CLEANUP_HOUR = int(os.getenv('EVENT_CLEANUP_HOUR', '2'))



# Debug settings
DEBUG = os.getenv('FLASK_ENV') != 'production'



# Upload folder
UPLOAD_FOLDER = os.path.join(
    os.getcwd(),
    'beulah_pkg/static/slide_images/'
)
