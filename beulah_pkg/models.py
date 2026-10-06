from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
db = SQLAlchemy()
import uuid




# Admin Table
class Admin(db.Model):
    __tablename__ = 'admin'
    admin_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    admin_fullname = db.Column(db.String(40), nullable=False)
    admin_username = db.Column(db.String(12), nullable=False, unique=True)
    admin_password = db.Column(db.String(200), nullable=False)
    admin_role = db.Column(db.Enum('Admin'), nullable=False)
    admin_date_added = db.Column(db.DateTime, default=datetime.utcnow)
    admin_last_logged_in = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Admin {self.admin_fullname}>"


class AdminSecurityState(db.Model):
    __tablename__ = 'admin_security_states'
    state_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admin.admin_id'), nullable=False, unique=True, index=True)
    failed_login_attempts = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    last_failed_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AdminMfaChallenge(db.Model):
    __tablename__ = 'admin_mfa_challenges'
    challenge_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admin.admin_id'), nullable=False, index=True)
    code_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    consumed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AdminSessionToken(db.Model):
    __tablename__ = 'admin_session_tokens'
    token_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admin.admin_id'), nullable=False, index=True)
    session_token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    rotate_after = db.Column(db.DateTime, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    revoked_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)


class AdminPasswordResetToken(db.Model):
    __tablename__ = 'admin_password_reset_tokens'
    token_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admin.admin_id'), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    consumed_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)


class AdminAuditLog(db.Model):
    __tablename__ = 'admin_audit_logs'
    audit_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admin.admin_id'), nullable=True, index=True)
    action = db.Column(db.String(80), nullable=False, index=True)
    details = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    





# Resources Table (Unified Reading and Audio Resources)
class Resource(db.Model):
    __tablename__ = 'resources'
    resource_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    resource_title = db.Column(db.String(255), nullable=False)
    resource_body = db.Column(db.Text, nullable=False)
    resource_type = db.Column(db.Enum('audio', 'text', 'slide', 'blog'), nullable=False)
    resource_slug = db.Column(db.String(255), nullable=True, unique=True, index=True)
    resource_featured_image = db.Column(db.String(255), nullable=True)
    resource_status = db.Column(db.Enum('draft', 'published', 'suspended', 'unpublished'), nullable=False, default='draft')
    resource_published_date = db.Column(db.DateTime, nullable=True, index=True)
    resource_category = db.Column(db.String(100), nullable=True)
    resource_content_json = db.Column(db.Text, nullable=True)
    resource_date = db.Column(db.DateTime, default=datetime.utcnow)
    resource_updated_date = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    resource_is_deleted = db.Column(db.Boolean, default=False)

    # Relationships
    comments = db.relationship('Comment', backref='resource', lazy='dynamic')
    slide_img = db.relationship('Slide', backref='resource', uselist=False)

    def __repr__(self):
        return f"<Resource {self.resource_title}>"
    





# Comment Table
class Comment(db.Model):
    __tablename__ = 'comments'
    comment_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    resource_id = db.Column(db.Integer, db.ForeignKey('resources.resource_id'), nullable=False, index=True)
    comment_token = db.Column(db.String(100), nullable=False,  index=True)
    comment_by = db.Column(db.String(50), nullable=False)
    comment_body = db.Column(db.String(850), nullable=False)
    comment_is_approve = db.Column(db.Boolean, default=True)
    comment_is_blocked = db.Column(db.Boolean, default=False)
    comment_date = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Comment by {self.comment_by}>"






# Slide Table
class Slide(db.Model):
    __tablename__ = 'slides'
    slide_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    resource_id = db.Column(db.Integer, db.ForeignKey('resources.resource_id'), nullable=False)
    slide_image = db.Column(db.String(100), nullable=False)







# Prayer Request Table
class PrayerRequest(db.Model):
    __tablename__ = 'prayer_requests'
    pr_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    pr_name = db.Column(db.String(50), nullable=False)
    pr_email = db.Column(db.String(100), nullable=True)
    pr_phone = db.Column(db.String(50), nullable=True)
    pr_message = db.Column(db.Text, nullable=False)
    pr_date = db.Column(db.DateTime, default=datetime.utcnow)
    pr_is_read = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f"<PrayerRequest by {self.pr_name}>"
    






# Newsletter Subscriber Table
class NewsletterSubscriber(db.Model):
    __tablename__ = 'newsletter_subscribers'
    subscriber_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    subscriber_email = db.Column(db.String(100), nullable=False, unique=True)
    subscriber_status = db.Column(db.Enum('active', 'unsubscribed'), nullable=False, default='active')
    subscriber_date_joined = db.Column(db.DateTime,nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<NewsletterSubscriber {self.subscriber_email}>"
    




# Upcoming Events Table
class Event(db.Model):
    __tablename__ = 'events'
    event_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    event_theme = db.Column(db.String(100), nullable=False)
    event_date = db.Column(db.Date, nullable=False)
    event_time = db.Column(db.Time, nullable=False)
    event_venue = db.Column(db.String(100), nullable=True)
    event_flyer_filename = db.Column(db.String(255), nullable=True)
    event_description = db.Column(db.Text, nullable=True)
    event_date_added = db.Column(db.DateTime, default=datetime.utcnow)
    event_updated_date = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Event {self.event_theme} at {self.event_venue}>"
    





# Notification Table
class Notification(db.Model):
    __tablename__ = 'notifications'
    notification_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    notification_message = db.Column(db.Text, nullable=False)
    notification_for = db.Column(db.Enum('newsletter_subscription', 'contact_form', 'prayer_request'), nullable=False)
    notification_date = db.Column(db.DateTime, default=datetime.utcnow)
    notification_is_read = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f"<Notification from {self.notifier_name}>"
    

#booker Table
class Booker(db.Model):
    __tablename__ = 'bookers'
    booker_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    booker_name = db.Column(db.String(100), nullable=False)
    booker_email = db.Column(db.String(120), nullable=False, index=True)
    booker_phone = db.Column(db.String(30), nullable=True)
    booker_date_added = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    bookings = db.relationship('Booking', backref='booker', lazy='dynamic')

    def __repr__(self):
        return f"<Booker {self.booker_name}>"




# Recurring Series Table
class RecurringSeries(db.Model):
    __tablename__ = 'recurring_series'
    series_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    series_frequency = db.Column(db.Enum('weekly', 'biweekly', 'monthly'), nullable=False)
    series_start_date = db.Column(db.Date, nullable=False)
    series_end_date = db.Column(db.Date, nullable=False)
    series_date_added = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    bookings = db.relationship('Booking', backref='recurring_series', lazy='dynamic')

    def __repr__(self):
        return f"<RecurringSeries {self.series_frequency}>"




# Booking Table
class Booking(db.Model):
    __tablename__ = 'bookings'
    booking_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    booker_id = db.Column(db.Integer, db.ForeignKey('bookers.booker_id'), nullable=False, index=True)
    series_id = db.Column(db.Integer, db.ForeignKey('recurring_series.series_id'), nullable=True, index=True)

    booking_date = db.Column(db.Date, nullable=False, index=True)
    booking_start_time = db.Column(db.Time, nullable=False)
    booking_end_time = db.Column(db.Time, nullable=False)

    booking_session_type = db.Column(db.Enum('video', 'audio'), nullable=False, default='video')
    booking_session_format = db.Column(db.Enum('single', 'recurring'), nullable=False, default='single')
    booking_status = db.Column(
        db.Enum('pending', 'confirmed', 'cancelled', 'completed', 'no_show', 'rescheduled'),
        nullable=False,
        default='confirmed'
    )

    booking_manage_token = db.Column(db.String(36), nullable=False, unique=True, index=True, default=lambda: str(uuid.uuid4()))
    booking_meet_link = db.Column(db.String(255), nullable=True)
    booking_calendar_event_id = db.Column(db.String(255), nullable=True)

    booking_reason = db.Column(db.Text, nullable=True)
    booking_notes = db.Column(db.Text, nullable=True)  # private admin notes

    booking_date_added = db.Column(db.DateTime, default=datetime.utcnow)
    booking_updated_date = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Booking {self.booking_id} on {self.booking_date}>"




# Working Hours Table
class WorkingHours(db.Model):
    __tablename__ = 'working_hours'
    wh_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    wh_day_of_week = db.Column(db.Integer, nullable=False, unique=True)  # 0=Sunday ... 6=Saturday
    wh_start_time = db.Column(db.Time, nullable=False)
    wh_end_time = db.Column(db.Time, nullable=False)
    wh_is_active = db.Column(db.Boolean, default=True)
    wh_updated_date = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<WorkingHours day={self.wh_day_of_week}>"




# Blocked Date Table
class BlockedDate(db.Model):
    __tablename__ = 'blocked_dates'
    bd_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    bd_date = db.Column(db.Date, nullable=False, unique=True, index=True)
    bd_reason = db.Column(db.String(255), nullable=True)

    def __repr__(self):
        return f"<BlockedDate {self.bd_date}>"


class RecurringUnavailability(db.Model):
    __tablename__ = 'recurring_unavailability'
    ru_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    ru_day_of_week = db.Column(db.Integer, nullable=False)  # 0=Sunday ... 6=Saturday, same convention as WorkingHours
    ru_start_time = db.Column(db.Time, nullable=False)
    ru_end_time = db.Column(db.Time, nullable=False)
    ru_reason = db.Column(db.String(255), nullable=True)
    ru_date_added = db.Column(db.DateTime, default=datetime.utcnow)
 
    def __repr__(self):
        return f"<RecurringUnavailability day={self.ru_day_of_week} {self.ru_start_time}-{self.ru_end_time}>"
 


# Donation Table
class Donation(db.Model):
    __tablename__ = 'donations'
    donation_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    donation_name = db.Column(db.String(100), nullable=True)
    donation_email = db.Column(db.String(120), nullable=True)
    donation_amount = db.Column(db.Numeric(10, 2), nullable=False)
    donation_currency = db.Column(db.String(10), nullable=False, default='NGN')  
    donation_gateway = db.Column(db.Enum('paystack', 'stripe'), nullable=False)
    donation_transaction_reference = db.Column(db.String(120), nullable=True, unique=True)
    donation_status = db.Column(db.Enum('pending', 'completed', 'failed', 'refunded'), nullable=False, default='pending')
    donation_is_anonymous = db.Column(db.Boolean, default=False)
    donation_date_added = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<Donation {self.donation_id} {self.donation_currency}{self.donation_amount}>"
