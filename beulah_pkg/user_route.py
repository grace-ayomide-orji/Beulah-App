import re
from math import ceil
import uuid
from flask import Flask,render_template,redirect,request,abort,flash,url_for,session,jsonify, make_response, g
from sqlalchemy import func, or_
from bleach.sanitizer import Cleaner
from flask_mail import Message
from markupsafe import escape
from beulah_pkg import app, mail, limiter
from beulah_pkg.blog_helpers import blog_image_url, blog_text_summary
from beulah_pkg.spam_defense import is_honeypot_triggered, verify_turnstile
from beulah_pkg.models import db, NewsletterSubscriber, Resource, PrayerRequest, Notification, Comment, Event, Slide




text_cleaner = Cleaner(
    tags=[], 
    strip=True
)


# @app.after_request
# def after_request(response):
#     response.headers['Cache-Control']='no-cache, no-store, must-revalidate'
#     return response 

@app.before_request
def ensure_user_token():
    user_token = request.cookies.get('user_token')
    if not user_token:
        user_token = str(uuid.uuid4())
        g.new_user_token = user_token
    g.user_token = user_token or g.new_user_token


@app.after_request
def after_request(response):
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    if hasattr(g, 'new_user_token'):
        response.set_cookie('user_token', g.new_user_token, httponly=True, secure=True, samesite='Strict')
    return response

def format_date_with_suffix(date):
    day = date.day
    if 10 <= day % 100 <= 20:  # Special case for teens
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")

    return date.strftime(f"%A, %B {day}{suffix}").lstrip("0")


def format_event_date(event_date):
    # Determine the suffix for the day
    day = event_date.day
    if 10 <= day % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
    
    # Format the date without the suffix, then manually insert it
    formatted_date = event_date.strftime(f'%A {day}{suffix} %B, %Y')
    return formatted_date


@app.route('/')
def home():
    slides = db.session.query(Resource, Slide).join(Slide).filter(
        Resource.resource_type == 'slide',
        Resource.resource_status == 'published',
        Resource.resource_is_deleted == False
    ).all()
    return render_template('user/index.html', slides=slides)


@app.route('/beulah/')
def user_home():
    slides = db.session.query(Resource, Slide).join(Slide).filter(
        Resource.resource_type == 'slide',
        Resource.resource_status == 'published',
        Resource.resource_is_deleted == False
    ).all()
    return render_template('user/index.html', slides=slides)


@app.route('/search/', methods=['GET', 'POST'])
def search_results():
    page = request.args.get('page', 1, type=int)  # Get the page number, default is 1
    per_page = 5

    search_term = ""
    if request.method == 'POST':
        search_term = request.form.get('query', '').strip()
    elif request.method == 'GET':
        search_term = request.args.get('query', '').strip()

    # Initialize query objects instead of empty strings
    reading_query = db.session.query(Resource).filter(
        or_(
            Resource.resource_type == 'text',
            Resource.resource_type == 'slide',
            Resource.resource_type == 'blog'
        ),
        Resource.resource_status == 'published',
        Resource.resource_is_deleted == False
    )

    audio_query = db.session.query(Resource).filter(
        Resource.resource_type == 'audio',
        Resource.resource_status == 'published',
        Resource.resource_is_deleted == False
    )

    # Apply search filter only if there's a search term
    if search_term:
        reading_query = reading_query.filter(Resource.resource_title.ilike(f"%{search_term}%"))
        audio_query = audio_query.filter(Resource.resource_title.ilike(f"%{search_term}%"))

    # Paginate the query objects directly (do NOT call .all() first)
    reading_pagination = reading_query.paginate(page=page, per_page=per_page, error_out=False)
    audio_pagination = audio_query.paginate(page=page, per_page=per_page, error_out=False)
    
    reading_resources = reading_pagination.items
    audio_resources = audio_pagination.items

    # base_url = url_for('search_results')
    base_url = url_for('search_results')
    return render_template('user/search_results.html', search_term=search_term,  reading_pagination=reading_pagination,
        audio_pagination=audio_pagination, reading_resources=reading_resources,audio_resources=audio_resources, base_url=base_url)


@app.route('/about-us/')
def about():
    return render_template('user/about.html')


@app.route('/partnership/')
def partnership():
    return render_template('user/partnership.html')


@app.route('/privacy-cookie-notice/')
def privacy_cookie_notice():
    return render_template('user/privacy_cookie_notice.html')


@app.route('/contact-us/', methods=['GET', 'POST'])
@limiter.limit("5 per hour", methods=["POST"])
def contact_us():
    if request.method == 'POST':
        try:

            if is_honeypot_triggered(request.form):
                # Pretend success — low-stakes form, no reason to tip a bot off
                return jsonify({'success': True, 'message': 'Your message has been sent successfully!'}), 200
 
            if not verify_turnstile(request.form.get('turnstile_token'), request.remote_addr):
                return jsonify({'success': False, 'message': 'CAPTCHA verification failed. Please try again.'}), 403
 
            # Retrieve form data
            name = str(escape(request.form.get('name')))
            phone = str(escape(request.form.get('phone')))
            email_address = str(escape(request.form.get('email')))
            message =str( escape(request.form.get('request')))

            # Validate required fields
            if not all([name, phone, email_address, message]):
                return jsonify({'success': False, 'message': 'All fields are required.'}), 400


             # Send email to admin
            try:
                admin_email = "beufound@gmail.com"
                msg = Message(
                    subject="New Contact Us Form Submission",
                    sender=app.config['MAIL_DEFAULT_SENDER'],
                    recipients=[admin_email]
                )
                msg.html = f"""
                <h3>
                    You have a new message from the contact form
                </h3>
                <p><strong>Name:</strong> {name}</p>
                <p><strong>Phone:</strong> {phone}</p>
                <p><strong>Email:</strong> {email_address}</p>
                <p><strong>Message:</strong> {message}</p>
                """
                mail.send(msg)

            except Exception as e:
                return jsonify({'success': False, 'message': 'There was an issue sending your email. Please try again later.'}), 500

             # Save notification to the database
            try:
                notification_message = f'New contact form submission from {name}'

                new_notification = Notification(
                    notification_message=notification_message,
                    notification_for= 'contact_form'
                )
                db.session.add(new_notification)
                db.session.commit()

            except Exception as e:
                db.session.rollback()
                return jsonify({'success': True, 'message': 'Your email was sent successfully, but we encountered an issue saving the notification.'}), 200

            return jsonify({'success': True, 'message': 'Your message has been sent successfully!' }), 200

        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': 'There was an issue sending your message. Please try again later.'}), 500

    return render_template('user/contact.html')


@app.route('/prayer-request/', methods=['GET', 'POST'])
@limiter.limit("5 per hour", methods=["POST"])
def prayer_request():
    if request.method == 'POST':
        try:
            if is_honeypot_triggered(request.form):
                # Pretend success — low-stakes form, no reason to tip a bot off
                return jsonify({'success': True, 'message': 'Prayer request submitted successfully'}), 200
 
            if not verify_turnstile(request.form.get('turnstile_token'), request.remote_addr):
                return jsonify({'success': False, 'message': 'CAPTCHA verification failed. Please try again.'}), 403
            
            # Retrieve form data
            name = escape(request.form.get('name'))
            phone = escape(request.form.get('phone')) or None
            email_address = escape(request.form.get('email')) or None
            p_request = escape(request.form.get('request'))
            # Validate required fields
            if not name or not p_request:
                return jsonify({'success': False, 'message': 'Name and Prayer Request are required fields.'}), 400

            # Save to the database
            notification_message = f'New Prayer Request from {name}'


            prayer_request = PrayerRequest(
                pr_name=str(name),
                pr_email=str(email_address),
                pr_phone=str(phone),
                pr_message=str(p_request)
            )

            new_notification = Notification(
            notification_message = notification_message,
            notification_for = 'prayer_request'

            )
            db.session.add(prayer_request)
            db.session.add(new_notification)
            db.session.commit()

            # Success response
            return jsonify({'success': True, 'message': 'Prayer request submitted successfully'}), 200

        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': 'Something went wrong. Please try again later.'}), 500

    return render_template('user/prayer_request.html')


@app.route('/upcoming-events/')
def upcoming_event():
    events = db.session.query(Event).order_by(Event.event_updated_date.desc()).all()
    for event in events:
        event.formatted_date = format_event_date(event.event_date)
    return render_template('user/upcoming_event.html', events=events)


@app.route('/reading-resources/')
def reading_resources():
    user_token = g.get('user_token')
    page = request.args.get('page', 1, type=int)  # Get the page number, default is 1
    per_page = 10

     # Subquery to count comments for each resource
    subquery = db.session.query(
        Comment.resource_id, 
        func.count(Comment.comment_id).label('comment_count')
    ).group_by(Comment.resource_id).subquery()

    # Query for resources and join with the subquery to get the comment count
    query = db.session.query(Resource, subquery.c.comment_count).outerjoin(subquery, subquery.c.resource_id == Resource.resource_id).filter(
        Resource.resource_type == 'text',
        Resource.resource_status == 'published',
        Resource.resource_is_deleted == False
    ).order_by(Resource.resource_date.desc())

  
    # Apply pagination
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Fetch paginated results
    
    base_url = url_for('reading_resources')
    reading_resources = pagination.items
 
    return render_template('user/reading_resource.html',base_url=base_url, reading_resources=reading_resources, pagination=pagination)


@app.route('/audio-resources/')
def audio_resources():
    user_token = g.get('user_token')
    page = request.args.get('page', 1, type=int)  # Get the page number, default is 1
    per_page = 10

     # Subquery to count comments for each resource
    subquery = db.session.query(
        Comment.resource_id, 
        func.count(Comment.comment_id).label('comment_count')
    ).group_by(Comment.resource_id).subquery()

    # Query for resources and join with the subquery to get the comment count
    query = db.session.query(Resource, subquery.c.comment_count).outerjoin(subquery, subquery.c.resource_id == Resource.resource_id).filter(
        Resource.resource_type == 'audio',
        Resource.resource_status == 'published',
        Resource.resource_is_deleted == False
    ).order_by(Resource.resource_date.desc())

  
    # Apply pagination
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Fetch paginated results
    
    base_url = url_for('audio_resources')
    resources = pagination.items
    return render_template('user/audio_resource.html', resources=resources ,base_url=base_url, pagination=pagination)


@app.route('/blog/')
def blog_posts():
    page = request.args.get('page', 1, type=int)
    per_page = 9
    subquery = db.session.query(
        Comment.resource_id,
        func.count(Comment.comment_id).label('comment_count')
    ).group_by(Comment.resource_id).subquery()

    query = db.session.query(Resource, subquery.c.comment_count).outerjoin(
        subquery,
        subquery.c.resource_id == Resource.resource_id
    ).filter(
        Resource.resource_type == 'blog',
        Resource.resource_status == 'published',
        Resource.resource_is_deleted == False
    ).order_by(Resource.resource_published_date.desc(), Resource.resource_updated_date.desc())

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return render_template(
        'user/blog_list.html',
        blog_resources=pagination.items,
        pagination=pagination,
        base_url=url_for('blog_posts'),
        blog_image_url=blog_image_url,
        blog_text_summary=blog_text_summary,
        meta_description='Read articles and spiritual reflections from Beulah Foundation for Christ.',
        canonical_url=url_for('blog_posts', _external=True),
        og_title='Blog - Beulah Foundation for Christ',
        og_description='Read articles and spiritual reflections from Beulah Foundation for Christ.',
    )


@app.route('/blog/<slug>/')
def blog_detail(slug):
    resource = Resource.query.filter(
        Resource.resource_type == 'blog',
        Resource.resource_slug == slug,
        Resource.resource_status == 'published',
        Resource.resource_is_deleted == False
    ).first_or_404()
    user_token = g.get('user_token')
    comments = db.session.query(Comment).filter(
        Comment.resource_id == resource.resource_id,
        Comment.comment_is_approve == True,
        Comment.comment_is_blocked == False
    ).order_by(Comment.comment_date.desc()).limit(20).all()
    for comment in comments:
        comment.formatted_date = format_date_with_suffix(comment.comment_date)

    image_path = blog_image_url(resource)
    image_url = url_for('static', filename=image_path, _external=True) if image_path else None
    description = blog_text_summary(resource, 300)

    return render_template(
        'user/blog_detail.html',
        resource=resource,
        is_preview=False,
        featured_image_url=url_for('static', filename=image_path) if image_path else '',
        user_token=user_token,
        comments=comments,
        page_title=f'{resource.resource_title} - Beulah Foundation for Christ',
        meta_description=description,
        canonical_url=url_for('blog_detail', slug=resource.resource_slug, _external=True),
        og_title=resource.resource_title,
        og_description=description,
        og_image_url=image_url,
        blog_text_summary=blog_text_summary,
    )


@app.route('/dynamic_messages/<int:id>/')
def dynamic_messages(id):
    user_token = g.get('user_token')
    if id <= 0:
        flash('Invalid resource ID.', 'error')
        return redirect(url_for('reading_resources'))
    resource = db.session.query(Resource).filter(
        Resource.resource_id == id,
        Resource.resource_type == 'text',
        Resource.resource_status == 'published',
        Resource.resource_is_deleted == False
    ).first()

    if not resource:
        flash('Resource not found', 'error')
        return redirect(url_for('reading_resources'))
    
    comments = db.session.query(Comment).filter(
        Comment.resource_id == resource.resource_id,
        Comment.comment_is_approve == True,
        Comment.comment_is_blocked == False
        ).order_by(Comment.comment_date.desc()).limit(20).all()
    for comment in comments:
        comment.formatted_date = format_date_with_suffix(comment.comment_date)
    
    return render_template('user/dynamic_messages.html', resource=resource, user_token=user_token, comments=comments)


@app.route('/dynamic_audios/<int:id>/')
def dynamic_audios(id):
    user_token = g.get('user_token')
    if id <= 0:
        flash('Invalid resource ID.', 'error')
        return redirect(url_for('audio_resources'))
    resource = db.session.query(Resource).filter(
        Resource.resource_id == id,
        Resource.resource_type == 'audio',
        Resource.resource_status == 'published',
        Resource.resource_is_deleted == False
    ).first()

    if not resource:
        flash('Resource not found', 'error')
        return redirect(url_for('audio_resources'))
    
    comments = db.session.query(Comment).filter(
        Comment.resource_id == resource.resource_id,
        Comment.comment_is_approve == True,
        Comment.comment_is_blocked == False
        ).order_by(Comment.comment_date.desc()).limit(20).all()
    for comment in comments:
        comment.formatted_date = format_date_with_suffix(comment.comment_date)
    return render_template('user/dynamic_audios.html', resource=resource, user_token=user_token, comments=comments)


@app.route('/dynamic_slide/<int:id>/')
def dynamic_slides(id):
    user_token = g.get('user_token')
    if id <= 0:
        flash('Invalid resource ID.', 'error')
        return redirect(url_for('home'))
    resource = db.session.query(Resource).filter(
        Resource.resource_id == id,
        Resource.resource_type == 'slide',
        Resource.resource_status == 'published',
        Resource.resource_is_deleted == False
    ).first()

    if not resource:
        flash('Resource not found', 'error')
        return redirect(url_for('home'))
    
    comments = db.session.query(Comment).filter(
        Comment.resource_id == resource.resource_id,
        Comment.comment_is_approve == True,
        Comment.comment_is_blocked == False
        ).order_by(Comment.comment_date.desc()).limit(20).all()
    for comment in comments:
        comment.formatted_date = format_date_with_suffix(comment.comment_date)
    
    return render_template('user/dynamic_messages.html', resource=resource, user_token=user_token, comments=comments)


@app.route('/add-comment/<int:id>/', methods=['POST'])
@limiter.limit("5 per hour", methods=['POST'])
def add_comment(id):
    if is_honeypot_triggered(request.form):
        return jsonify({'success': True, 'message': 'Comment submitted sucessfully.'}), 200
 
    if not verify_turnstile(request.form.get('turnstile_token'), request.remote_addr):
        return jsonify({'success': False, 'message': 'CAPTCHA verification failed. Please try again.'}), 403
 
    commenter_name = request.form.get('name')
    comment_body = request.form.get('comment')
    resource = db.session.query(Resource).filter(
        Resource.resource_id == id,
        Resource.resource_status == 'published',
        Resource.resource_is_deleted == False
    ).first()
 
    if not resource:
        return jsonify({'success': False, 'message': 'Resource Not Found.'}), 400
 
    if not comment_body or not commenter_name:
         return jsonify({'success': False, 'message': 'Missing required fields.'}), 400
 
    user_token = g.get('user_token')
    if not user_token:
        return jsonify({'success': False, 'message': 'Something Went wrong, Please try again later.'}), 400
 
    try:
        sanitized_name = text_cleaner.clean(commenter_name)
        sanitized_content = text_cleaner.clean(comment_body)
        new_comment = Comment(
            resource_id=resource.resource_id,
            comment_token=user_token,
            comment_by=sanitized_name,
            comment_body=sanitized_content
        )
        db.session.add(new_comment)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Comment submitted sucessfully.'}), 200
    except:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Something Went wrong, Please try again.'}), 400
    

@app.route('/edit_comment/<int:comment_id>/', methods=['POST'])
@limiter.limit("5 per hour", methods=['POST'])
def edit_comment(comment_id):
    comment = Comment.query.get(comment_id)
    if not comment:
        return jsonify({'success': False, 'message': 'Comment not found'}),400
    if comment.comment_is_blocked:
        return jsonify({'success': False, 'message': 'This comment can no longer be edited.'}), 403
    
    user_token = g.get('user_token')

    if user_token and user_token == comment.comment_token:
    
        commenter_name = request.form.get('name')
        new_body = request.form.get('comment') 
        if not new_body or not commenter_name:
            return jsonify({'success': False, 'message': 'Name and Comment fields cannot be empty'}),400
        
        try:
            sanitized_name = text_cleaner.clean(commenter_name)
            sanitized_content = text_cleaner.clean(new_body)

            comment.comment_by = sanitized_name
            comment.comment_body = sanitized_content
            db.session.commit()
            return jsonify({'success': True, 'message': 'Comment edited successfully!'})
        except:
           db.session.rollback()
           return jsonify({'success': False, 'message': 'Something went wrong'}),400 
    else:
        return jsonify({'success': False, 'message': 'Unauthorized to edit this comment.'}), 403


@app.route('/delete-comment/', methods=['POST'])
@limiter.limit("10 per hour", methods=['POST'])
def delete_comment():
    data = request.get_json(silent=True) or {}
    comment_id = data.get('id')
    comment = Comment.query.get(comment_id)
    if not comment:
        return jsonify({'success': False, 'message': 'Comment not found'}),404
    if comment.comment_is_blocked:
        return jsonify({'success': False, 'message': 'This comment can no longer be deleted.'}), 403
    
    user_token = g.get('user_token')

    if user_token and user_token == comment.comment_token:

        db.session.delete(comment)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Comment deleted successfully!'}),200
    else:
        return jsonify({'success': False, 'message': 'Unauthorized to delete this comment.'}), 403


@app.route('/beulah/subscriber/email/', methods=['POST'])
@limiter.limit("5 per hour", methods=['POST'])
def subscribe():
    # Get the subscriber email from the request body (JSON)
    data = request.get_json(silent=True) or {}
    subscriber = str(escape(data.get('subscriberEmail', '')).strip())
    
    email_regex = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'

    # If email is empty, return an error response
    if not subscriber:
        return jsonify({'success': False, 'message': 'Email field cannot be empty.'}), 400

    # If email is invalid, return an error response
    if not re.match(email_regex, subscriber):
        return jsonify({'success': False, 'message': 'Please enter a valid email address.'}), 400

    existing_subscription = db.session.query(NewsletterSubscriber).filter_by(subscriber_email=subscriber).first()
    if existing_subscription:
        return jsonify({'success': True, 'message': 'You are already a subscriber. Thank you!'}), 200

    # Proceed with adding the new subscriber
    notification_message = f'New subscriber: {subscriber}'

    new_subscriber = NewsletterSubscriber(subscriber_email=subscriber)
    new_notification = Notification(
        notification_message=notification_message,
        notification_for='newsletter_subscription'
    )

    try:
        # Add the subscriber and notification to the session
        db.session.add(new_subscriber)
        db.session.add(new_notification)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Thanks for subscribing to our newsletter.'}), 200

    except Exception as e:
        # Rollback if an error occurs
        db.session.rollback()
        return jsonify({'success': False, 'message': 'An error occurred while processing your subscription. Please try again later.'}), 500
    



