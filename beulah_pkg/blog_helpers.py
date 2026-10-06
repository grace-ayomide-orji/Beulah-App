import json
import re
from html import unescape

from bleach.css_sanitizer import CSSSanitizer
from bleach.sanitizer import Cleaner

from beulah_pkg.models import Resource


BLOG_STATUSES = ('draft', 'published', 'suspended', 'unpublished')

blog_content_cleaner = Cleaner(
    tags=[
        'a', 'blockquote', 'br', 'code', 'div', 'em', 'figcaption', 'figure', 'h1', 'h2',
        'h3', 'h4', 'hr', 'img', 'li', 'ol', 'p', 'pre', 's', 'span', 'strong', 'u', 'ul'
    ],
    attributes={
        'a': ['href', 'title', 'target', 'rel'],
        'img': ['src', 'alt', 'title'],
        'p': ['style'],
        'h1': ['style'],
        'h2': ['style'],
        'h3': ['style'],
        'h4': ['style'],
        'blockquote': ['style'],
        'span': ['style'],
    },
    protocols=['http', 'https', 'mailto'],
    css_sanitizer=CSSSanitizer(allowed_css_properties=['text-align']),
    strip=True,
)


def slugify(value):
    value = (value or '').strip().lower()
    value = re.sub(r'[^a-z0-9\s-]', '', value)
    value = re.sub(r'[\s-]+', '-', value).strip('-')
    return value or 'blog-post'


def unique_blog_slug(title, requested_slug=None, resource_id=None):
    base_slug = slugify(requested_slug or title)
    slug = base_slug
    counter = 2

    while True:
        query = Resource.query.filter(
            Resource.resource_slug == slug,
            Resource.resource_type == 'blog',
        )
        if resource_id:
            query = query.filter(Resource.resource_id != resource_id)
        if not query.first():
            return slug
        slug = f'{base_slug}-{counter}'
        counter += 1


def sanitize_blog_html(html):
    return blog_content_cleaner.clean(html or '').strip()


def blog_plain_text(resource):
    if not resource:
        return ''
    html = resource.resource_body or ''
    html = re.sub(r'<\s*br\s*/?\s*>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</\s*(p|div|h[1-6]|li|blockquote|pre)\s*>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'<\s*li[^>]*>', '- ', html, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', html)
    text = unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()


def normalize_tiptap_json(value):
    value = (value or '').strip()
    if not value:
        return None
    json.loads(value)
    return value


def blog_image_url(resource):
    if not resource or not resource.resource_featured_image:
        return None
    return f'uploads/blogs/{resource.resource_featured_image}'


def blog_text_summary(resource, length=180):
    text = re.sub(r'\s+', ' ', blog_plain_text(resource)).strip()
    if len(text) <= length:
        return text
    return f'{text[:length].rsplit(" ", 1)[0]}...'
