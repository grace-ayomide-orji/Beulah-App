import os
import logging
import uuid
from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}
MAX_FLYER_SIZE_BYTES = 5 * 1024 * 1024  # 5MB
logger = logging.getLogger(__name__)


def _allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_event_flyer(file_storage, upload_folder):
    """
    file_storage: the werkzeug FileStorage from request.files.get('flyer')
    upload_folder: absolute path to static/uploads/events

    Returns the saved unique filename, or None if no file was provided
    (a flyer is optional). Raises ValueError on invalid type/size —
    callers should catch this and turn it into a 400 response.
    """
    if not file_storage or not file_storage.filename:
        return None

    original_name = secure_filename(file_storage.filename)
    if not _allowed_file(original_name):
        raise ValueError('Flyer must be a JPG, PNG, or WEBP image.')

    # Check size without loading the whole file into memory
    file_storage.seek(0, os.SEEK_END)
    size = file_storage.tell()
    file_storage.seek(0)
    if size > MAX_FLYER_SIZE_BYTES:
        raise ValueError('Flyer image must be under 5MB.')

    ext = original_name.rsplit('.', 1)[1].lower()
    unique_name = f"{uuid.uuid4().hex}.{ext}"  # avoids collisions between events

    os.makedirs(upload_folder, exist_ok=True)
    file_storage.save(os.path.join(upload_folder, unique_name))

    return unique_name


def delete_event_flyer(filename, upload_folder):
    """
    Safely remove a flyer file. Never raises — a missing file (already
    deleted, or event never had one) is not an error condition here.
    """
    if not filename:
        return
    filepath = os.path.join(upload_folder, filename)
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except OSError:
        logger.exception('Failed to delete event flyer %s.', filepath)
