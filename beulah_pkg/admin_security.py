import hashlib
import os
import secrets
from datetime import datetime, timedelta
from flask import current_app, request, session, url_for
from flask_mail import Message
from werkzeug.security import check_password_hash, generate_password_hash
from beulah_pkg import mail
from beulah_pkg.models import (
    AdminAuditLog,
    AdminMfaChallenge,
    AdminPasswordResetToken,
    AdminSecurityState,
    AdminSessionToken,
    db,
)

SESSION_ROTATION_MINUTES = int(os.getenv('ADMIN_SESSION_ROTATION_MINUTES', '15'))
SESSION_IDLE_MINUTES = int(os.getenv('ADMIN_SESSION_IDLE_MINUTES', '30'))
SESSION_MAX_HOURS = int(os.getenv('ADMIN_SESSION_MAX_HOURS', '5'))
MFA_CODE_MINUTES = int(os.getenv('ADMIN_MFA_CODE_MINUTES', '10'))
MFA_MAX_ATTEMPTS = int(os.getenv('ADMIN_MFA_MAX_ATTEMPTS', '5'))
LOCKOUT_THRESHOLD = int(os.getenv('ADMIN_LOCKOUT_THRESHOLD', '5'))
LOCKOUT_MINUTES = int(os.getenv('ADMIN_LOCKOUT_MINUTES', '30'))
PASSWORD_RESET_MINUTES = int(os.getenv('ADMIN_PASSWORD_RESET_MINUTES', '30'))

SECURITY_TABLES = (
    AdminSecurityState.__table__,
    AdminMfaChallenge.__table__,
    AdminSessionToken.__table__,
    AdminPasswordResetToken.__table__,
    AdminAuditLog.__table__,
)

def ensure_security_tables(app):
    with app.app_context():
        for table in SECURITY_TABLES:
            table.create(bind=db.engine, checkfirst=True)
        try:
            with db.engine.begin() as connection:
                connection.exec_driver_sql(
                    'ALTER TABLE admin_mfa_challenges MODIFY code_hash VARCHAR(255) NOT NULL'
                )
        except Exception:
            current_app.logger.debug('Could not widen admin_mfa_challenges.code_hash.', exc_info=True)

def hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()

def admin_security_email_recipient():
    return (
        os.getenv('ADMIN_MFA_EMAIL')
        or os.getenv('ADMIN_EMAIL')
        or current_app.config.get('MAIL_USERNAME')
    )

def _request_ip():
    return (request.remote_addr or '')[:45]

def _request_user_agent():
    return (request.headers.get('User-Agent') or '')[:255]

def log_admin_action(action, admin_id=None, details=None):
    try:
        db.session.add(AdminAuditLog(
            admin_id=admin_id,
            action=action,
            details=details,
            ip_address=_request_ip(),
            user_agent=_request_user_agent(),
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()

def get_security_state(admin):
    state = AdminSecurityState.query.filter_by(admin_id=admin.admin_id).first()
    if not state:
        state = AdminSecurityState(admin_id=admin.admin_id)
        db.session.add(state)
        db.session.flush()
    return state

def is_admin_locked(admin):
    state = get_security_state(admin)
    return bool(state.locked_until and state.locked_until > datetime.utcnow())

def register_failed_login(admin):
    state = get_security_state(admin)
    state.failed_login_attempts += 1
    state.last_failed_at = datetime.utcnow()
    if state.failed_login_attempts >= LOCKOUT_THRESHOLD:
        state.locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
        log_admin_action('admin_login_locked', admin.admin_id, 'Too many failed login attempts.')
    db.session.commit()

def reset_failed_login(admin):
    state = get_security_state(admin)
    state.failed_login_attempts = 0
    state.locked_until = None
    state.last_failed_at = None
    db.session.commit()

def validate_admin_password(password):
    if len(password) < 12:
        return False, 'Password must be at least 12 characters long.'
    if not any(char.islower() for char in password):
        return False, 'Password must include a lowercase letter.'
    if not any(char.isupper() for char in password):
        return False, 'Password must include an uppercase letter.'
    if not any(char.isdigit() for char in password):
        return False, 'Password must include a number.'
    return True, 'Password is valid.'

def create_mfa_challenge(admin):
    now = datetime.utcnow()

    # Invalidate previous unused MFA codes
    AdminMfaChallenge.query.filter(
        AdminMfaChallenge.admin_id == admin.admin_id,
        AdminMfaChallenge.consumed_at.is_(None),
    ).update(
        {'consumed_at': now},
        synchronize_session=False
    )

    code = f'{secrets.randbelow(1_000_000):06d}'

    challenge = AdminMfaChallenge(
        admin_id=admin.admin_id,
        code_hash=generate_password_hash(code),
        expires_at=now + timedelta(minutes=MFA_CODE_MINUTES),
    )

    db.session.add(challenge)
    db.session.commit()

    send_mfa_code(admin, code)

    return challenge

def send_mfa_code(admin, code):
    recipient = admin_security_email_recipient()
    if not recipient:
        raise RuntimeError('ADMIN_MFA_EMAIL or MAIL_USERNAME is required for admin MFA.')

    msg = Message(
        subject='Your Beulah admin verification code',
        sender=current_app.config['MAIL_DEFAULT_SENDER'],
        recipients=[recipient],
    )
    msg.body = (
        f'Your admin verification code is {code}.\n\n'
        f'This code expires in {MFA_CODE_MINUTES} minutes. '
        'If you did not try to sign in, change the admin password immediately.'
    )
    mail.send(msg)

def create_password_reset_token(admin):
    now = datetime.utcnow()

    AdminPasswordResetToken.query.filter(
        AdminPasswordResetToken.admin_id == admin.admin_id,
        AdminPasswordResetToken.consumed_at.is_(None),
    ).update(
        {'consumed_at': now},
        synchronize_session=False
    )

    token = secrets.token_urlsafe(48)
    token_record = AdminPasswordResetToken(
        admin_id=admin.admin_id,
        token_hash=hash_token(token),
        expires_at=now + timedelta(minutes=PASSWORD_RESET_MINUTES),
        ip_address=_request_ip(),
        user_agent=_request_user_agent(),
    )

    db.session.add(token_record)
    db.session.commit()
    send_password_reset_link(admin, token)
    return token_record

def send_password_reset_link(admin, token):
    recipient = admin_security_email_recipient()
    if not recipient:
        raise RuntimeError('ADMIN_MFA_EMAIL or MAIL_USERNAME is required for admin password reset.')

    reset_path = url_for('admin_reset_password', token=token, _external=False)
    app_url = (current_app.config.get('APP_URL') or '').rstrip('/')
    reset_url = f'{app_url}{reset_path}' if app_url else url_for('admin_reset_password', token=token, _external=True)
    msg = Message(
        subject='Reset your Beulah admin password',
        sender=current_app.config['MAIL_DEFAULT_SENDER'],
        recipients=[recipient],
    )
    msg.body = (
        f'A password reset was requested for the Beulah admin account "{admin.admin_username}".\n\n'
        f'Reset your password here: {reset_url}\n\n'
        f'This link expires in {PASSWORD_RESET_MINUTES} minutes. '
        'If you did not request this, ignore this email and review admin audit logs.'
    )
    mail.send(msg)

def get_password_reset_record(token):
    if not token:
        return None, 'Password reset link is invalid.'

    record = AdminPasswordResetToken.query.filter_by(
        token_hash=hash_token(str(token))
    ).first()

    if not record or record.consumed_at:
        return None, 'Password reset link is invalid or has already been used.'
    if record.expires_at < datetime.utcnow():
        return None, 'Password reset link has expired.'

    return record, 'Password reset link is valid.'

def revoke_all_admin_sessions(admin_id):
    now = datetime.utcnow()
    AdminSessionToken.query.filter(
        AdminSessionToken.admin_id == admin_id,
        AdminSessionToken.revoked_at.is_(None),
    ).update(
        {'revoked_at': now},
        synchronize_session=False
    )
    db.session.commit()

    session.clear()

def verify_mfa_challenge(challenge, code):
    if not challenge or challenge.consumed_at:
        return False, 'Verification code is no longer valid.'
    if challenge.expires_at < datetime.utcnow():
        return False, 'Verification code has expired. Please log in again.'
    if challenge.attempts >= MFA_MAX_ATTEMPTS:
        return False, 'Too many verification attempts. Please log in again.'

    challenge.attempts += 1
    if not check_password_hash(challenge.code_hash, code):
        db.session.commit()
        return False, 'Invalid verification code.'

    challenge.consumed_at = datetime.utcnow()
    db.session.commit()
    return True, 'Verified.'

def create_admin_session(admin):
    session_token = secrets.token_urlsafe(48)
    now = datetime.utcnow()

    # Revoke every previous active session for this admin
    AdminSessionToken.query.filter(
        AdminSessionToken.admin_id == admin.admin_id,
        AdminSessionToken.revoked_at.is_(None),
    ).update(
        {'revoked_at': now},
        synchronize_session=False
    )

    token_record = AdminSessionToken(
        admin_id=admin.admin_id,

        session_token_hash=hash_token(session_token),

        rotate_after=now + timedelta(
            minutes=SESSION_ROTATION_MINUTES
        ),

        expires_at=now + timedelta(
            hours=SESSION_MAX_HOURS
        ),

        ip_address=_request_ip(),
        user_agent=_request_user_agent(),
        last_seen_at=now,
    )

    db.session.add(token_record)
    db.session.commit()

    session.clear()

    session.permanent = True
    session['admin_session_id'] = session_token

    return token_record

def revoke_current_admin_session():
    session_token = session.get('admin_session_id')

    if session_token:
        record = AdminSessionToken.query.filter_by(
            session_token_hash=hash_token(str(session_token))
        ).first()

        if record and not record.revoked_at:
            record.revoked_at = datetime.utcnow()
            db.session.commit()

    session.clear()

def _valid_admin_session_record(admin_id=None):
    session_token = session.get('admin_session_id')

    if not session_token:
        return None

    query = AdminSessionToken.query.filter_by(
        session_token_hash=hash_token(str(session_token))
    )

    if admin_id is not None:
        query = query.filter_by(admin_id=admin_id)

    record = query.first()
    now = datetime.utcnow()

    if not record:
        session.clear()
        return None

    if record.revoked_at:
        session.clear()
        return None

    # Maximum lifetime: currently 5 hours
    if record.expires_at <= now:
        revoke_current_admin_session()
        return None

    # Logout after 30 minutes of inactivity
    if (
        record.last_seen_at
        and now - record.last_seen_at
        > timedelta(minutes=SESSION_IDLE_MINUTES)
    ):
        revoke_current_admin_session()
        return None

    # Rotate session ID every 15 minutes
    if record.rotate_after <= now:
        new_session_token = secrets.token_urlsafe(48)

        session['admin_session_id'] = new_session_token

        record.session_token_hash = hash_token(
            new_session_token
        )

        record.rotate_after = (
            now + timedelta(minutes=SESSION_ROTATION_MINUTES)
        )

    record.last_seen_at = now

    db.session.commit()

    return record

def current_admin_id_from_session():
    record = _valid_admin_session_record()
    return record.admin_id if record else None

def validate_or_refresh_admin_session(admin_id=None):
    record = _valid_admin_session_record(admin_id)
    if not record:
        return False
    if admin_id and record.admin_id != admin_id:
        return False
    return True
