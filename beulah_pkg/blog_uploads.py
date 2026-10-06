import os
import logging
import uuid

from werkzeug.utils import secure_filename


ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}
MAX_BLOG_IMAGE_SIZE_BYTES = 5 * 1024 * 1024
logger = logging.getLogger(__name__)


def _allowed_image(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def save_blog_image(file_storage, upload_folder):
    if not file_storage or not file_storage.filename:
        return None

    original_name = secure_filename(file_storage.filename)
    if not _allowed_image(original_name):
        raise ValueError('Image must be a JPG, PNG, or WEBP file.')

    file_storage.seek(0, os.SEEK_END)
    size = file_storage.tell()
    file_storage.seek(0)
    if size > MAX_BLOG_IMAGE_SIZE_BYTES:
        raise ValueError('Image must be under 5MB.')

    ext = original_name.rsplit('.', 1)[1].lower()
    unique_name = f'{uuid.uuid4().hex}.{ext}'

    os.makedirs(upload_folder, exist_ok=True)
    file_storage.save(os.path.join(upload_folder, unique_name))
    return unique_name


def delete_blog_image(filename, upload_folder):
    if not filename:
        return
    filepath = os.path.join(upload_folder, filename)
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except OSError:
        logger.exception('Failed to delete blog image %s.', filepath)
