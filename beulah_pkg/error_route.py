from flask import Flask, current_app, render_template
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import TooManyRequests
from sqlalchemy.exc import InterfaceError
from beulah_pkg import app
# import logging



# @app.errorhandler(TooManyRequests)
# def handle_rate_limit_error(error):
#     return jsonify({
#         'status': 'error',
#         'message': 'Rate limit exceeded. Please try again later.'
#     }), 429


# page not found
@app.errorhandler(404)
def error404(e):
    return render_template('error/error404.html'), 404


# Bad request
@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    return render_template('error/error400.html', reason=e.description), 400


#method not allowed
@app.errorhandler(405)
def error405(e):
    return render_template('error/error405.html'), 405



# Maintanance, busy server, service unavaible
@app.errorhandler(503)
def error503(e):
    return render_template('error/error503.html'), 503


# Unauthorized/ access denied
@app.errorhandler(401)
def error401(e):
    return render_template('error/error401.html'), 401


#Access Forbidden! 
@app.errorhandler(403)
def error403(e):
    return render_template('error/error403.html'), 403



#too many requests
@app.errorhandler(429)
def error429(e):
    return render_template('error/error429.html'), 429




#request time out
@app.errorhandler(408)
def error408(e):
    return render_template('error/error408.html'), 408




#server error
@app.errorhandler(500)
def error500(e):
    current_app.logger.exception('Server error: %s', e)
    return render_template('error/error500.html'), 500




@app.errorhandler(Exception)
def handle_exception(e):
    # This will catch any unhandled exceptions and trigger your custom 500 page
    current_app.logger.exception('Unhandled exception: %s', e)
    return render_template('error/error500.html'), 500





@app.errorhandler(InterfaceError)
def handle_interface_error(e):
    current_app.logger.exception('Database connection error: %s', e)
    return render_template("error/dberror.html"), 500
