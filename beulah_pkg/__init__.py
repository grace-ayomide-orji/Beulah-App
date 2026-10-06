import os
import secrets
from datetime import timedelta
from flask import Flask, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFProtect
from flask_migrate import Migrate
from flask_mail import Mail
from beulah_pkg.models import db


csrf=CSRFProtect()
migrate=Migrate()
mail=Mail()
limiter = Limiter(key_func=get_remote_address)

# Load environment variables from the .env file
load_dotenv()


def create_app():
    app=Flask(__name__,instance_relative_config=True)
    app.config.from_pyfile('config.py', silent=True)

    env = os.getenv('FLASK_ENV', 'development')
    database_uri = os.getenv('SQLALCHEMY_DATABASE_URI')
    secret_key = os.getenv('SECRET_KEY')

    if env == 'production' and not secret_key:
        raise RuntimeError(
            'SECRET_KEY must be configured in production.'
        )

    if secret_key:
        app.config['SECRET_KEY'] = secret_key

    if env == 'production':
        app.config.update(
            DEBUG=False,
            SQLALCHEMY_DATABASE_URI=database_uri,

            SESSION_COOKIE_SECURE=True,
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE='Lax',
            SESSION_COOKIE_NAME='beulah_admin_session',

            PERMANENT_SESSION_LIFETIME=timedelta(
                hours=int(
                    os.getenv('ADMIN_SESSION_MAX_HOURS', '5')
                )
            ),
        )
    elif env == 'testing':
        # Override specific settings for testing
        app.config.update(
            TESTING=True,
            DEBUG=True,
            SQLALCHEMY_DATABASE_URI=database_uri
        )
    else:
        # Override specific settings for development (optional)
        app.config.update(
            DEBUG=True,
            SQLALCHEMY_DATABASE_URI=database_uri
        )

    app.config.setdefault('RATELIMIT_STORAGE_URI', os.getenv('RATELIMIT_STORAGE_URI', 'beulah-mysql://'))
    app.config.setdefault('RATELIMIT_STRATEGY', 'fixed-window')

    csrf.init_app(app)
    migrate.init_app(app,db)
    db.init_app(app)
    mail.init_app(app)

    from beulah_pkg import rate_limit_storage
    limiter.init_app(app)

    from beulah_pkg.cleanup_scheduler import start_cleanup_scheduler
    start_cleanup_scheduler(app)

    return app

app = create_app()
# app.config['PROPAGATE_EXCEPTIONS'] = True

@app.context_processor
def inject_turnstile_site_key():
    return {
        'turnstile_site_key': os.environ.get('TURNSTILE_SITE_KEY', ''),
        'csp_nonce': getattr(g, 'csp_nonce', ''),
    }

@app.before_request
def set_csp_nonce():
    g.csp_nonce = secrets.token_urlsafe(16)

@app.after_request
def apply_security_headers(response):
    csp_nonce = getattr(g, 'csp_nonce', '')
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    csp = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{csp_nonce}' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://esm.sh https://challenges.cloudflare.com; "
        "script-src-attr 'none'; "
        "style-src 'self' https://fonts.googleapis.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src-elem 'self' https://fonts.googleapis.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src-attr 'unsafe-inline'; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://challenges.cloudflare.com; "
        "frame-src https://challenges.cloudflare.com https://www.youtube.com https://www.youtube-nocookie.com; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    if os.getenv('FLASK_ENV') == 'production':
        csp = f'{csp}; upgrade-insecure-requests'
    response.headers.setdefault(
        'Content-Security-Policy',
        csp
    )
    if os.getenv('FLASK_ENV') == 'production':
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return response

from beulah_pkg import user_route, admin_route, error_route, counselling_route
