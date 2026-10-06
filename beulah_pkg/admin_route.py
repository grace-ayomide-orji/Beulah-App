import os, secrets
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template,redirect,request,flash,url_for,session,jsonify,current_app, abort
from werkzeug.utils import secure_filename
from functools import wraps
from werkzeug.security import check_password_hash,generate_password_hash
from flask_sqlalchemy import pagination
from sqlalchemy.sql import func
from sqlalchemy import extract
from bleach.css_sanitizer import CSSSanitizer
from bleach.sanitizer import Cleaner
from beulah_pkg import app, limiter
from beulah_pkg.models import db, NewsletterSubscriber,Resource,Admin, AdminMfaChallenge, AdminAuditLog, Comment, PrayerRequest, Event, Notification, Slide, Booking, Booker, Donation, WorkingHours, BlockedDate, RecurringUnavailability
from markupsafe import escape
from beulah_pkg.event_uploads import save_event_flyer,delete_event_flyer
from beulah_pkg.blog_helpers import (
    BLOG_STATUSES,
    blog_plain_text,
    blog_text_summary,
    normalize_tiptap_json,
    sanitize_blog_html,
    unique_blog_slug,
)
from beulah_pkg.blog_uploads import save_blog_image, delete_blog_image
from beulah_pkg.availability_helpers import SESSION_DURATION_MINUTES, is_slot_available
from beulah_pkg.booking_emails import send_booking_rescheduled_confirmation, send_counselor_notification
from beulah_pkg.google_calendar import create_calendar_event, delete_calendar_event, update_calendar_event
from beulah_pkg.admin_security import (
    create_admin_session,
    create_mfa_challenge,
    create_password_reset_token,
    current_admin_id_from_session,
    get_password_reset_record,
    is_admin_locked,
    log_admin_action,
    register_failed_login,
    reset_failed_login,
    revoke_all_admin_sessions,
    revoke_current_admin_session,
    validate_admin_password,
    verify_mfa_challenge,
)


RESOURCE_STATUSES = ('draft', 'published', 'suspended')
RESOURCE_STATUS_LABELS = {
    'draft': 'Draft',
    'published': 'Published',
    'suspended': 'Suspended',
}
STATUS_MANAGED_RESOURCE_TYPES = ('text', 'audio', 'slide')
COMMENT_STATUSES = ('approved', 'unapproved', 'blocked')
COMMENT_STATUS_LABELS = {
    'approved': 'Approved',
    'unapproved': 'Unapproved',
    'blocked': 'Blocked',
}

# Initialize Cleaners
content_cleaner = Cleaner(
    tags=[
        'a', 'b', 'blockquote', 'br', 'code', 'div', 'em', 'h1', 'h2', 'h3',
        'hr', 'i', 'img', 'li', 'ol', 'p', 'pre', 's', 'span', 'strong', 'u', 'ul'
    ],
    attributes={
        'a': ['href', 'title', 'target', 'rel'],
        'img': ['src', 'alt', 'title'],
        'p': ['style'],
        'h1': ['style'],
        'h2': ['style'],
        'h3': ['style'],
        'blockquote': ['style'],
        'span': ['style'],
    },
    protocols=['http', 'https', 'mailto'],
    css_sanitizer=CSSSanitizer(allowed_css_properties=['text-align']),
    strip=True
)


title_cleaner = Cleaner(
    tags=[],  # No tags allowed in titles
    strip=True
)


@app.after_request
def after_request(response):
    response.headers['Cache-Control']='no-cache, no-store, must-revalidate'
    return response 


@app.after_request
def audit_admin_mutations(response):
    if (
        request.path.startswith('/admin/')
        and request.method in ('POST', 'PUT', 'PATCH', 'DELETE')
        and request.endpoint not in ('admin_login', 'admin_mfa')
    ):
        log_admin_action(
            'admin_mutation',
            current_admin_id_from_session(),
            f'{request.method} {request.path} endpoint={request.endpoint} status={response.status_code}'
        )
    return response


def admin_required(f):
    """
    Decorator that ensures an admin is logged in before accessing a route.
    If not, it clears the admin session or aborts with a 403 error depending on the issue,
    flashes a message, and redirects to login.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        admin_id = current_admin_id_from_session()
        if not admin_id:  
            flash('Unauthorized access. Please log in.', 'danger')
            return redirect(url_for('home'))  # Redirect to home page

        admin_online = Admin.query.get(admin_id)
        if not admin_online:
            revoke_current_admin_session()
            flash('Invalid session. Please log in again.', 'warning')
            return redirect(url_for('admin_login'))  # Redirect to login page

        # Pass the admin instance to the route function for further use.
        return f(*args, admin_online=admin_online, **kwargs)

    return decorated_function


@app.template_global()
def get_selected_option(option_value):
     return 'selected' if request.args.get('sortOrder', 'desc') == option_value else ''


@app.template_global()
def get_selected_status(option_value, default_value='all'):
    return 'selected' if request.args.get('subscriberStatus', default_value) == option_value else ''


def get_json_payload():
    return request.get_json(silent=True) or {}


def get_resource_status_from_form(default='published'):
    status = request.form.get('resource_status', default).strip().lower()
    if status not in RESOURCE_STATUSES:
        return None
    return status


def get_comment_status(comment):
    if getattr(comment, 'comment_is_blocked', False):
        return 'blocked'
    if comment.comment_is_approve:
        return 'approved'
    return 'unapproved'


def format_event_date(event_date):
    # Determine the suffix for the day
    day = event_date.day
    if 10 <= day % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
    
    # Format the date without the suffix, then manually insert it
    formatted_date = event_date.strftime(f'{day}{suffix} - %b - %Y')
    return formatted_date



@app.route('/admin/login/', methods=['GET', 'POST'])
@limiter.limit("5 per 30 minutes", methods=['POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        # Validate inputs
        if not username or not password:
            return jsonify({'success': False, 'message': 'Both username and password are required.'}), 400

        # Check admin in the database
        admin = Admin.query.filter_by(admin_username=username).first()
        if admin:
            if is_admin_locked(admin):
                log_admin_action('admin_login_blocked_locked', admin.admin_id, 'Login attempt while account is locked.')
                return jsonify({'success': False, 'message': 'Invalid credentials or login temporarily unavailable.'}), 401

            # Verify password
            if check_password_hash(admin.admin_password, password):
                try:
                    challenge = create_mfa_challenge(admin)
                except Exception:
                    log_admin_action('admin_mfa_send_failed', admin.admin_id, 'Could not send MFA code.')
                    return jsonify({'success': False, 'message': 'Could not send verification code. Please try again.'}), 500

                session['pending_admin_id'] = admin.admin_id
                session['pending_mfa_challenge_id'] = challenge.challenge_id
                log_admin_action('admin_password_verified', admin.admin_id, 'MFA challenge sent.')
                return jsonify({
                    'success': True,
                    'message': 'Verification code sent.',
                    'redirect_url': url_for('admin_mfa')
                }), 200
            else:
                register_failed_login(admin)
                log_admin_action('admin_login_failed', admin.admin_id, 'Invalid password.')
                return jsonify({'success': False, 'message': 'Invalid credentials. Please try again.'}), 401
        else:
            log_admin_action('admin_login_failed_unknown', None, f'Unknown username: {username}')
            return jsonify({'success': False, 'message': 'Invalid credentials. Please try again.'}), 401

    # If the request is a GET request, redirect if already logged in
    if current_admin_id_from_session():
        return redirect(url_for('admin_dashboard'))

    return render_template('admin/login.html')


@app.route('/admin/login/mfa/', methods=['GET', 'POST'])
@limiter.limit("10 per 15 minutes")
def admin_mfa():
    pending_admin_id = session.get('pending_admin_id')
    challenge_id = session.get('pending_mfa_challenge_id')
    if not pending_admin_id or not challenge_id:
        flash('Please log in first.', 'warning')
        return redirect(url_for('admin_login'))

    admin = Admin.query.get(pending_admin_id)
    challenge = AdminMfaChallenge.query.filter_by(challenge_id=challenge_id, admin_id=pending_admin_id).first()
    if not admin or not challenge:
        session.pop('pending_admin_id', None)
        session.pop('pending_mfa_challenge_id', None)
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        ok, message = verify_mfa_challenge(challenge, code)
        if not ok:
            log_admin_action('admin_mfa_failed', admin.admin_id, message)
            return jsonify({'success': False, 'message': message}), 401

        create_admin_session(admin)
        reset_failed_login(admin)
        admin.admin_last_logged_in = datetime.utcnow()
        db.session.commit()
        session.pop('pending_admin_id', None)
        session.pop('pending_mfa_challenge_id', None)
        log_admin_action('admin_login_success', admin.admin_id, 'MFA verified.')
        return jsonify({
            'success': True,
            'message': 'Successfully logged in.',
            'redirect_url': url_for('admin_dashboard')
        }), 200

    return render_template('admin/mfa.html')


@app.route('/admin/login/mfa/resend/', methods=['POST'])
@limiter.limit("3 per 10 minutes")
def admin_mfa_resend():
    pending_admin_id = session.get('pending_admin_id')
    challenge_id = session.get('pending_mfa_challenge_id')
    if not pending_admin_id or not challenge_id:
        return jsonify({
            'success': False,
            'message': 'Please log in again before requesting a new code.',
            'redirect_url': url_for('admin_login')
        }), 401

    admin = Admin.query.get(pending_admin_id)
    challenge = AdminMfaChallenge.query.filter_by(
        challenge_id=challenge_id,
        admin_id=pending_admin_id
    ).first()

    if not admin or not challenge:
        session.pop('pending_admin_id', None)
        session.pop('pending_mfa_challenge_id', None)
        return jsonify({
            'success': False,
            'message': 'Please log in again before requesting a new code.',
            'redirect_url': url_for('admin_login')
        }), 401

    if is_admin_locked(admin):
        log_admin_action('admin_mfa_resend_blocked_locked', admin.admin_id, 'MFA resend attempted while account is locked.')
        return jsonify({'success': False, 'message': 'Login temporarily unavailable.'}), 401

    try:
        new_challenge = create_mfa_challenge(admin)
    except Exception:
        current_app.logger.exception('Unable to resend admin MFA code.')
        log_admin_action('admin_mfa_resend_failed', admin.admin_id, 'Could not resend MFA code.')
        return jsonify({'success': False, 'message': 'Could not resend verification code. Please try again.'}), 500

    session['pending_mfa_challenge_id'] = new_challenge.challenge_id
    log_admin_action('admin_mfa_resent', admin.admin_id, 'MFA challenge resent.')
    return jsonify({'success': True, 'message': 'A new verification code has been sent.'}), 200


@app.route('/admin/password/forgot/', methods=['GET', 'POST'])
@limiter.limit("5 per hour", methods=['POST'])
def admin_forgot_password():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        generic_message = 'If the admin account exists, a password reset link has been sent.'

        if username:
            admin = Admin.query.filter_by(admin_username=username).first()
            if admin:
                try:
                    create_password_reset_token(admin)
                    log_admin_action('admin_password_reset_requested', admin.admin_id, 'Password reset link sent.')
                except Exception:
                    current_app.logger.exception('Unable to send admin password reset link.')
                    log_admin_action('admin_password_reset_send_failed', admin.admin_id, 'Could not send password reset link.')
            else:
                log_admin_action('admin_password_reset_requested_unknown', None, f'Unknown username: {username}')

        return jsonify({
            'success': True,
            'message': generic_message,
            'redirect_url': url_for('admin_login')
        }), 200

    if current_admin_id_from_session():
        return redirect(url_for('admin_dashboard'))

    return render_template('admin/forgot_password.html')


@app.route('/admin/password/reset/<token>/', methods=['GET', 'POST'])
@limiter.limit("10 per 15 minutes")
def admin_reset_password(token):
    reset_record, message = get_password_reset_record(token)
    if not reset_record:
        if request.method == 'POST':
            return jsonify({
                'success': False,
                'message': message,
                'redirect_url': url_for('admin_forgot_password')
            }), 400

        flash(message, 'warning')
        return redirect(url_for('admin_forgot_password'))

    admin = Admin.query.get(reset_record.admin_id)
    if not admin:
        reset_record.consumed_at = datetime.utcnow()
        db.session.commit()

        if request.method == 'POST':
            return jsonify({
                'success': False,
                'message': 'Password reset link is invalid.',
                'redirect_url': url_for('admin_forgot_password')
            }), 400

        flash('Password reset link is invalid.', 'warning')
        return redirect(url_for('admin_forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if password != confirm_password:
            return jsonify({'success': False, 'message': 'The two passwords must match.'}), 400

        ok, password_message = validate_admin_password(password)
        if not ok:
            return jsonify({'success': False, 'message': password_message}), 400

        if check_password_hash(admin.admin_password, password):
            return jsonify({'success': False, 'message': 'New password must be different from the current password.'}), 400

        admin.admin_password = generate_password_hash(password)
        reset_record.consumed_at = datetime.utcnow()
        reset_failed_login(admin)
        log_admin_action('admin_password_reset_completed', admin.admin_id, 'Password reset completed.')
        revoke_all_admin_sessions(admin.admin_id)

        return jsonify({
            'success': True,
            'message': 'Password has been reset. Please log in again.',
            'redirect_url': url_for('admin_login')
        }), 200

    return render_template('admin/reset_password.html', token=token)


@app.route('/admin/password/change/', methods=['GET', 'POST'])
@admin_required
@limiter.limit("5 per hour", methods=['POST'])
def admin_change_password(admin_online):
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not check_password_hash(admin_online.admin_password, current_password):
            log_admin_action('admin_password_change_failed', admin_online.admin_id, 'Invalid current password.')
            return jsonify({'success': False, 'message': 'Current password is incorrect.'}), 401

        if password != confirm_password:
            return jsonify({'success': False, 'message': 'The two passwords must match.'}), 400

        ok, password_message = validate_admin_password(password)
        if not ok:
            return jsonify({'success': False, 'message': password_message}), 400

        if check_password_hash(admin_online.admin_password, password):
            return jsonify({'success': False, 'message': 'New password must be different from the current password.'}), 400

        admin_online.admin_password = generate_password_hash(password)
        db.session.commit()
        log_admin_action('admin_password_changed', admin_online.admin_id, 'Password changed by logged-in admin.')
        revoke_all_admin_sessions(admin_online.admin_id)

        return jsonify({
            'success': True,
            'message': 'Password changed. Please log in again.',
            'redirect_url': url_for('admin_login')
        }), 200

    return render_template('admin/change_password.html', admin_online=admin_online)



@app.route('/admin/logout/', methods=['POST'])
@admin_required
def log_out(admin_online):
    admin_id = current_admin_id_from_session()
    if admin_id != None:
        revoke_current_admin_session()
        log_admin_action('admin_logout', admin_id, 'Admin logged out.')
        flash('You are now logged out','success')
    else:
        flash('You are not logged in', 'error')
    return redirect(url_for('home'))



@app.route('/admin/dashboard/', methods=['POST', 'GET'])
@admin_required 
def admin_dashboard(admin_online):
    subscribers = db.session.query(NewsletterSubscriber).count()
    upcoming_events = db.session.query(Event).count()
    prayer_requests = db.session.query(PrayerRequest).count()
    audit_logs = db.session.query(AdminAuditLog).count()
    return render_template('admin/admin_index.html', subscribers=subscribers, upcoming_events=upcoming_events, prayer_requests=prayer_requests, audit_logs=audit_logs, admin_online=admin_online)


@app.route('/admin/calendar/')
@admin_required 
def admin_calendar(admin_online):
    return render_template('admin/admin_calendar.html', admin_online=admin_online)


@app.route('/admin/audit-logs/')
@admin_required
def admin_audit_logs(admin_online):
    sort_order = request.args.get('sortOrder', 'desc').lower()
    month = request.args.get('month', 'all')
    action_query = request.args.get('action', '').strip()
    admin_query = request.args.get('admin', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 25

    query = db.session.query(AdminAuditLog, Admin).outerjoin(Admin, AdminAuditLog.admin_id == Admin.admin_id)

    if month != 'all':
        try:
            month_int = int(month)
            if 1 <= month_int <= 12:
                query = query.filter(extract('month', AdminAuditLog.created_at) == month_int)
            else:
                raise ValueError("Invalid month value")
        except ValueError:
            return "Invalid month provided", 400

    if action_query:
        query = query.filter(AdminAuditLog.action.ilike(f'%{action_query}%'))

    if admin_query:
        query = query.filter(
            (Admin.admin_username.ilike(f'%{admin_query}%')) |
            (Admin.admin_fullname.ilike(f'%{admin_query}%'))
        )

    if sort_order == 'asc':
        query = query.order_by(AdminAuditLog.created_at.asc())
    else:
        query = query.order_by(AdminAuditLog.created_at.desc())

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    audit_logs = pagination.items
    base_url = url_for('admin_audit_logs')

    return render_template(
        'admin/admin_audit_logs.html',
        audit_logs=audit_logs,
        pagination=pagination,
        base_url=base_url,
        admin_online=admin_online
    )



# this function works for deleteing but audio, reading, and slide resources
@app.route('/admin/delete-resource/', methods=['POST'])
@admin_required
def delete_resource(admin_online):
    data = get_json_payload()
    id = data.get('id')  # Expecting JSON data

    if not id:
        return jsonify({'status': 'error', 'message': 'Resource ID is required.'}), 400

    resource = db.session.query(Resource).filter_by(resource_id=id).first()
    
    if not resource:
        return jsonify({'status': 'error', 'message': 'Resource not found.'}), 404
    
    try:
        resource.resource_is_deleted = True
        db.session.add(resource)
        db.session.commit()

        resource_type_labels = {
            'text': 'Message',
            'audio': 'Audio',
            'slide': 'Slide',
            'blog': 'Blog'
        }
        resource_type = resource_type_labels.get(resource.resource_type, 'Resource')
        log_admin_action(
            'resource_deleted',
            admin_online.admin_id,
            f'{resource_type} resource #{resource.resource_id} archived.'
        )
        return jsonify({'status': 'success', 'message': f'{resource_type} resource deleted successfully.'}), 200
    
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('Unable to delete resource %s', id)
        return jsonify({'status': 'error', 'message': 'Something went wrong, Please try again later'}), 500



@app.route('/admin/message-resources/')
@admin_required 
def admin_message_resources(admin_online):
    sort_order = request.args.get('sortOrder', 'desc')
    month = request.args.get('month', 'all')
    title_query = request.args.get('title', '').strip()
    status = request.args.get('status', 'all').strip().lower()
    page = request.args.get('page', 1, type=int)  # Get the page number, default is 1
    per_page = 25

    query = db.session.query(
    Resource,
    func.coalesce(func.count(Comment.comment_id), 0).label('comment_count')
    ).outerjoin(Comment, Resource.resource_id == Comment.resource_id).filter(
        Resource.resource_type == 'text',
        Resource.resource_is_deleted == False
    ).group_by(Resource.resource_id)

    if status != 'all':
        if status not in RESOURCE_STATUSES:
            return "Invalid status provided", 400
        query = query.filter(Resource.resource_status == status)
    
    # Filter by month
    if month != 'all':
        try:
            month_int = int(month)
            if 1 <= month_int <= 12:
                query = query.filter(extract('month', Resource.resource_updated_date) == month_int)
            else:
                raise ValueError("Invalid month value")
        except ValueError:
            return "Invalid month provided", 400

    # Sort based on the selected order
    if sort_order == 'asc':
        query = query.order_by(Resource.resource_updated_date.asc())
    else:
        query = query.order_by(Resource.resource_updated_date.desc())

    # Filter by title (if search query is provided)
    if title_query:
        query = query.filter(Resource.resource_title.ilike(f'%{title_query}%'))

    # Apply pagination
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Fetch paginated results
    reading_resources = pagination.items
    base_url = url_for('admin_message_resources')
    return render_template('admin/admin_message_resources.html', reading_resources=reading_resources, pagination=pagination, base_url=base_url, admin_online=admin_online)



@app.route('/admin/audio-resources/')
@admin_required 
def admin_audio_resources(admin_online):
    sort_order = request.args.get('sortOrder', 'desc').lower()
    month = request.args.get('month', 'all')
    title_query = request.args.get('title', '').strip()
    status = request.args.get('status', 'all').strip().lower()
    page = request.args.get('page', 1, type=int)  # Get the page number, default is 1
    per_page = 25


    query = db.session.query(
    Resource,
    func.coalesce(func.count(Comment.comment_id), 0).label('comment_count')
    ).outerjoin(Comment, Resource.resource_id == Comment.resource_id).filter(
        Resource.resource_type == 'audio',
        Resource.resource_is_deleted == False
    ).group_by(Resource.resource_id)

    if status != 'all':
        if status not in RESOURCE_STATUSES:
            return "Invalid status provided", 400
        query = query.filter(Resource.resource_status == status)

    # Filter by month
    if month != 'all':
        try:
            month_int = int(month)
            if 1 <= month_int <= 12:
                query = query.filter(extract('month', Resource.resource_updated_date) == month_int)
            else:
                raise ValueError("Invalid month value")
        except ValueError:
            return "Invalid month provided", 400

    # Sort based on the selected order
    if sort_order == 'asc':
        query = query.order_by(Resource.resource_updated_date.asc())
    else:
        query = query.order_by(Resource.resource_updated_date.desc())

    # Filter by title (if search query is provided)
    if title_query:
        query = query.filter(Resource.resource_title.ilike(f'%{title_query}%'))

    # Apply pagination
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Fetch paginated results
    audio_resources = pagination.items

    base_url = url_for('admin_audio_resources')
    return render_template('admin/admin_audio_resources.html',  audio_resources=audio_resources, pagination=pagination, base_url=base_url,admin_online=admin_online)



@app.route('/admin/slide-resources/')
@admin_required 
def admin_slide_resources(admin_online):
    sort_order = request.args.get('sortOrder', 'desc').lower()
    month = request.args.get('month', 'all')
    title_query = request.args.get('title', '').strip()
    status = request.args.get('status', 'all').strip().lower()
    page = request.args.get('page', 1, type=int)
    per_page = 25

    query = db.session.query(
    Resource, Slide,
    func.coalesce(func.count(Comment.comment_id), 0).label('comment_count')
    ).outerjoin(Comment, Resource.resource_id == Comment.resource_id).join(Slide).filter(
        Resource.resource_type == 'slide',
        Resource.resource_is_deleted == False
    ).group_by(Resource.resource_id, Slide.slide_id)

    if status != 'all':
        if status not in RESOURCE_STATUSES:
            return "Invalid status provided", 400
        query = query.filter(Resource.resource_status == status)

    if month != 'all':
        try:
            month_int = int(month)
            if 1 <= month_int <= 12:
                query = query.filter(extract('month', Resource.resource_updated_date) == month_int)
            else:
                raise ValueError("Invalid month value")
        except ValueError:
            return "Invalid month provided", 400

    if title_query:
        query = query.filter(Resource.resource_title.ilike(f'%{title_query}%'))

    if sort_order == 'asc':
        query = query.order_by(Resource.resource_updated_date.asc())
    else:
        query = query.order_by(Resource.resource_updated_date.desc())

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    base_url = url_for('admin_slide_resources')
    return render_template('admin/admin_slide_resources.html', slide_resources=pagination.items, pagination=pagination, base_url=base_url, admin_online=admin_online)



@app.route('/admin/add-message-resource/', methods=['POST', 'GET'])
@admin_required 
def add_message_resource(admin_online):
    if request.method =='POST':    
        try:
            title = request.form.get('title', '').strip()
            content = request.form.get('content', '').strip()
            content_json = normalize_tiptap_json(request.form.get('content_json', ''))
            resource_status = get_resource_status_from_form()

            # Validate title and content
            if not title:
                return jsonify({'success': False, 'message': 'Title cannot be empty.'}), 400
            if not content:
                return jsonify({'success': False, 'message': 'Content cannot be empty.'}), 400
            if not resource_status:
                return jsonify({'success': False, 'message': 'Invalid status selected.'}), 400
            
            # Sanitize inputs
            sanitized_title = title_cleaner.clean(title)
            sanitized_content = content_cleaner.clean(content)

            new_resource = Resource(
                # Update resource
                resource_title = str(sanitized_title), # Store sanitized title
                resource_body = str(sanitized_content),  # Store sanitized HTML content
                resource_type = 'text',
                resource_status=resource_status,
                resource_published_date=datetime.utcnow() if resource_status == 'published' else None,
                resource_content_json=content_json
                )
            # Update resource
            db.session.add(new_resource)
            db.session.commit() 

            return jsonify({'success': True, 'redirect_url': url_for('admin_message_resources')}),200
        
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': 'something went wrong, Please try again later.'}),500
       
    return render_template('admin/add_message_resource.html',admin_online=admin_online)



@app.route('/admin/add-audio-resource/', methods=['POST', 'GET'])
@admin_required 
def add_audio_resource(admin_online):
    if request.method == 'POST':    
        try:
            # Retrieve data from the form
            title = request.form.get('title', '').strip()  # Audio title
            audio_url = request.form.get('audio_url', '').strip()  # Audio URL
            resource_status = get_resource_status_from_form()

            # Validate form inputs
            if not title:
                return jsonify({'success': False, 'message': 'Title cannot be empty.'}), 400
            if not audio_url or 'youtube.com/watch' not in audio_url:
               return jsonify({'success': False, 'message': 'Invalid or empty YouTube URL.'}), 400
            if not resource_status:
                return jsonify({'success': False, 'message': 'Invalid status selected.'}), 400

            audio_id = audio_url.split('v=')[1].split('&')[0] if 'v=' in audio_url else None
            
            if not audio_id:
                return jsonify({'success': False, 'message': 'Invalid YouTube URL format.'}), 400
            sanitized_title = title_cleaner.clean(title)


            # Add new resource to the database
            new_resource = Resource(
                resource_title = str(sanitized_title), # Store sanitized title
                resource_body = audio_id,
                resource_type = 'audio',
                resource_status=resource_status,
                resource_published_date=datetime.utcnow() if resource_status == 'published' else None
            )
            db.session.add(new_resource)
            db.session.commit()

            # Return success response
            return jsonify({'success': True, 'redirect_url': url_for('admin_audio_resources')}), 200
        
        except Exception as e:
            # Rollback database changes in case of an error
            db.session.rollback()
            return jsonify({'success': False, 'message': 'Something went wrong. Please try again later.'}), 500

    return render_template('admin/add_audio_resource.html',admin_online=admin_online)



@app.route('/admin/add-slide-resource/', methods=['POST', 'GET'])
@admin_required 
def add_slide_resource(admin_online):
    if request.method =='POST':    
        try:
            title = request.form.get('title', '').strip()
            image= request.files.get('image')
            content = request.form.get('content', '').strip()
            content_json = normalize_tiptap_json(request.form.get('content_json', ''))
            resource_status = get_resource_status_from_form()

            # Validate title and content
            if not title:
                return jsonify({'success': False, 'message': 'Title cannot be empty.'}), 400
            if not content:
                return jsonify({'success': False, 'message': 'Content cannot be empty.'}), 400
            if not image:
                return jsonify({'success': False, 'message': 'Image is required.'}), 400
            if not resource_status:
                return jsonify({'success': False, 'message': 'Invalid status selected.'}), 400
            
            #Validate and save the image
            original_image = secure_filename(image.filename)
            allowed_extensions = {'jpg', 'png', 'webp'}
            ext = original_image.rsplit('.', 1)[1].lower() if '.' in original_image else ''
            if ext not in allowed_extensions:
                return jsonify({'success': False, 'message': 'Invalid image format. Allowed: jpg, png, gif.'}), 400

            unique_name = secrets.token_hex(5)
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], f'{unique_name}.{ext}')
            web_path = f'slide_images/{unique_name}.{ext}' 
            image.save(image_path)
            
            # Sanitize inputs
            sanitized_title = title_cleaner.clean(title)
            sanitized_content = content_cleaner.clean(content)

            new_resource = Resource(
                resource_title = str(sanitized_title), # Store sanitized title
                resource_body = str(sanitized_content),  # Store sanitized HTML content
                resource_type = 'slide',
                resource_status=resource_status,
                resource_published_date=datetime.utcnow() if resource_status == 'published' else None,
                resource_content_json=content_json
                )
            # Update slide
            db.session.add(new_resource)
            db.session.flush()  
            slide = Slide(
                resource_id = new_resource.resource_id,
                slide_image = web_path
            )           
            # Update slide
            db.session.add(slide)
            db.session.commit() 

            return jsonify({'success': True, 'redirect_url': url_for('admin_slide_resources')}),200
        
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception('Failed to add slide resource.')
            return jsonify({'success': False, 'message': 'something went wrong, Please try again later.'}),500
       
    return render_template('admin/add_slide_resource.html',admin_online=admin_online)



@app.route('/admin/edit-message-resource/<int:id>/', methods=['GET', 'POST'])
@admin_required 
def edit_message_resource(id, admin_online):
    if id <= 0:
        flash('Invalid resource ID.', 'error')
        return redirect(url_for('admin_message_resources'))

    resource = db.session.query(Resource).filter(
        Resource.resource_id == id,
        Resource.resource_type == 'text',
        Resource.resource_is_deleted == False
    ).first()

    if not resource:
        flash('Resource not found', 'error')
        return redirect(url_for('admin_message_resources'))

    if request.method == 'POST':
        try:
            
            title = request.form.get('title', '').strip()
            content = request.form.get('content', '').strip()
            content_json = normalize_tiptap_json(request.form.get('content_json', ''))
            resource_status = get_resource_status_from_form(resource.resource_status or 'published')

            # Validate title and content
            if not title:
                return jsonify({'success': False, 'message': 'Title cannot be empty.'}), 400
            if not content:
                return jsonify({'success': False, 'message': 'Content cannot be empty.'}), 400
            if not resource_status:
                return jsonify({'success': False, 'message': 'Invalid status selected.'}), 400
            
            # Sanitize inputs
            sanitized_title = title_cleaner.clean(title)
            sanitized_content = content_cleaner.clean(content)

            # Update resource
            resource.resource_title = str(sanitized_title) # Store sanitized title
            resource.resource_body = str(sanitized_content)  # Store sanitized HTML content
            resource.resource_content_json = content_json
            resource.resource_status = resource_status
            if resource_status == 'published' and not resource.resource_published_date:
                resource.resource_published_date = datetime.utcnow()

            db.session.commit()
            return jsonify({'success': True, 'redirect_url': url_for('admin_message_resources')}),200
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': 'An unexpected error occurred. Please try again later.'}),500

    return render_template('admin/edit_message_resource.html', resource=resource, admin_online=admin_online)



@app.route('/admin/edit-audio-resource/<int:id>/', methods=['GET','POST'])
@admin_required
def edit_audio_resource(id, admin_online):
    if id <= 0:
        flash('Invalid resource ID.', 'error')
        return redirect(url_for('admin_audio_resources'))
    
    resource = db.session.query(Resource).filter(
        Resource.resource_id == id,
        Resource.resource_type == 'audio',
        Resource.resource_is_deleted == False
    ).first()

    if not resource:
        flash('Resource not found', 'error')
        return redirect(url_for('admin_audio_resources'))

    if request.method == 'POST':
        try:
            
            # Retrieve data from the form
            title = request.form.get('title', '').strip()  # Audio title
            audio_url = request.form.get('audio_url', '').strip()  # Audio URL
            resource_status = get_resource_status_from_form(resource.resource_status or 'published')

            # Validate form inputs
            if not title:
                return jsonify({'success': False, 'message': 'Title cannot be empty.'}), 400
            if not audio_url or 'youtube.com/watch' not in audio_url:
               return jsonify({'success': False, 'message': 'Invalid or empty YouTube URL.'}), 400
            if not resource_status:
                return jsonify({'success': False, 'message': 'Invalid status selected.'}), 400

            audio_id = audio_url.split('v=')[1].split('&')[0] if 'v=' in audio_url else None

            if not audio_id:
                return jsonify({'success': False, 'message': 'Invalid YouTube URL format.'}), 400
            sanitized_title = title_cleaner.clean(title)

            # Update resource
            resource.resource_title = str(sanitized_title) 
            resource.resource_body = audio_id  
            resource.resource_status = resource_status
            if resource_status == 'published' and not resource.resource_published_date:
                resource.resource_published_date = datetime.utcnow()

            db.session.commit()
            return jsonify({'success': True, 'redirect_url': url_for('admin_audio_resources')}),200
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': 'An unexpected error occurred. Please try again later.'}),500

    return render_template('admin/edit_audio_resource.html', resource=resource, admin_online=admin_online)



@app.route('/admin/edit-slide-resource/<int:id>/', methods=['POST', 'GET'])
@admin_required
def edit_slide_resource(id, admin_online):
    if id <= 0:
        flash('Invalid resource ID.', 'error')
        return redirect(url_for('admin_slide_resources'))

    resource = db.session.query(Resource).filter(
        Resource.resource_id == id,
        Resource.resource_type == 'slide',
        Resource.resource_is_deleted == False
    ).first()

    if not resource:
        flash('Resource not found', 'error')
        return redirect(url_for('admin_slide_resources'))
    
    if request.method =='POST':    
        try:
            title = request.form.get('title', '').strip()
            content = request.form.get('content', '').strip()
            content_json = normalize_tiptap_json(request.form.get('content_json', ''))
            resource_status = get_resource_status_from_form(resource.resource_status or 'published')

            # Validate title and content
            if not title:
                return jsonify({'success': False, 'message': 'Title cannot be empty.'}), 400
            if not content:
                return jsonify({'success': False, 'message': 'Content cannot be empty.'}), 400
            if not resource_status:
                return jsonify({'success': False, 'message': 'Invalid status selected.'}), 400
            
            # Sanitize inputs
            sanitized_title = title_cleaner.clean(title)
            sanitized_content = content_cleaner.clean(content)

            resource.resource_title = str(sanitized_title) # Store sanitized title
            resource.resource_body = str(sanitized_content)  # Store sanitized HTML content
            resource.resource_content_json = content_json
            resource.resource_status = resource_status
            if resource_status == 'published' and not resource.resource_published_date:
                resource.resource_published_date = datetime.utcnow()

            # Update slide
            db.session.commit() 

            return jsonify({'success': True, 'redirect_url': url_for('admin_slide_resources')}),200
        
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': 'something went wrong, Please try again later.'}),500
       

    return render_template('admin/edit_slide_resource.html', resource=resource, admin_online=admin_online)


@app.route('/admin/resource-preview/<int:id>/')
@admin_required
def admin_resource_preview(id, admin_online):
    resource = Resource.query.filter(
        Resource.resource_id == id,
        Resource.resource_type.in_(STATUS_MANAGED_RESOURCE_TYPES),
        Resource.resource_is_deleted == False
    ).first_or_404()

    comments = db.session.query(Comment).filter(
        Comment.resource_id == resource.resource_id,
        Comment.comment_is_approve == True,
        Comment.comment_is_blocked == False
    ).order_by(Comment.comment_date.desc()).limit(20).all()
    for comment in comments:
        comment.formatted_date = comment.comment_date.strftime('%d-%b-%Y')

    template = 'user/dynamic_audios.html' if resource.resource_type == 'audio' else 'user/dynamic_messages.html'
    return render_template(template, resource=resource, user_token=None, comments=comments, admin_online=admin_online)


@app.route('/admin/update-resource-status/', methods=['POST'])
@admin_required
def update_resource_status(admin_online):
    data = get_json_payload()
    resource_id = data.get('id')
    status = str(data.get('status', '')).strip().lower()

    if not resource_id:
        return jsonify({'success': False, 'message': 'Resource ID is required.'}), 400
    if status not in RESOURCE_STATUSES:
        return jsonify({'success': False, 'message': 'Invalid status selected.'}), 400

    resource = Resource.query.filter(
        Resource.resource_id == resource_id,
        Resource.resource_type.in_(STATUS_MANAGED_RESOURCE_TYPES),
        Resource.resource_is_deleted == False
    ).first()

    if not resource:
        return jsonify({'success': False, 'message': 'Resource not found.'}), 404

    try:
        resource.resource_status = status
        if status == 'published' and not resource.resource_published_date:
            resource.resource_published_date = datetime.utcnow()
        db.session.commit()
        log_admin_action(
            'resource_status_updated',
            admin_online.admin_id,
            f'{resource.resource_type} resource #{resource.resource_id} moved to {status}.'
        )
        return jsonify({'success': True, 'message': f'Resource moved to {RESOURCE_STATUS_LABELS[status]}.'}), 200
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Unable to update resource status for resource %s', resource_id)
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again later.'}), 500


def _blog_upload_folder():
    return os.path.join(current_app.root_path, 'static', 'uploads', 'blogs')


def _blog_featured_image_url(resource):
    if not resource or not resource.resource_featured_image:
        return ''
    return url_for('static', filename=f'uploads/blogs/{resource.resource_featured_image}')


def _apply_blog_form(resource, requested_status):
    was_published = resource.resource_status == 'published'
    title = title_cleaner.clean(request.form.get('title', '').strip())
    content = request.form.get('content', '').strip()
    content_json = normalize_tiptap_json(request.form.get('content_json', ''))
    category = title_cleaner.clean(request.form.get('category', '').strip())
    remove_featured_image = request.form.get('remove_featured_image') == '1'

    if not title and requested_status == 'draft':
        title = 'Untitled draft'
    if not title:
        return 'Title is required.'
    if requested_status == 'published' and not sanitize_blog_html(content):
        return 'Content is required before publishing.'
    if requested_status not in BLOG_STATUSES:
        return 'Invalid blog status.'

    upload_folder = _blog_upload_folder()
    featured_image = request.files.get('featured_image')
    if featured_image and featured_image.filename:
        try:
            new_image = save_blog_image(featured_image, upload_folder)
        except ValueError as exc:
            return str(exc)
        delete_blog_image(resource.resource_featured_image, upload_folder)
        resource.resource_featured_image = new_image
    elif remove_featured_image:
        delete_blog_image(resource.resource_featured_image, upload_folder)
        resource.resource_featured_image = None

    resource.resource_title = str(title)
    if not was_published:
        resource.resource_slug = unique_blog_slug(title, None, resource.resource_id)
    resource.resource_body = sanitize_blog_html(content)
    resource.resource_content_json = content_json
    resource.resource_status = requested_status
    resource.resource_category = str(category)[:100] if category else None

    if requested_status == 'published':
        resource.resource_published_date = resource.resource_published_date or datetime.utcnow()

    return None


@app.route('/admin/blogs/')
@admin_required
def admin_blog_resources(admin_online):
    sort_order = request.args.get('sortOrder', 'desc').lower()
    month = request.args.get('month', 'all')
    title_query = request.args.get('title', '').strip()
    status = request.args.get('status', 'all').lower()
    page = request.args.get('page', 1, type=int)
    per_page = 25

    query = db.session.query(
        Resource,
        func.coalesce(func.count(Comment.comment_id), 0).label('comment_count')
    ).outerjoin(Comment, Resource.resource_id == Comment.resource_id).filter(
        Resource.resource_type == 'blog',
        Resource.resource_is_deleted == False
    ).group_by(Resource.resource_id)

    if status != 'all':
        if status not in BLOG_STATUSES:
            return "Invalid status provided", 400
        query = query.filter(Resource.resource_status == status)

    if month != 'all':
        try:
            month_int = int(month)
            if 1 <= month_int <= 12:
                query = query.filter(extract('month', Resource.resource_updated_date) == month_int)
            else:
                raise ValueError("Invalid month value")
        except ValueError:
            return "Invalid month provided", 400

    if title_query:
        query = query.filter(Resource.resource_title.ilike(f'%{title_query}%'))

    if sort_order == 'asc':
        query = query.order_by(Resource.resource_updated_date.asc())
    else:
        query = query.order_by(Resource.resource_updated_date.desc())

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return render_template(
        'admin/admin_blog_resources.html',
        blog_resources=pagination.items,
        pagination=pagination,
        base_url=url_for('admin_blog_resources'),
        admin_online=admin_online
    )


@app.route('/admin/blogs/new/', methods=['GET', 'POST'])
@admin_required
def add_blog_resource(admin_online):
    if request.method == 'POST':
        action = request.form.get('action', 'draft')
        requested_status = 'published' if action == 'publish' else 'draft'
        resource = Resource(resource_title='Untitled draft', resource_body='', resource_type='blog')

        try:
            error = _apply_blog_form(resource, requested_status)
            if error:
                return jsonify({'success': False, 'message': error}), 400

            db.session.add(resource)
            db.session.commit()
            log_admin_action('blog_created', admin_online.admin_id, f'Blog #{resource.resource_id} saved as {resource.resource_status}.')
            return jsonify({
                'success': True,
                'message': 'Blog post saved successfully.',
                'redirect_url': url_for('edit_blog_resource', id=resource.resource_id),
                'public_url': url_for('blog_detail', slug=resource.resource_slug, _external=True) if resource.resource_status == 'published' else None
            }), 200
        except ValueError as exc:
            db.session.rollback()
            return jsonify({'success': False, 'message': str(exc)}), 400
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Unable to create blog resource.')
            return jsonify({'success': False, 'message': 'Something went wrong. Please try again later.'}), 500

    return render_template(
        'admin/blog_editor.html',
        resource=None,
        admin_online=admin_online,
        editor_action=url_for('add_blog_resource'),
        preview_url=None,
        featured_image_url='',
    )


@app.route('/admin/blogs/edit/<int:id>/', methods=['GET', 'POST'])
@admin_required
def edit_blog_resource(id, admin_online):
    resource = Resource.query.filter(
        Resource.resource_id == id,
        Resource.resource_type == 'blog',
        Resource.resource_is_deleted == False
    ).first()

    if not resource:
        flash('Blog post not found.', 'error')
        return redirect(url_for('admin_blog_resources'))

    if request.method == 'POST':
        action = request.form.get('action', resource.resource_status)
        requested_status = 'published' if action == 'publish' else 'draft' if action == 'unpublish' else 'draft'
        if action == 'keep':
            requested_status = resource.resource_status

        try:
            error = _apply_blog_form(resource, requested_status)
            if error:
                return jsonify({'success': False, 'message': error}), 400

            db.session.commit()
            log_admin_action('blog_updated', admin_online.admin_id, f'Blog #{resource.resource_id} saved as {resource.resource_status}.')
            return jsonify({
                'success': True,
                'message': 'Blog post updated successfully.',
                'redirect_url': url_for('edit_blog_resource', id=resource.resource_id),
                'preview_url': url_for('admin_blog_preview', id=resource.resource_id),
                'public_url': url_for('blog_detail', slug=resource.resource_slug, _external=True) if resource.resource_status == 'published' else None
            }), 200
        except ValueError as exc:
            db.session.rollback()
            return jsonify({'success': False, 'message': str(exc)}), 400
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Unable to update blog resource %s', id)
            return jsonify({'success': False, 'message': 'Something went wrong. Please try again later.'}), 500

    return render_template(
        'admin/blog_editor.html',
        resource=resource,
        admin_online=admin_online,
        editor_action=url_for('edit_blog_resource', id=resource.resource_id),
        preview_url=url_for('admin_blog_preview', id=resource.resource_id),
        featured_image_url=_blog_featured_image_url(resource),
    )


@app.route('/admin/blogs/preview/<int:id>/')
@admin_required
def admin_blog_preview(id, admin_online):
    resource = Resource.query.filter(
        Resource.resource_id == id,
        Resource.resource_type == 'blog',
        Resource.resource_is_deleted == False
    ).first_or_404()
    description = blog_text_summary(resource, 300)
    return render_template(
        'admin/blog_preview.html',
        resource=resource,
        featured_image_url=_blog_featured_image_url(resource),
        copy_public_url=url_for('blog_detail', slug=resource.resource_slug, _external=True) if resource.resource_status == 'published' and resource.resource_slug else None,
        copy_content_text=blog_plain_text(resource),
        page_title=f'Preview: {resource.resource_title}',
        meta_description=description,
        canonical_url=url_for('admin_blog_preview', id=resource.resource_id, _external=True),
        og_title=resource.resource_title,
        og_description=description,
        og_image_url=url_for('static', filename=f'uploads/blogs/{resource.resource_featured_image}', _external=True) if resource.resource_featured_image else None,
    )


@app.route('/admin/blogs/copy-data/<int:id>/')
@admin_required
def admin_blog_copy_data(id, admin_online):
    resource = Resource.query.filter(
        Resource.resource_id == id,
        Resource.resource_type == 'blog',
        Resource.resource_is_deleted == False
    ).first()

    if not resource:
        return jsonify({'success': False, 'message': 'Blog post not found.'}), 404

    public_url = None
    if resource.resource_status == 'published' and resource.resource_slug:
        public_url = url_for('blog_detail', slug=resource.resource_slug, _external=True)

    return jsonify({
        'success': True,
        'public_url': public_url,
        'content': blog_plain_text(resource),
    }), 200


@app.route('/admin/blogs/delete/', methods=['POST'])
@admin_required
def delete_blog_resource(admin_online):
    data = get_json_payload()
    resource_id = data.get('id') or request.form.get('id')
    resource = Resource.query.filter(
        Resource.resource_id == resource_id,
        Resource.resource_type == 'blog',
        Resource.resource_is_deleted == False
    ).first()

    if not resource:
        return jsonify({'status': 'error', 'message': 'Blog post not found.'}), 404

    try:
        resource.resource_is_deleted = True
        db.session.commit()
        log_admin_action('blog_deleted', admin_online.admin_id, f'Blog #{resource.resource_id} archived.')
        return jsonify({'status': 'success', 'message': 'Blog post deleted successfully.'}), 200
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Unable to delete blog resource %s', resource_id)
        return jsonify({'status': 'error', 'message': 'Something went wrong. Please try again later.'}), 500


@app.route('/admin/blogs/publish/', methods=['POST'])
@admin_required
def publish_blog_resource(admin_online):
    data = get_json_payload()
    resource_id = data.get('id')
    resource = Resource.query.filter(
        Resource.resource_id == resource_id,
        Resource.resource_type == 'blog',
        Resource.resource_is_deleted == False
    ).first()

    if not resource:
        return jsonify({'success': False, 'message': 'Blog post not found.'}), 404
    if not resource.resource_body:
        return jsonify({'success': False, 'message': 'Content is required before publishing.'}), 400

    try:
        if not resource.resource_slug:
            resource.resource_slug = unique_blog_slug(resource.resource_title, None, resource.resource_id)
        resource.resource_status = 'published'
        resource.resource_published_date = resource.resource_published_date or datetime.utcnow()
        db.session.commit()
        log_admin_action('blog_published', admin_online.admin_id, f'Blog #{resource.resource_id} published.')
        return jsonify({
            'success': True,
            'message': 'Blog post published successfully.',
            'public_url': url_for('blog_detail', slug=resource.resource_slug, _external=True)
        }), 200
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Unable to publish blog resource %s', resource_id)
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again later.'}), 500


@app.route('/admin/blogs/unpublish/', methods=['POST'])
@admin_required
def unpublish_blog_resource(admin_online):
    data = get_json_payload()
    resource_id = data.get('id')
    resource = Resource.query.filter(
        Resource.resource_id == resource_id,
        Resource.resource_type == 'blog',
        Resource.resource_is_deleted == False
    ).first()

    if not resource:
        return jsonify({'success': False, 'message': 'Blog post not found.'}), 404

    try:
        resource.resource_status = 'draft'
        db.session.commit()
        log_admin_action('blog_unpublished', admin_online.admin_id, f'Blog #{resource.resource_id} moved to draft.')
        return jsonify({'success': True, 'message': 'Blog post moved to draft successfully.'}), 200
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Unable to unpublish blog resource %s', resource_id)
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again later.'}), 500


@app.route('/admin/blogs/upload-image/', methods=['POST'])
@admin_required
def admin_blog_editor_image_upload(admin_online):
    try:
        filename = save_blog_image(request.files.get('image'), _blog_upload_folder())
        if not filename:
            return jsonify({'success': False, 'message': 'Image is required.'}), 400
        return jsonify({
            'success': True,
            'url': url_for('static', filename=f'uploads/blogs/{filename}')
        }), 200
    except ValueError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400
    except Exception:
        return jsonify({'success': False, 'message': 'Image upload failed. Please try again.'}), 500




@app.route('/admin/admin-prayer-requests/')
@admin_required
def admin_prayer_requests(admin_online):

    sort_order = request.args.get('sortOrder', 'desc').lower()
    month = request.args.get('month', 'all')
    page = request.args.get('page', 1, type=int)  # Get the page number, default is 1
    per_page = 25


    query = db.session.query(PrayerRequest)

    # Filter by month
    if month != 'all':
        try:
            month_int = int(month)
            if 1 <= month_int <= 12:
                query = query.filter(extract('month', PrayerRequest.pr_date) == month_int)
            else:
                raise ValueError("Invalid month value")
        except ValueError:
            return "Invalid month provided", 400

    # Sort based on the selected order
    if sort_order == 'asc':
        query = query.order_by(PrayerRequest.pr_date.asc())
    else:
        query = query.order_by(PrayerRequest.pr_date.desc())


    # Apply pagination
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Fetch paginated results
    prayer_requests = pagination.items
    base_url = url_for('admin_prayer_requests')

    return render_template('admin/admin_prayer_requests.html',prayer_requests=prayer_requests, pagination=pagination, base_url=base_url, admin_online=admin_online)



@app.route('/admin/delete-prayer-request/', methods=['POST'])
@admin_required
def delete_prayer_request(admin_online):
    data = get_json_payload()
    id = data.get('id')  # Expecting JSON data

    if not id:
        return jsonify({'status': 'error', 'message': 'Prayer Request ID is required.'}), 400

    prayer_request = db.session.query(PrayerRequest).filter_by(pr_id=id).first()
    
    if not prayer_request:
        return jsonify({'status': 'error', 'message': 'Prayer Request not found.'}), 404
    
    try:
        db.session.delete(prayer_request)
        db.session.commit()

        return jsonify({'status': 'success', 'message': 'Prayer Request deleted successfully.'}), 200
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': 'Something went wrong, Please try again later'}), 500



@app.route('/admin/admin-upcoming-events/')
@admin_required
def admin_upcoming_events(admin_online):
    # Fetch  results
    events = db.session.query(Event).order_by(Event.event_updated_date.desc())
    for event in events:
        event.formated_date = format_event_date(event.event_date)
    return render_template('admin/admin_upcoming_events.html', events=events, admin_online=admin_online)


@app.route('/admin/add-new-event/', methods=['GET', 'POST'])
@admin_required
def add_new_event(admin_online):
    if request.method == 'POST':
        try:
            theme = request.form.get('theme', '').strip()
            event_date = request.form.get('event_date', '').strip()
            event_time = request.form.get('event_time', '').strip()
            event_venue = request.form.get('event_venue', '').strip() or None
            prayer = request.form.get('prayer', '').strip() or None

            if not theme:
                return jsonify({'success': False, 'message': 'Theme cannot be empty.'}), 400
            if not event_date:
                return jsonify({'success': False, 'message': 'Event date cannot be empty.'}), 400
            if not event_time:
                return jsonify({'success': False, 'message': 'Event time cannot be empty.'}), 400

            try:
                event_date = datetime.strptime(event_date, '%Y-%m-%d').date()
                event_time = datetime.strptime(event_time, '%I:%M %p').time()
            except ValueError:
                return jsonify({'success': False, 'message': 'Invalid date or time format.'}), 400

            sanitized_theme = title_cleaner.clean(theme)
            sanitized_venue = title_cleaner.clean(event_venue) if event_venue else None
            sanitized_prayer = title_cleaner.clean(prayer) if prayer else None

            upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'events')
            try:
                flyer_filename = save_event_flyer(request.files.get('flyer'), upload_folder)
            except ValueError as ve:
                return jsonify({'success': False, 'message': str(ve)}), 400

            new_event = Event(
                event_theme=sanitized_theme,
                event_date=event_date,
                event_time=event_time,
                event_venue=sanitized_venue,
                event_description=sanitized_prayer,
                event_flyer_filename=flyer_filename,
            )

            db.session.add(new_event)
            db.session.commit()

            return jsonify({'success': True, 'redirect_url': url_for('admin_upcoming_events')}), 200

        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': 'something went wrong, Please try again later.'}), 500

    return render_template('admin/add_new_event.html', admin_online=admin_online)


@app.route('/admin/edit_event/<int:id>/', methods=['GET', 'POST'])
@admin_required
def edit_event(id, admin_online):
    if id <= 0:
        flash('Invalid Event ID.', 'error')
        return redirect(url_for('admin_upcoming_events'))

    event = db.session.query(Event).filter_by(event_id=id).first()

    if not event:
        flash('Event not found', 'error')
        return redirect(url_for('admin_upcoming_events'))

    if request.method == 'POST':
        try:
            theme = request.form.get('theme', '').strip()
            event_date = request.form.get('event_date', '').strip()
            event_time = request.form.get('event_time', '').strip()
            event_venue = request.form.get('event_venue', '').strip() or None
            prayer = request.form.get('prayer', '').strip() or None
            # Checkbox in the edit form — lets the admin clear a flyer
            # without necessarily uploading a replacement
            remove_flyer = request.form.get('remove_flyer') == 'on'

            if not theme:
                return jsonify({'success': False, 'message': 'Theme cannot be empty.'}), 400
            if not event_date:
                return jsonify({'success': False, 'message': 'Event date cannot be empty.'}), 400
            if not event_time:
                return jsonify({'success': False, 'message': 'Event time cannot be empty.'}), 400

            try:
                event_date = datetime.strptime(event_date, '%Y-%m-%d').date()
                event_time = datetime.strptime(event_time, '%I:%M %p').time()
            except ValueError:
                return jsonify({'success': False, 'message': 'Invalid date or time format.'}), 400

            sanitized_theme = title_cleaner.clean(theme)
            sanitized_venue = title_cleaner.clean(event_venue) if event_venue else None
            sanitized_prayer = title_cleaner.clean(prayer) if prayer else None

            upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'events')
            try:
                new_flyer_filename = save_event_flyer(request.files.get('flyer'), upload_folder)
            except ValueError as ve:
                return jsonify({'success': False, 'message': str(ve)}), 400

            if new_flyer_filename:
                # A replacement was uploaded — remove the old file first
                delete_event_flyer(event.event_flyer_filename, upload_folder)
                event.event_flyer_filename = new_flyer_filename
            elif remove_flyer:
                delete_event_flyer(event.event_flyer_filename, upload_folder)
                event.event_flyer_filename = None
            # else: no new file and no removal requested — leave it untouched

            event.event_theme = sanitized_theme
            event.event_date = event_date
            event.event_time = event_time
            event.event_venue = sanitized_venue
            event.event_description = sanitized_prayer

            db.session.commit()

            return jsonify({'success': True, 'redirect_url': url_for('admin_upcoming_events')}), 200

        except Exception as e:
            db.session.rollback()
            current_app.logger.exception('Failed to edit event %s.', id)
            return jsonify({'success': False, 'message': 'something went wrong, Please try again later.'}), 500

    return render_template('admin/edit_event.html', event=event, admin_online=admin_online)


@app.route('/admin/delete-event/', methods=['POST'])
@admin_required
def delete_event(admin_online):
    data = get_json_payload()
    id = data.get('id')

    if not id:
        return jsonify({'status': 'error', 'message': 'Event ID is required.'}), 400

    event = db.session.query(Event).filter_by(event_id=id).first()

    if not event:
        return jsonify({'status': 'error', 'message': 'Event not found.'}), 404

    try:
        event_theme = event.event_theme
        upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'events')
        delete_event_flyer(event.event_flyer_filename, upload_folder)

        db.session.delete(event)
        db.session.commit()
        log_admin_action(
            'event_deleted',
            admin_online.admin_id,
            f'Event #{id} deleted: {event_theme}.'
        )

        return jsonify({'status': 'success', 'message': 'Event deleted successfully.'}), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('Unable to delete event %s', id)
        return jsonify({'status': 'error', 'message': 'Something went wrong, Please try again later'}), 500


@app.route('/admin/admin-comments/')
@admin_required
def admin_comments(admin_online):

    sort_order = request.args.get('sortOrder', 'desc').lower()
    month = request.args.get('month', 'all')
    status = request.args.get('status', 'all').strip().lower()
    page = request.args.get('page', 1, type=int)  # Get the page number, default is 1
    per_page = 25


    query = db.session.query(Comment, Resource).join(Resource).filter( Resource.resource_is_deleted == False)
    if status != 'all':
        if status not in COMMENT_STATUSES:
            return "Invalid status provided", 400
        if status == 'approved':
            query = query.filter(Comment.comment_is_approve == True, Comment.comment_is_blocked == False)
        elif status == 'unapproved':
            query = query.filter(Comment.comment_is_approve == False, Comment.comment_is_blocked == False)
        else:
            query = query.filter(Comment.comment_is_blocked == True)

    # Filter by month
    if month != 'all':
        try:
            month_int = int(month)
            if 1 <= month_int <= 12:
                query = query.filter(extract('month', Comment.comment_date) == month_int)
            else:
                raise ValueError("Invalid month value")
        except ValueError:
            return "Invalid month provided", 400

    # Sort based on the selected order
    if sort_order == 'asc':
        query = query.order_by(Comment.comment_date.asc())
    else:
        query = query.order_by(Comment.comment_date.desc())

    # Apply pagination
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Fetch paginated results
    comments = pagination.items
    base_url = url_for('admin_comments')

    return render_template(
        'admin/admin_comments.html',
        comments=comments,
        pagination=pagination,
        base_url=base_url,
        admin_online=admin_online,
        get_comment_status=get_comment_status,
        comment_status_labels=COMMENT_STATUS_LABELS
    )


@app.route('/admin/update-comment-status/', methods=['POST'])
@admin_required
def admin_update_comment_status(admin_online):
    data = get_json_payload()
    comment_id = data.get('id')
    status = str(data.get('status', '')).strip().lower()

    if not comment_id:
        return jsonify({'success': False, 'message': 'Comment ID is required.'}), 400
    if status not in COMMENT_STATUSES:
        return jsonify({'success': False, 'message': 'Invalid status selected.'}), 400

    comment = db.session.query(Comment).filter_by(comment_id=comment_id).first()
    if not comment:
        return jsonify({'success': False, 'message': 'Comment not found.'}), 404

    old_status = get_comment_status(comment)
    try:
        if status == 'approved':
            comment.comment_is_approve = True
            comment.comment_is_blocked = False
        elif status == 'unapproved':
            comment.comment_is_approve = False
            comment.comment_is_blocked = False
        else:
            comment.comment_is_approve = False
            comment.comment_is_blocked = True

        db.session.commit()
        log_admin_action(
            'comment_status_updated',
            admin_online.admin_id,
            f'Comment #{comment.comment_id} moved from {old_status} to {status}.'
        )
        return jsonify({'success': True, 'message': f'Comment moved to {COMMENT_STATUS_LABELS[status]}.'}), 200
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Unable to update comment status for comment %s', comment_id)
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again later.'}), 500



@app.route('/admin/delete-comment/', methods=['POST'])
@admin_required
def admin_delete_comment(admin_online):
    data = get_json_payload()
    id = data.get('id')  # Expecting JSON data

    if not id:
        return jsonify({'status': 'error', 'message': 'Comment ID is required.'}), 400

    comment = db.session.query(Comment).filter_by(comment_id=id).first()
    
    if not comment:
        return jsonify({'status': 'error', 'message': 'Comment not found.'}), 404
    
    try:
        resource_id = comment.resource_id
        db.session.delete(comment)
        db.session.commit()
        log_admin_action(
            'comment_deleted',
            admin_online.admin_id,
            f'Comment #{id} deleted from resource #{resource_id}.'
        )

        return jsonify({'status': 'success', 'message': 'Comment deleted successfully.'}), 200
    
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('Unable to delete comment %s', id)
        return jsonify({'status': 'error', 'message': 'Something went wrong, Please try again later'}), 500



@app.route('/admin/subscribers/')
@admin_required
def admin_subscribers(admin_online):
    sort_order = request.args.get('sortOrder', 'desc')
    subscriber_status = request.args.get('subscriberStatus', 'all')
    email_query = request.args.get('email', '').strip()
    page = request.args.get('page', 1, type=int) 
    per_page = 25

    query = db.session.query(NewsletterSubscriber)

    # Filtering by status
    if subscriber_status != 'all':
        query = query.filter_by(subscriber_status=subscriber_status)

    # Sorting based on the selected order
    if sort_order == 'asc':
        query = query.order_by(NewsletterSubscriber.subscriber_date_joined.asc())
    else:
        query = query.order_by(NewsletterSubscriber.subscriber_date_joined.desc())

    # Filtering by email (if search query is provided)
    if email_query:
        query = query.filter(NewsletterSubscriber.subscriber_email.ilike(f'%{email_query}%'))

     # Apply pagination
    
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Fetch paginated results
    subscribers = pagination.items
    base_url = url_for('admin_subscribers')
    return render_template('admin/admin_subscribers.html', base_url=base_url, subscribers=subscribers, pagination=pagination, admin_online=admin_online)



@app.route('/admin/subscribers-email/')
@admin_required
def subscribers_email(admin_online):
    sort_order = request.args.get('sortOrder', 'desc')
    subscriber_status = request.args.get('subscriberStatus', 'all')
    page = request.args.get('page', 1, type=int)  # New line
    per_page = 25  # Adjust as needed

    query = db.session.query(NewsletterSubscriber.subscriber_email)

    if subscriber_status != 'all':
        query = query.filter_by(subscriber_status=subscriber_status)

    if sort_order == 'asc':
        query = query.order_by(NewsletterSubscriber.subscriber_date_joined.asc())
    else:
        query = query.order_by(NewsletterSubscriber.subscriber_date_joined.desc())

    # Apply pagination
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    emails = pagination.items

    # Return just the emails (each is a tuple with one element)
    return jsonify([email[0] for email in emails])



@app.route('/admin/delete-subscriber/', methods=['POST'])
@admin_required
def delete_subscriber(admin_online):
    data = get_json_payload()
    id = data.get('id')  # Expecting JSON data

    if not id:
        return jsonify({'status': 'error', 'message': "Subscriber's ID is required."}), 400

    subscriber = db.session.query(NewsletterSubscriber).filter_by(subscriber_id=id).first()
    
    if not subscriber:
        return jsonify({'status': 'error', 'message': 'Subscriber not found.'}), 404
    
    try:
        db.session.delete(subscriber)
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'Subscriber deleted successfully.'}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500




@app.route('/admin/admin-notification/')
@admin_required
def admin_notifications(admin_online):

    page = request.args.get('page', 1, type=int)  # Get the page number, default is 1
    per_page = 20

    query = db.session.query(Notification).order_by(Notification.notification_date.desc())

    # Apply pagination
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Fetch paginated results
    notifications = pagination.items
    base_url = url_for('admin_notifications')

    return render_template('admin/admin_notification.html',notifications = notifications, pagination=pagination, base_url=base_url, admin_online=admin_online)



@app.route('/admin/delete-notification/', methods=['POST'])
@admin_required
def delete_notification(admin_online):
    data = get_json_payload()
    id = data.get('id')  # Expecting JSON data
    notification = db.session.query(Notification).filter_by(notification_id=id).first()
    
    if not notification:
        return jsonify({'status': 'error', 'message': 'Notification not found.'}), 404
    
    try:
        db.session.delete(notification)
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'Notification deleted successfully.'}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/mark-as-read-notification/', methods=['POST'])
@admin_required
def mark_read_notification(admin_online):
    data = get_json_payload()
    id = data.get('id')  # Expecting JSON data
    notification = db.session.query(Notification).filter_by(notification_id=id).first()
    
    if not notification:
        return jsonify({'status': 'error', 'message': 'Notification not found.'}), 404
    
    try:
        notification.notification_is_read = True
        db.session.commit()
        return jsonify({'status': 'success'}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500



@app.route('/admin/delete-bulk/notifications/', methods=['POST'])
@admin_required
def delete_bulk_notifications(admin_online):
    try:
        # Parse the JSON payload
        data = get_json_payload()
        notification_ids = data.get('notification_ids', [])

        # Validate input
        if not notification_ids:
            return jsonify({'success': False, 'message': 'No notifications selected for deletion.'}), 400

        # Perform deletion
        Notification.query.filter(Notification.notification_id.in_(notification_ids)).delete(synchronize_session=False)
        db.session.commit()

        return jsonify({'success': True, 'message': 'Notification(s) deleted successfully.'}), 200

    except Exception as e:
        current_app.logger.exception('Error during bulk notification deletion.')
        db.session.rollback()
        return jsonify({'success': False, 'message': 'An error occurred while deleting notifications.'}), 500


@app.route('/admin/sign-up/', methods=['POST', 'GET'])
def admin_sign_up():
    if os.getenv('FLASK_ENV') == 'production':
        abort(404)  # doesn't even reveal the route exists in production

    if request.method == 'POST':
        fullname=request.form.get('fullname', '').strip()
        username=request.form.get('username', '').strip()
        pass1=request.form.get('password', '').strip()
        pass2=request.form.get('cpassword', '').strip()
        
        if not all([fullname, username, pass1, pass2]):
            return jsonify({'success': False, 'message': 'All fields are required.'}), 400
        
        elif pass1!=pass2:
            return jsonify({'success': False, 'message': 'The two passwords must match.'}), 400
        
        else:
            hashed=generate_password_hash(pass1)
            data=Admin(
                admin_fullname = fullname,
                admin_username = username,
                admin_password=hashed,
                admin_role= 'Admin'
            )
            try:
                db.session.add(data)
                db.session.commit()
                return jsonify({'success': True, 'message': 'Successfully Signed up. Plese Login in.', 'redirect_url': url_for('admin_login')}), 200
            except Exception as e:
                db.session.rollback()
                return jsonify({'success': False, 'message': 'Something went wrong. Please try again later.'}), 500  
    return render_template('admin/sign_up.html')    
    

STATUS_COLORS = {
    'pending': '#ffc107', 'confirmed': '#146c43', 'cancelled': '#b02a37',
    'completed': '#0a58ca', 'no_show': '#6c757d', 'rescheduled': '#6f42c1',
}
DONATION_STATUS_COLORS = {
    'pending': '#ffc107', 'completed': '#146c43', 'failed': '#b02a37', 'refunded': '#6c757d',
}
BOOKING_STATUSES = ('pending', 'confirmed', 'cancelled', 'completed', 'no_show', 'rescheduled')


# Appointments
@app.route('/admin/appointments/')
@admin_required
def admin_appointments(admin_online):
    status = request.args.get('status', 'all').strip()
    date_str = request.args.get('date', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    sort_order = request.args.get('sortOrder', 'desc').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 20
 
    query = db.session.query(Booking).join(Booker)
 
    if status != 'all':
        query = query.filter(Booking.booking_status == status)
 
    if date_str:
        try:
            filter_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            query = query.filter(Booking.booking_date == filter_date)
        except ValueError:
            pass
    if date_from:
        try:
            df = datetime.strptime(date_from, '%Y-%m-%d').date()
            query = query.filter(Booking.booking_date >= df)
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.strptime(date_to, '%Y-%m-%d').date()
            query = query.filter(Booking.booking_date <= dt)
        except ValueError:
            pass
 
    if sort_order == 'desc':
        query = query.order_by(Booking.booking_date.desc(), Booking.booking_start_time.desc())
    else:
        query = query.order_by(Booking.booking_date.asc(), Booking.booking_start_time.asc())
 
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    bookings = pagination.items
    base_url = url_for('admin_appointments')
 
    return render_template(
        'admin/admin_appointments.html',
        bookings=bookings,
        pagination=pagination,
        base_url=base_url,
        status=status,
        date_filter=date_str,
        date_from=date_from,
        date_to=date_to,
        sort_order=sort_order,
        booking_statuses=BOOKING_STATUSES,
        status_colors=STATUS_COLORS,
        admin_online=admin_online
    )
 
 

@app.route('/admin/appointments/update/', methods=['POST'])
@admin_required
def admin_update_appointment(admin_online):
    data = request.get_json(silent=True) or {}
    booking_id = data.get('id')
    status = data.get('status')
    notes = data.get('notes')
    date_value = (data.get('date') or '').strip()
    start_time_value = (data.get('start_time') or '').strip()

    booking = db.session.query(Booking).filter_by(booking_id=booking_id).first()
    if not booking:
        return jsonify({'success': False, 'message': 'Booking not found.'}), 404

    if status and status not in BOOKING_STATUSES:
        return jsonify({'success': False, 'message': 'Invalid status.'}), 400

    new_date = booking.booking_date
    new_start = booking.booking_start_time
    if date_value or start_time_value:
        if not date_value or not start_time_value:
            return jsonify({'success': False, 'message': 'Please provide both date and start time to reschedule.'}), 400

        try:
            new_date = datetime.strptime(date_value, '%Y-%m-%d').date()
            new_start = datetime.strptime(start_time_value, '%H:%M').time()
        except ValueError:
            return jsonify({'success': False, 'message': 'Please select a valid date and time.'}), 400

    new_end = (datetime.combine(new_date, new_start) + timedelta(minutes=SESSION_DURATION_MINUTES)).time()
    is_reschedule = (
        new_date != booking.booking_date
        or new_start != booking.booking_start_time
    )

    if is_reschedule and booking.booking_status in ('cancelled', 'completed', 'no_show'):
        return jsonify({'success': False, 'message': 'This appointment can no longer be rescheduled.'}), 400

    if is_reschedule and status == 'cancelled':
        return jsonify({'success': False, 'message': 'Cancel the appointment without changing the date or time.'}), 400

    if is_reschedule:
        now_wat = datetime.utcnow() + timedelta(hours=1)
        if datetime.combine(new_date, new_start) <= now_wat:
            return jsonify({'success': False, 'message': 'Please choose a future appointment time.'}), 400

        if not is_slot_available(new_date, new_start, exclude_booking_id=booking.booking_id):
            return jsonify({'success': False, 'message': 'That slot is no longer available.'}), 409

    try:
        old_date = booking.booking_date
        old_start = booking.booking_start_time
        old_end = booking.booking_end_time

        if status:
            if status == 'cancelled' and booking.booking_status != 'cancelled':
                try:
                    delete_calendar_event(booking.booking_calendar_event_id)
                    booking.booking_calendar_event_id = None
                    booking.booking_meet_link = None
                except Exception:
                    current_app.logger.exception(
                        'Failed to delete Google Calendar event while cancelling appointment %s.',
                        booking.booking_id
                    )
            booking.booking_status = status
        if is_reschedule:
            try:
                if booking.booking_calendar_event_id:
                    update_calendar_event(
                        booking.booking_calendar_event_id,
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
                    booking.booking_calendar_event_id = calendar_result.get('event_id')
                    booking.booking_meet_link = calendar_result.get('meet_link')
            except Exception:
                db.session.rollback()
                current_app.logger.exception(
                    'Failed to update Google Calendar event while rescheduling appointment %s.',
                    booking.booking_id
                )
                return jsonify({'success': False, 'message': 'Could not update Google Calendar. Appointment was not rescheduled.'}), 502

            booking.booking_date = new_date
            booking.booking_start_time = new_start
            booking.booking_end_time = new_end
            booking.booking_status = 'rescheduled'
        if notes is not None:
            booking.booking_notes = str(escape(notes))
        db.session.commit()

        if is_reschedule:
            log_admin_action(
                'admin_appointment_rescheduled',
                admin_online.admin_id,
                (
                    f'Booking {booking.booking_id} moved from '
                    f'{old_date.isoformat()} {old_start.strftime("%H:%M")}-{old_end.strftime("%H:%M")} '
                    f'to {booking.booking_date.isoformat()} {booking.booking_start_time.strftime("%H:%M")}-{booking.booking_end_time.strftime("%H:%M")}.'
                )
            )
            try:
                send_booking_rescheduled_confirmation(
                    booking,
                    old_date=old_date,
                    old_start=old_start,
                    old_end=old_end
                )
            except Exception:
                current_app.logger.exception(
                    'Failed to send client reschedule email for appointment %s.',
                    booking.booking_id
                )
            try:
                send_counselor_notification('rescheduled', booking)
            except Exception:
                current_app.logger.exception(
                    'Failed to send counselor reschedule email for appointment %s.',
                    booking.booking_id
                )

        message = 'Appointment rescheduled.' if is_reschedule else 'Appointment updated.'
        return jsonify({'success': True, 'message': message}), 200
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again.'}), 500


@app.route('/admin/appointments/delete/', methods=['POST'])
@admin_required
def admin_delete_appointment(admin_online):
    data = request.get_json(silent=True) or {}
    booking_id = data.get('id')

    booking = db.session.query(Booking).filter_by(booking_id=booking_id).first()
    if not booking:
        return jsonify({'success': False, 'message': 'Appointment not found.'}), 404

    try:
        try:
            delete_calendar_event(booking.booking_calendar_event_id)
        except Exception:
            current_app.logger.exception(
                'Failed to delete Google Calendar event while deleting appointment %s.',
                booking.booking_id
            )
        db.session.delete(booking)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Appointment deleted.'}), 200
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again.'}), 500


@app.route('/admin/appointments/delete-range/', methods=['POST'])
@admin_required
def admin_delete_appointments_range(admin_online):
    data = request.get_json(silent=True) or {}
    date_from = (data.get('date_from') or '').strip()
    date_to = (data.get('date_to') or '').strip()

    try:
        df = datetime.strptime(date_from, '%Y-%m-%d').date()
        dt = datetime.strptime(date_to, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'success': False, 'message': 'Please select a valid from and to date.'}), 400

    if df > dt:
        return jsonify({'success': False, 'message': 'From date cannot be after to date.'}), 400

    bookings = db.session.query(Booking).filter(
        Booking.booking_date >= df,
        Booking.booking_date <= dt
    ).all()

    if not bookings:
        return jsonify({'success': False, 'message': 'No appointments found in that date range.'}), 404

    try:
        for booking in bookings:
            try:
                delete_calendar_event(booking.booking_calendar_event_id)
            except Exception:
                current_app.logger.exception(
                    'Failed to delete Google Calendar event while deleting appointment %s in range.',
                    booking.booking_id
                )
            db.session.delete(booking)
        db.session.commit()
        return jsonify({'success': True, 'message': f'Deleted {len(bookings)} appointment record(s).'}), 200
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again.'}), 500

# Donations
@app.route('/admin/donations/')
@admin_required
def admin_donations(admin_online):
    gateway = request.args.get('gateway', '').strip()
    status = request.args.get('status', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 25

    query = db.session.query(Donation)

    if gateway:
        query = query.filter(Donation.donation_gateway == gateway)
    if status:
        query = query.filter(Donation.donation_status == status)
    if date_from:
        try:
            df = datetime.strptime(date_from, '%Y-%m-%d').date()
            query = query.filter(func.date(Donation.donation_date_added) >= df)
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.strptime(date_to, '%Y-%m-%d').date()
            query = query.filter(func.date(Donation.donation_date_added) <= dt)
        except ValueError:
            pass

    query = query.order_by(Donation.donation_date_added.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    donations = pagination.items
    base_url = url_for('admin_donations')

    currency_totals_raw = db.session.query(
        Donation.donation_currency,
        func.coalesce(func.sum(Donation.donation_amount), 0),
        func.count(Donation.donation_id)
    ).filter(Donation.donation_status == 'completed').group_by(Donation.donation_currency).all()

    currency_totals = [
        {'currency': c, 'total': total, 'count': count}
        for c, total, count in currency_totals_raw
    ]
    total_completed_count = sum(ct['count'] for ct in currency_totals)

    return render_template(
        'admin/admin_donations.html',
        donations=donations,
        pagination=pagination,
        base_url=base_url,
        currency_totals=currency_totals,
        total_completed_count=total_completed_count,
        donation_status_colors=DONATION_STATUS_COLORS,
        gateway=gateway,
        status=status,
        date_from=date_from,
        date_to=date_to,
        admin_online=admin_online
    )

# Availability (working hours + blocked dates)
@app.route('/admin/availability/')
@admin_required
def admin_availability(admin_online):
    existing = {wh.wh_day_of_week: wh for wh in db.session.query(WorkingHours).all()}
    day_names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

    working_hours = []
    for i, day_name in enumerate(day_names):
        wh = existing.get(i)
        working_hours.append({
            'day_of_week': i,
            'day_name': day_name,
            'start_time': wh.wh_start_time.strftime('%H:%M') if wh else '09:00',
            'end_time': wh.wh_end_time.strftime('%H:%M') if wh else '17:00',
            'is_active': wh.wh_is_active if wh else False,
        })

    blocked_dates = db.session.query(BlockedDate).order_by(BlockedDate.bd_date.asc()).all()

    recurring_blocks_raw = db.session.query(RecurringUnavailability).order_by(
        RecurringUnavailability.ru_day_of_week.asc(),
        RecurringUnavailability.ru_start_time.asc()
    ).all()
    recurring_blocks = [{
        'id': rb.ru_id,
        'day_of_week': rb.ru_day_of_week,
        'day_name': day_names[rb.ru_day_of_week],
        'start_time': rb.ru_start_time.strftime('%H:%M'),
        'end_time': rb.ru_end_time.strftime('%H:%M'),
        'reason': rb.ru_reason,
    } for rb in recurring_blocks_raw]

    return render_template(
        'admin/admin_availability.html',
        working_hours=working_hours,
        blocked_dates=blocked_dates,
        recurring_blocks=recurring_blocks,
        day_names=day_names,
        admin_online=admin_online
    )


@app.route('/admin/availability/recurring-blocks/', methods=['POST'])
@admin_required
def admin_add_recurring_block(admin_online):
    data = request.get_json(silent=True) or {}
 
    try:
        day_of_week = int(data.get('day_of_week'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Invalid day.'}), 400
 
    if not (0 <= day_of_week <= 6):
        return jsonify({'success': False, 'message': 'Invalid day.'}), 400
 
    try:
        start_time = datetime.strptime(data.get('start_time', ''), '%H:%M').time()
        end_time = datetime.strptime(data.get('end_time', ''), '%H:%M').time()
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid time format.'}), 400
 
    if start_time >= end_time:
        return jsonify({'success': False, 'message': 'Start time must be before end time.'}), 400
 
    reason = str(escape((data.get('reason') or '').strip())) or None
 
    # Scan every future, non-cancelled booking and flag any that:
    #   (a) fall on the same day of week, AND
    #   (b) overlap the new block's time range at all
    today = datetime.utcnow().date()
    future_bookings = db.session.query(Booking).join(Booker).filter(
        Booking.booking_date >= today,
        Booking.booking_status.notin_(['cancelled', 'completed'])
    ).all()
 
    blk_start = datetime.combine(datetime.min, start_time)
    blk_end = datetime.combine(datetime.min, end_time)
 
    conflicting_bookings = []
    for b in future_bookings:
        booking_dow = (b.booking_date.weekday() + 1) % 7  # Mon=0 -> Sunday=0 convention
        if booking_dow != day_of_week:
            continue
        b_start = datetime.combine(datetime.min, b.booking_start_time)
        b_end = datetime.combine(datetime.min, b.booking_end_time)
        if b_start < blk_end and b_end > blk_start:
            conflicting_bookings.append(b)
 
    try:
        rb = RecurringUnavailability(
            ru_day_of_week=day_of_week,
            ru_start_time=start_time,
            ru_end_time=end_time,
            ru_reason=reason,
        )
        db.session.add(rb)
        db.session.commit()
        return jsonify({
            'success': True,
            'block': {
                'id': rb.ru_id,
                'day_of_week': rb.ru_day_of_week,
                'start_time': start_time.strftime('%H:%M'),
                'end_time': end_time.strftime('%H:%M'),
                'reason': rb.ru_reason,
            },
            'conflicts': [
                {
                    'name': b.booker.booker_name,
                    'date': b.booking_date.strftime('%b %d, %Y'),
                    'time': b.booking_start_time.strftime('%H:%M'),
                    'status': b.booking_status,
                }
                for b in conflicting_bookings
            ]
        }), 200
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again.'}), 500
    

@app.route('/admin/availability/recurring-blocks/delete/', methods=['POST'])
@admin_required
def admin_delete_recurring_block(admin_online):
    data = request.get_json(silent=True) or {}
    rb_id = data.get('id')

    rb = db.session.query(RecurringUnavailability).filter_by(ru_id=rb_id).first()
    if not rb:
        return jsonify({'success': False, 'message': 'Not found.'}), 404

    try:
        db.session.delete(rb)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Removed.'}), 200
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again.'}), 500


@app.route('/admin/availability/working-hours/', methods=['POST'])
@admin_required
def admin_update_working_hours(admin_online):
    data = request.get_json(silent=True) or {}

    try:
        day_of_week = int(data.get('day_of_week'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Invalid day.'}), 400

    if not (0 <= day_of_week <= 6):
        return jsonify({'success': False, 'message': 'Invalid day.'}), 400

    try:
        start_time = datetime.strptime(data.get('start_time', ''), '%H:%M').time()
        end_time = datetime.strptime(data.get('end_time', ''), '%H:%M').time()
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid time format.'}), 400

    is_active = bool(data.get('is_active'))

    wh = db.session.query(WorkingHours).filter_by(wh_day_of_week=day_of_week).first()
    try:
        if wh:
            wh.wh_start_time = start_time
            wh.wh_end_time = end_time
            wh.wh_is_active = is_active
        else:
            wh = WorkingHours(
                wh_day_of_week=day_of_week,
                wh_start_time=start_time,
                wh_end_time=end_time,
                wh_is_active=is_active,
            )
            db.session.add(wh)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Working hours saved.'}), 200
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again.'}), 500

@app.route('/admin/availability/blocked-dates/', methods=['POST'])
@admin_required
def admin_add_blocked_date(admin_online):
    data = request.get_json(silent=True) or {}
    date_str = (data.get('date') or '').strip()
    reason = str(escape((data.get('reason') or '').strip())) or None

    try:
        blocked_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid date.'}), 400

    existing = db.session.query(BlockedDate).filter_by(bd_date=blocked_date).first()
    if existing:
        return jsonify({'success': False, 'message': 'That date is already blocked.'}), 409

    # Check for bookings that already exist on this date — blocking it
    # doesn't touch them, but Grace needs to know they're there.
    conflicting_bookings = db.session.query(Booking).join(Booker).filter(
        Booking.booking_date == blocked_date,
        Booking.booking_status.notin_(['cancelled', 'completed'])
    ).all()
    try:
        bd = BlockedDate(bd_date=blocked_date, bd_reason=reason)
        db.session.add(bd)
        db.session.commit()
        return jsonify({
            'success': True,
            'blocked_date': {'id': bd.bd_id, 'date': bd.bd_date.isoformat(), 'reason': bd.bd_reason},
            'conflicts': [
                {
                    'name': b.booker.booker_name,
                    'time': b.booking_start_time.strftime('%H:%M'),
                    'status': b.booking_status,
                }
                for b in conflicting_bookings
            ]
        }), 200
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again.'}), 500

@app.route('/admin/availability/blocked-dates/delete/', methods=['POST'])
@admin_required
def admin_delete_blocked_date(admin_online):
    data = request.get_json(silent=True) or {}
    bd_id = data.get('id')

    bd = db.session.query(BlockedDate).filter_by(bd_id=bd_id).first()
    if not bd:
        return jsonify({'success': False, 'message': 'Blocked date not found.'}), 404

    try:
        db.session.delete(bd)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Blocked date removed.'}), 200
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again.'}), 500

   
