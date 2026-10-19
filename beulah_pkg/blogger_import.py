"""Dry-run-first, Resource-only Blogger Takeout importer support."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

from sqlalchemy import bindparam, inspect, text

from beulah_pkg.blog_helpers import sanitize_blog_html, slugify


ATOM = '{http://www.w3.org/2005/Atom}'
BLOGGER = '{http://schemas.google.com/blogger/2018}'
IMPORTABLE_STATUSES = {'DRAFT': 'draft', 'LIVE': 'published', 'SCHEDULED': 'draft'}
VOID_TAGS = {'br', 'hr', 'img'}
BLOCK_TAGS = {
    'blockquote', 'div', 'figure', 'h1', 'h2', 'h3', 'h4', 'ol', 'p', 'pre', 'ul'
}
REQUIRED_RESOURCE_COLUMNS = {
    'resource_id', 'resource_title', 'resource_body', 'resource_type', 'resource_slug',
    'resource_featured_image', 'resource_status', 'resource_published_date',
    'resource_category', 'resource_content_json', 'resource_date',
    'resource_updated_date', 'resource_is_deleted',
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class BloggerEntry:
    entry_id: str
    entry_type: str
    status: str
    title: str
    content_html: str
    author_name: str
    author_uri: str
    filename: str
    original_url: str | None
    labels: tuple[str, ...]
    meta_description: str
    created_at: datetime | None
    published_at: datetime | None
    updated_at: datetime | None


@dataclass
class FeedData:
    feed_id: str
    title: str
    entries: list[BloggerEntry]
    sha256: str


@dataclass
class PreparedPost:
    source: BloggerEntry
    title: str
    body_html: str
    content_json: str
    requested_slug: str
    slug: str = ''
    status: str = 'draft'
    category: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExistingResource:
    resource_id: int
    title: str
    slug: str | None
    resource_type: str
    status: str
    published_at: datetime | None
    is_deleted: bool


@dataclass
class DatabaseSnapshot:
    target: str
    resources: list[ExistingResource]
    occupied_slugs: set[str]
    resource_counts_by_type: dict[str, int]


@dataclass
class HtmlElement:
    tag: str
    attrs: dict[str, str]
    children: list[Any] = field(default_factory=list)


class _DomParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlElement('root', {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        node = HtmlElement(tag, {key.lower(): value or '' for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


class _FeatureScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: Counter[str] = Counter()
        self.images: list[str] = []
        self.embeds: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = dict(attrs)
        self.tags[tag] += 1
        if tag == 'img':
            self.images.append(values.get('src') or '')
        if tag in {'audio', 'embed', 'iframe', 'object', 'video'}:
            self.embeds.append((tag, values.get('src') or values.get('data') or ''))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def parse_datetime(value: str | None) -> datetime | None:
    value = (value or '').strip()
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _child_text(element: ET.Element, tag: str) -> str:
    return (element.findtext(tag, default='') or '').strip()


def _category_term(category: ET.Element) -> str:
    return (
        category.get('term')
        or category.findtext(ATOM + 'term')
        or category.findtext(BLOGGER + 'term')
        or (category.text or '')
    ).strip()


def parse_feed(path: Path, blog_base_url: str | None = None) -> FeedData:
    file_bytes = path.read_bytes()
    root = ET.fromstring(file_bytes)
    entries: list[BloggerEntry] = []

    for element in root.findall(ATOM + 'entry'):
        author = element.find(ATOM + 'author')
        author_name = _child_text(author, ATOM + 'name') if author is not None else ''
        author_uri = _child_text(author, ATOM + 'uri') if author is not None else ''
        filename = _child_text(element, BLOGGER + 'filename')
        base_url = (blog_base_url or author_uri).strip()
        original_url = urljoin(base_url.rstrip('/') + '/', filename.lstrip('/')) if base_url and filename else None
        labels = tuple(
            dict.fromkeys(
                term for term in (_category_term(item) for item in element.findall(ATOM + 'category'))
                if term
            )
        )
        entries.append(
            BloggerEntry(
                entry_id=_child_text(element, ATOM + 'id'),
                entry_type=_child_text(element, BLOGGER + 'type').upper(),
                status=_child_text(element, BLOGGER + 'status').upper(),
                title=_child_text(element, ATOM + 'title'),
                content_html=element.findtext(ATOM + 'content', default='') or '',
                author_name=author_name,
                author_uri=author_uri,
                filename=filename,
                original_url=original_url,
                labels=labels,
                meta_description=_child_text(element, BLOGGER + 'metaDescription'),
                created_at=parse_datetime(element.findtext(BLOGGER + 'created')),
                published_at=parse_datetime(element.findtext(ATOM + 'published')),
                updated_at=parse_datetime(element.findtext(ATOM + 'updated')),
            )
        )

    return FeedData(
        feed_id=_child_text(root, ATOM + 'id'),
        title=_child_text(root, ATOM + 'title'),
        entries=entries,
        sha256=sha256(file_bytes).hexdigest(),
    )


def blogger_slug(filename: str, title: str) -> str:
    basename = filename.rstrip('/').rsplit('/', 1)[-1]
    basename = re.sub(r'\.html?$', '', basename, flags=re.IGNORECASE)
    candidate = slugify(basename or title)
    if len(candidate) <= 255:
        return candidate
    digest = sha256(candidate.encode('utf-8')).hexdigest()[:10]
    return f'{candidate[:244].rstrip("-")}-{digest}'


def normalize_blogger_html(value: str) -> str:
    value = re.sub(r'<!--\[if.*?<!\[endif\]-->', '', value or '', flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r'<\s*(style|script)\b[^>]*>.*?<\s*/\s*\1\s*>', '', value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r'<\s*b(?=\s|>)\s*[^>]*>', '<strong>', value, flags=re.IGNORECASE)
    value = re.sub(r'<\s*/\s*b\s*>', '</strong>', value, flags=re.IGNORECASE)
    value = re.sub(r'<\s*i(?=\s|>)\s*[^>]*>', '<em>', value, flags=re.IGNORECASE)
    value = re.sub(r'<\s*/\s*i\s*>', '</em>', value, flags=re.IGNORECASE)
    return sanitize_blog_html(value)


def _style_map(value: str) -> dict[str, str]:
    result = {}
    for declaration in (value or '').split(';'):
        if ':' in declaration:
            key, val = declaration.split(':', 1)
            result[key.strip().lower()] = val.strip().lower()
    return result


def _add_mark(marks: list[dict[str, Any]], mark: dict[str, Any]) -> list[dict[str, Any]]:
    key = json.dumps(mark, sort_keys=True)
    if all(json.dumps(item, sort_keys=True) != key for item in marks):
        return [*marks, mark]
    return marks


def _marks_for(element: HtmlElement, inherited: list[dict[str, Any]]) -> list[dict[str, Any]]:
    marks = list(inherited)
    tag_marks = {
        'strong': {'type': 'bold'},
        'em': {'type': 'italic'},
        's': {'type': 'strike'},
        'u': {'type': 'underline'},
        'code': {'type': 'code'},
        'sup': {'type': 'superscript'},
    }
    if element.tag in tag_marks:
        marks = _add_mark(marks, tag_marks[element.tag])
    if element.tag == 'a' and element.attrs.get('href'):
        marks = _add_mark(
            marks,
            {
                'type': 'link',
                'attrs': {
                    'href': element.attrs['href'],
                    'target': element.attrs.get('target') or None,
                    'rel': element.attrs.get('rel') or None,
                    'class': None,
                },
            },
        )
    styles = _style_map(element.attrs.get('style', ''))
    weight = styles.get('font-weight', '')
    if weight == 'bold' or (weight.isdigit() and int(weight) >= 600):
        marks = _add_mark(marks, {'type': 'bold'})
    if styles.get('font-style') in {'italic', 'oblique'}:
        marks = _add_mark(marks, {'type': 'italic'})
    decoration = styles.get('text-decoration', '')
    if 'underline' in decoration:
        marks = _add_mark(marks, {'type': 'underline'})
    if 'line-through' in decoration:
        marks = _add_mark(marks, {'type': 'strike'})
    return marks


def _inline_nodes(value: Any, marks: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    marks = marks or []
    if isinstance(value, str):
        cleaned = re.sub(r'\s+', ' ', value)
        if not cleaned:
            return []
        node: dict[str, Any] = {'type': 'text', 'text': cleaned}
        if marks:
            node['marks'] = marks
        return [node]
    if value.tag == 'br':
        return [{'type': 'hardBreak'}]
    if value.tag in {'hr', 'img'} or value.tag in BLOCK_TAGS:
        return []
    child_marks = _marks_for(value, marks)
    result: list[dict[str, Any]] = []
    for child in value.children:
        result.extend(_inline_nodes(child, child_marks))
    return _merge_text_nodes(result)


def _merge_text_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for node in nodes:
        if not node.get('text') and node.get('type') == 'text':
            continue
        if (
            result
            and node.get('type') == 'text'
            and result[-1].get('type') == 'text'
            and result[-1].get('marks') == node.get('marks')
        ):
            result[-1]['text'] += node['text']
        else:
            result.append(node)
    return result


def _alignment(element: HtmlElement) -> str | None:
    value = _style_map(element.attrs.get('style', '')).get('text-align')
    return value if value in {'center', 'justify', 'left', 'right'} else None


def _paragraph(nodes: list[dict[str, Any]], alignment: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {'type': 'paragraph'}
    if alignment:
        node['attrs'] = {'textAlign': alignment}
    nodes = _merge_text_nodes(nodes)
    if nodes:
        node['content'] = nodes
    return node


def _image_node(element: HtmlElement) -> dict[str, Any] | None:
    src = element.attrs.get('src', '').strip()
    if not src:
        return None
    return {
        'type': 'image',
        'attrs': {
            'src': src,
            'alt': element.attrs.get('alt') or None,
            'title': element.attrs.get('title') or None,
        },
    }


def _container_blocks(children: Iterable[Any], alignment: str | None = None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    inline: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal inline
        if inline and any(item.get('type') != 'text' or item.get('text', '').strip() for item in inline):
            blocks.append(_paragraph(inline, alignment))
        inline = []

    for child in children:
        if isinstance(child, HtmlElement) and (child.tag in BLOCK_TAGS or child.tag in {'hr', 'img'}):
            flush()
            blocks.extend(_block_nodes(child))
        else:
            inline.extend(_inline_nodes(child))
    flush()
    return blocks


def _block_nodes(element: HtmlElement) -> list[dict[str, Any]]:
    tag = element.tag
    if tag == 'img':
        image = _image_node(element)
        return [image] if image else []
    if tag == 'hr':
        return [{'type': 'horizontalRule'}]
    if tag in {'p', 'h1', 'h2', 'h3', 'h4'}:
        blocks = _container_blocks(element.children, _alignment(element))
        if not blocks:
            blocks = [_paragraph([], _alignment(element))]
        if tag.startswith('h'):
            level = int(tag[1])
            for block in blocks:
                if block['type'] == 'paragraph':
                    block['type'] = 'heading'
                    block['attrs'] = {'level': level, 'textAlign': _alignment(element)}
        return blocks
    if tag in {'div', 'figure'}:
        return _container_blocks(element.children, _alignment(element))
    if tag == 'blockquote':
        content = _container_blocks(element.children)
        return [{'type': 'blockquote', 'content': content or [_paragraph([])]}]
    if tag == 'pre':
        raw = ''.join(_text_content(child) for child in element.children)
        node: dict[str, Any] = {'type': 'codeBlock', 'attrs': {'language': None}}
        if raw:
            node['content'] = [{'type': 'text', 'text': raw}]
        return [node]
    if tag in {'ul', 'ol'}:
        items = []
        for child in element.children:
            if isinstance(child, HtmlElement) and child.tag == 'li':
                content = _container_blocks(child.children)
                if not content:
                    content = [_paragraph([])]
                if content[0]['type'] != 'paragraph':
                    content.insert(0, _paragraph([]))
                items.append({'type': 'listItem', 'content': content})
        list_node: dict[str, Any] = {
            'type': 'bulletList' if tag == 'ul' else 'orderedList',
            'content': items,
        }
        if tag == 'ol':
            try:
                start = int(element.attrs.get('start', '1'))
            except ValueError:
                start = 1
            list_node['attrs'] = {'start': start}
        return [list_node] if items else []
    return _container_blocks(element.children)


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value.tag == 'br':
        return '\n'
    return ''.join(_text_content(child) for child in value.children)


def html_to_tiptap_json(html: str) -> dict[str, Any]:
    parser = _DomParser()
    parser.feed(html or '')
    parser.close()
    content = _container_blocks(parser.root.children)
    document: dict[str, Any] = {'type': 'doc'}
    if content:
        document['content'] = content
    validate_tiptap_json(document)
    return document


def validate_tiptap_json(document: dict[str, Any]) -> None:
    allowed_nodes = {
        'blockquote', 'bulletList', 'codeBlock', 'doc', 'hardBreak', 'heading', 'horizontalRule',
        'image', 'listItem', 'orderedList', 'paragraph', 'text',
    }
    allowed_marks = {'bold', 'code', 'italic', 'link', 'strike', 'superscript', 'underline'}

    def visit(node: dict[str, Any], path: str) -> None:
        node_type = node.get('type')
        if node_type not in allowed_nodes:
            raise ValueError(f'Unsupported Tiptap node {node_type!r} at {path}.')
        if node_type == 'text' and not isinstance(node.get('text'), str):
            raise ValueError(f'Text node without text at {path}.')
        for mark in node.get('marks', []):
            if mark.get('type') not in allowed_marks:
                raise ValueError(f'Unsupported Tiptap mark at {path}: {mark!r}.')
        for index, child in enumerate(node.get('content', [])):
            if not isinstance(child, dict):
                raise ValueError(f'Invalid Tiptap child at {path}.content[{index}].')
            visit(child, f'{path}.content[{index}]')

    if not isinstance(document, dict) or document.get('type') != 'doc':
        raise ValueError('Tiptap content must be a doc object.')
    visit(document, '$')


def prepare_post(entry: BloggerEntry) -> PreparedPost:
    if not entry.entry_id:
        raise ValueError('Blogger entry has no stable id.')
    if not entry.title:
        raise ValueError('Post title is empty.')
    if entry.status not in IMPORTABLE_STATUSES:
        raise ValueError(f'Unsupported Blogger status: {entry.status or "<empty>"}.')
    if entry.status == 'LIVE' and not entry.published_at:
        raise ValueError('Published post has no publication date.')

    scanner = _FeatureScanner()
    scanner.feed(entry.content_html)
    if scanner.embeds:
        details = ', '.join(f'{tag}:{url or "<missing URL>"}' for tag, url in scanner.embeds)
        raise ValueError(f'Post contains embeds unsupported by the current editor: {details}.')

    body_html = normalize_blogger_html(entry.content_html)
    if entry.status == 'LIVE' and not body_html:
        raise ValueError('Published post body is empty after sanitization.')
    sanitized_scanner = _FeatureScanner()
    sanitized_scanner.feed(body_html)
    if len(sanitized_scanner.images) != len(scanner.images):
        raise ValueError('One or more images were lost during HTML sanitization.')

    document = html_to_tiptap_json(body_html)
    category = ', '.join(entry.labels) or None
    warnings = []
    if category and len(category) > 100:
        warnings.append('Labels were truncated to fit the existing 100-character resource_category field.')
        category = category[:100].rsplit(',', 1)[0].strip() or category[:100]
    if any(urlsplit(url).scheme == 'http' for url in scanner.images if url):
        warnings.append('Post contains HTTP image URLs; URLs were preserved without rehosting.')

    return PreparedPost(
        source=entry,
        title=entry.title,
        body_html=body_html,
        content_json=json.dumps(document, ensure_ascii=False, separators=(',', ':')),
        requested_slug=blogger_slug(entry.filename, entry.title),
        status=IMPORTABLE_STATUSES[entry.status],
        category=category,
        warnings=warnings,
    )


def database_target(engine: Any) -> str:
    url = engine.url
    host = url.host or 'local-file'
    port = f':{url.port}' if url.port else ''
    database = f'/{url.database}' if url.database else ''
    return f'{url.get_backend_name()}://{host}{port}{database}'


def load_database_snapshot_from_connection(
    connection: Any,
    *,
    for_update: bool = False,
) -> DatabaseSnapshot:
    """Load duplicate/slug state, optionally locking it for an atomic import."""
    inspector = inspect(connection)
    if not inspector.has_table('resources'):
        raise RuntimeError('Database does not contain the resources table.')
    columns = {item['name'] for item in inspector.get_columns('resources')}
    missing = REQUIRED_RESOURCE_COLUMNS - columns
    if missing:
        raise RuntimeError(f'Resources schema is missing: {", ".join(sorted(missing))}.')
    # SQLite does not support SELECT ... FOR UPDATE. Production MySQL does, and
    # uses these locks to keep duplicate/slug planning stable until commit.
    supports_for_update = connection.dialect.name != 'sqlite'
    suffix = ' FOR UPDATE' if for_update and supports_for_update else ''
    rows = connection.execute(text("""
            SELECT resource_id, resource_title, resource_slug, resource_type, resource_status,
                   resource_published_date, resource_is_deleted
            FROM resources
            WHERE resource_type = :blog_type
        """ + suffix), {'blog_type': 'blog'}).mappings().all()
    resources = [
        ExistingResource(
            resource_id=int(row['resource_id']),
            title=row['resource_title'] or '',
            slug=row['resource_slug'],
            resource_type=row['resource_type'] or '',
            status=row['resource_status'] or '',
            published_at=_coerce_database_datetime(row['resource_published_date']),
            is_deleted=bool(row['resource_is_deleted']),
        )
        for row in rows
    ]
    occupied_slugs = {
        row[0]
        for row in connection.execute(
            text(
                'SELECT resource_slug FROM resources '
                'WHERE resource_slug IS NOT NULL' + suffix
            )
        )
        if row[0]
    }
    resource_counts_by_type = {
        row['resource_type']: int(row['resource_count'])
        for row in connection.execute(text("""
            SELECT resource_type, COUNT(*) AS resource_count
            FROM resources
            GROUP BY resource_type
        """)).mappings()
    }
    return DatabaseSnapshot(
        target=database_target(connection.engine),
        resources=resources,
        occupied_slugs=occupied_slugs,
        resource_counts_by_type=resource_counts_by_type,
    )


def load_database_snapshot(engine: Any) -> DatabaseSnapshot:
    with engine.connect() as connection:
        return load_database_snapshot_from_connection(connection)


def _coerce_database_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return parse_datetime(str(value))


def _normalized_title(value: str) -> str:
    return re.sub(r'\s+', ' ', value or '').strip().casefold()


def duplicate_match(
    entry: BloggerEntry,
    snapshot: DatabaseSnapshot,
) -> tuple[ExistingResource, str] | None:
    title = _normalized_title(entry.title)
    requested_slug = blogger_slug(entry.filename, entry.title)
    for resource in snapshot.resources:
        if resource.resource_type != 'blog':
            continue
        same_title = _normalized_title(resource.title) == title
        same_day = bool(
            entry.published_at and resource.published_at
            and entry.published_at.date() == resource.published_at.date()
        )
        same_slug = resource.slug == requested_slug
        if same_title and same_slug and same_day:
            return resource, 'title+slug+published_date'
        if same_title and same_day:
            return resource, 'title+published_date'
        if same_slug and same_day:
            return resource, 'slug+published_date'
        if same_title and same_slug:
            return resource, 'title+slug'
    return None


def _unique_slug(requested: str, occupied: set[str]) -> str:
    if requested not in occupied:
        occupied.add(requested)
        return requested
    counter = 2
    while True:
        suffix = f'-{counter}'
        candidate = f'{requested[:255 - len(suffix)].rstrip("-")}{suffix}'
        if candidate not in occupied:
            occupied.add(candidate)
            return candidate
        counter += 1


def build_plan(
    feed: FeedData,
    snapshot: DatabaseSnapshot | None,
    *,
    allowed_statuses: set[str] | None = None,
) -> tuple[list[PreparedPost], dict[str, Any]]:
    effective_statuses = set(IMPORTABLE_STATUSES) if allowed_statuses is None else set(allowed_statuses)
    unsupported = effective_statuses - set(IMPORTABLE_STATUSES)
    if unsupported:
        raise ValueError(f'Unsupported import statuses requested: {sorted(unsupported)}.')
    type_counts = Counter(entry.entry_type or '<missing>' for entry in feed.entries)
    status_counts = Counter(entry.status or '<missing>' for entry in feed.entries if entry.entry_type == 'POST')
    all_labels = sorted({label for entry in feed.entries for label in entry.labels}, key=str.casefold)
    image_urls: list[str] = []
    problems: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    prepared: list[PreparedPost] = []

    candidates = [
        entry for entry in feed.entries
        if entry.entry_type == 'POST' and entry.status in effective_statuses
    ]
    candidates.sort(key=lambda item: (item.published_at or item.created_at or datetime.min, item.entry_id))
    occupied = set(snapshot.occupied_slugs) if snapshot else set()

    for entry in candidates:
        if snapshot:
            match = duplicate_match(entry, snapshot)
            if match:
                duplicates.append(
                    {
                        'blogger_entry_id': entry.entry_id,
                        'title': entry.title,
                        'resource_id': match[0].resource_id,
                        'resource_type': match[0].resource_type,
                        'reason': match[1],
                    }
                )
                continue
        try:
            post = prepare_post(entry)
            post.slug = _unique_slug(post.requested_slug, occupied)
            if post.slug != post.requested_slug:
                post.warnings.append(f'Slug collision: using {post.slug!r} instead of {post.requested_slug!r}.')
            scanner = _FeatureScanner()
            scanner.feed(post.body_html)
            image_urls.extend(scanner.images)
            for message in post.warnings:
                warnings.append({'blogger_entry_id': entry.entry_id, 'title': entry.title, 'message': message})
            prepared.append(post)
        except Exception as exc:
            problems.append(
                {'blogger_entry_id': entry.entry_id, 'title': entry.title, 'error': str(exc)}
            )

    skipped_non_posts = sum(value for key, value in type_counts.items() if key != 'POST')
    skipped_statuses = sum(value for key, value in status_counts.items() if key not in effective_statuses)
    skipped_items = skipped_non_posts + skipped_statuses + len(duplicates) + len(problems)
    planned_resources = [
        {
            'resource_type': 'blog',
            'resource_title': post.title,
            'resource_slug': post.slug,
            'resource_status': post.status,
            'resource_published_date': (
                post.source.published_at.isoformat() if post.status == 'published' else None
            ),
            'resource_date': (
                (post.source.created_at or post.source.published_at).isoformat()
                if post.source.created_at or post.source.published_at else None
            ),
            'resource_updated_date': (
                (post.source.updated_at or post.source.published_at or post.source.created_at).isoformat()
                if post.source.updated_at or post.source.published_at or post.source.created_at else None
            ),
            'resource_category': post.category,
            'body_sha256': sha256(post.body_html.encode('utf-8')).hexdigest(),
        }
        for post in prepared
    ]
    plan_sha256 = sha256(
        json.dumps(planned_resources, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()
    report = {
        'dry_run': True,
        'plan_sha256': plan_sha256,
        'feed': {
            'id': feed.feed_id,
            'title': feed.title,
            'sha256': feed.sha256,
        },
        'database': {
            'checked': snapshot is not None,
            'target': snapshot.target if snapshot else None,
            'existing_blog_posts': len(snapshot.resources) if snapshot else None,
            'resources_by_type': snapshot.resource_counts_by_type if snapshot else None,
        },
        'counts': {
            'total_entries': len(feed.entries),
            'posts': type_counts['POST'],
            'pages': type_counts['PAGE'],
            'comments': type_counts['COMMENT'],
            'drafts': status_counts['DRAFT'] + status_counts['SCHEDULED'],
            'published_posts': status_counts['LIVE'],
            'soft_trashed_posts': status_counts['SOFT_TRASHED'],
            'skipped_items': skipped_items,
            'duplicates': len(duplicates),
            'categories_labels': len(all_labels),
            'label_assignments': sum(len(entry.labels) for entry in feed.entries if entry.entry_type == 'POST'),
            'errors': len(problems),
            'warnings': len(warnings),
            'images': len(image_urls),
            'would_import': len(prepared),
        },
        'labels': all_labels,
        'image_urls': image_urls,
        'planned_resources': planned_resources,
        'duplicates': duplicates,
        'errors': problems,
        'warnings': warnings,
    }
    return prepared, report


def resource_fingerprint(post: PreparedPost) -> str:
    source = post.source
    published_at = source.published_at if post.status == 'published' else None
    values = {
        'title': post.title,
        'body': post.body_html,
        'slug': post.slug,
        'status': post.status,
        'published_at': _fingerprint_datetime(published_at),
        'category': post.category,
        'content_json': post.content_json,
        'is_deleted': False,
    }
    return sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def insert_post(connection: Any, post: PreparedPost) -> dict[str, Any]:
    """Insert one prepared Resource through a caller-owned transaction."""
    source = post.source
    result = connection.execute(
        text("""
            INSERT INTO resources (
                resource_title, resource_body, resource_type, resource_slug,
                resource_featured_image, resource_status, resource_published_date,
                resource_category, resource_content_json, resource_date,
                resource_updated_date, resource_is_deleted
            ) VALUES (
                :title, :body, 'blog', :slug, NULL, :status, :published_at,
                :category, :content_json, :created_at, :updated_at, :is_deleted
            )
        """),
        {
            'title': post.title,
            'body': post.body_html,
            'slug': post.slug,
            'status': post.status,
            'published_at': source.published_at if post.status == 'published' else None,
            'category': post.category,
            'content_json': post.content_json,
            'created_at': source.created_at or source.published_at or utc_now(),
            'updated_at': source.updated_at or source.published_at or source.created_at or utc_now(),
            'is_deleted': False,
        },
    )
    return {
        'resource_id': int(result.lastrowid),
        'blogger_entry_id': source.entry_id,
        'blogger_original_url': source.original_url,
        'title': post.title,
        'slug': post.slug,
        'published_at': source.published_at.isoformat() if source.published_at else None,
        'resource_fingerprint': resource_fingerprint(post),
    }


def import_posts(
    engine: Any,
    posts: list[PreparedPost],
    report: dict[str, Any],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    imported: list[dict[str, Any]] = []
    write_errors: list[dict[str, Any]] = []
    final_report = dict(report)
    final_report['dry_run'] = False
    final_report['imported'] = imported
    final_report['write_errors'] = write_errors
    final_report['counts'] = dict(report['counts'])
    final_report['counts']['imported'] = 0
    final_report['counts']['write_errors'] = 0
    if progress:
        progress(final_report)

    for post in posts:
        source = post.source
        imported_item = None
        try:
            with engine.begin() as connection:
                imported_item = insert_post(connection, post)
                imported.append(imported_item)
                final_report['counts']['imported'] = len(imported)
                if progress:
                    progress(final_report)
        except Exception as exc:
            if imported_item in imported:
                imported.remove(imported_item)
            write_errors.append(
                {'blogger_entry_id': source.entry_id, 'title': post.title, 'error': str(exc)}
            )
            final_report['counts']['imported'] = len(imported)
            final_report['counts']['write_errors'] = len(write_errors)
            if progress:
                progress(final_report)

    return final_report


def resource_row_fingerprint(row: dict[str, Any]) -> str:
    values = {
        'title': row['resource_title'],
        'body': row['resource_body'],
        'slug': row['resource_slug'],
        'status': row['resource_status'],
        'published_at': _fingerprint_datetime(
            _coerce_database_datetime(row['resource_published_date'])
        ),
        'category': row['resource_category'],
        'content_json': row['resource_content_json'],
        'is_deleted': bool(row['resource_is_deleted']),
    }
    return sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _fingerprint_datetime(value: datetime | None) -> str | None:
    # The deployed MySQL DATETIME columns store whole seconds.
    return value.replace(microsecond=0).isoformat() if value else None


def rollback_report(engine: Any, import_report: dict[str, Any]) -> dict[str, Any]:
    if import_report.get('dry_run') is not False:
        raise RuntimeError('Rollback requires a completed import report, not a dry-run report.')
    expected_target = import_report.get('database', {}).get('target')
    if expected_target != database_target(engine):
        raise RuntimeError('Rollback database target differs from the import report.')
    imported = import_report.get('imported') or []
    if not imported:
        raise RuntimeError('Import report contains no created Resource records.')
    fingerprints = {
        int(item['resource_id']): item.get('resource_fingerprint')
        for item in imported
    }
    if len(fingerprints) != len(imported) or any(not value for value in fingerprints.values()):
        raise RuntimeError('Import report has duplicate IDs or missing Resource fingerprints.')
    ids = sorted(fingerprints)
    select_statement = text("""
        SELECT r.resource_id, r.resource_title, r.resource_body, r.resource_slug,
               r.resource_status, r.resource_published_date, r.resource_category,
               r.resource_content_json, r.resource_is_deleted, r.resource_type,
               (SELECT COUNT(*) FROM comments c WHERE c.resource_id = r.resource_id) AS comment_count
        FROM resources r
        WHERE r.resource_id IN :resource_ids
    """).bindparams(bindparam('resource_ids', expanding=True))
    delete_statement = text(
        'DELETE FROM resources WHERE resource_id IN :resource_ids'
    ).bindparams(bindparam('resource_ids', expanding=True))
    with engine.begin() as connection:
        rows = connection.execute(
            select_statement,
            {'resource_ids': ids},
        ).mappings().all()
        protected = []
        found_ids = {int(row['resource_id']) for row in rows}
        for missing_id in sorted(set(ids) - found_ids):
            protected.append({'resource_id': missing_id, 'reason': 'Resource no longer exists'})
        for row in rows:
            if row['comment_count']:
                protected.append({'resource_id': row['resource_id'], 'reason': 'post has comments'})
            elif row['resource_type'] != 'blog':
                protected.append({'resource_id': row['resource_id'], 'reason': 'Resource is no longer a blog'})
            elif resource_row_fingerprint(dict(row)) != fingerprints[int(row['resource_id'])]:
                protected.append({'resource_id': row['resource_id'], 'reason': 'post changed after import'})
        if protected:
            raise RuntimeError(
                'Rollback refused because imported posts have later changes: '
                + json.dumps(protected, ensure_ascii=False)
            )
        connection.execute(delete_statement, {'resource_ids': ids})
    return {
        'source_import_plan_sha256': import_report.get('plan_sha256'),
        'rolled_back': len(ids),
        'resource_ids': ids,
    }
