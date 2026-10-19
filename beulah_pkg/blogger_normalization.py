"""Semantic HTML normalization for the one-time Blogger migration."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html import escape
import json
import re
from typing import Any, Iterable

from bleach.html5lib_shim import HTMLParser

from beulah_pkg.blogger_import import html_to_tiptap_json, validate_tiptap_json


BLOCK_TAGS = {
    'blockquote', 'div', 'figure', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'li', 'ol', 'p', 'pre', 'ul',
}
INLINE_TAGS = {'a', 'code', 'em', 's', 'span', 'strong', 'sup', 'u'}
VOID_TAGS = {'br', 'hr', 'img'}
MARK_ORDER = ('link', 'bold', 'italic', 'underline', 'strike', 'superscript', 'code')
WHITESPACE_RE = re.compile(r'\s+', re.UNICODE)


@dataclass
class SourceNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class Mark:
    kind: str
    attrs: tuple[tuple[str, str], ...] = ()

    def attr_dict(self) -> dict[str, str]:
        return dict(self.attrs)


@dataclass
class InlineToken:
    kind: str
    text: str = ''
    marks: tuple[Mark, ...] = ()
    attrs: dict[str, str] = field(default_factory=dict)


@dataclass
class Block:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Any] = field(default_factory=list)


@dataclass
class NormalizationMetrics:
    div_paragraph_blocks_converted: int = 0
    empty_spacer_blocks_removed: int = 0
    paragraphs_produced: int = 0
    malformed_nesting_repairs: int = 0
    html5_parse_errors: int = 0
    real_line_breaks_preserved: int = 0


@dataclass
class SemanticSignature:
    visible_text: str
    meaningful_links: list[tuple[str, str]]
    all_anchors: list[dict[str, str]]
    empty_url_anchors: list[dict[str, str]]
    images: list[tuple[str, str, str]]
    lists: list[tuple[str, str, tuple[str, ...]]]
    headings: list[tuple[str, str]]
    formatting: dict[str, str]
    line_breaks: int


@dataclass
class NormalizationResult:
    html: str
    content_json: str
    metrics: NormalizationMetrics
    original_signature: SemanticSignature
    normalized_signature: SemanticSignature
    validation_errors: list[str]
    old_json_issues: list[str]


@dataclass
class ProseBoundaryNormalizationResult:
    html: str
    content_json: str
    boundaries_split: int
    break_elements_removed: int
    paragraphs_before: int
    paragraphs_after: int
    boundary_details: list[dict[str, Any]]
    original_signature: SemanticSignature
    normalized_signature: SemanticSignature
    validation_errors: list[str]


def _tag_name(value: Any) -> str:
    if not isinstance(value, str):
        return ''
    return value.rsplit('}', 1)[-1].lower()


def _from_etree(element: Any) -> SourceNode:
    node = SourceNode(
        tag=_tag_name(element.tag),
        attrs={_tag_name(key): str(value) for key, value in element.attrib.items()},
    )
    if element.text:
        node.children.append(element.text)
    for child in element:
        if isinstance(child.tag, str):
            node.children.append(_from_etree(child))
        if child.tail:
            node.children.append(child.tail)
    return node


def parse_html5_fragment(value: str) -> tuple[SourceNode, int]:
    parser = HTMLParser(namespaceHTMLElements=False)
    fragment = parser.parseFragment(value or '', container='div')
    root = _from_etree(fragment)
    root.tag = 'root'
    return root, len(parser.errors)


def _normalize_space(value: str) -> str:
    return WHITESPACE_RE.sub(' ', (value or '').replace('\xa0', ' '))


def _plain_text(node: Any, block_boundaries: bool = False) -> str:
    if isinstance(node, str):
        return node
    if node.tag == 'br':
        return '\n'
    if node.tag in {'img', 'hr'}:
        return ' ' if block_boundaries else ''
    text = ''.join(_plain_text(child, block_boundaries) for child in node.children)
    if block_boundaries and node.tag in BLOCK_TAGS:
        return f' {text} '
    return text


def _normalized_visible_text(node: SourceNode) -> str:
    return _normalize_space(_plain_text(node, block_boundaries=True)).strip()


def _style_map(value: str) -> dict[str, str]:
    result = {}
    for declaration in (value or '').split(';'):
        if ':' in declaration:
            key, val = declaration.split(':', 1)
            result[key.strip().lower()] = val.strip().lower()
    return result


def _node_marks(node: SourceNode, inherited: tuple[Mark, ...]) -> tuple[Mark, ...]:
    marks = list(inherited)
    tag_mark = {
        'strong': Mark('bold'),
        'em': Mark('italic'),
        'u': Mark('underline'),
        's': Mark('strike'),
        'sup': Mark('superscript'),
        'code': Mark('code'),
    }.get(node.tag)
    if tag_mark:
        marks.append(tag_mark)

    styles = _style_map(node.attrs.get('style', ''))
    weight = styles.get('font-weight', '')
    if weight == 'bold' or (weight.isdigit() and int(weight) >= 600):
        marks.append(Mark('bold'))
    if styles.get('font-style') in {'italic', 'oblique'}:
        marks.append(Mark('italic'))
    decoration = styles.get('text-decoration', '')
    if 'underline' in decoration:
        marks.append(Mark('underline'))
    if 'line-through' in decoration:
        marks.append(Mark('strike'))

    if node.tag == 'a':
        attrs = tuple(
            (key, node.attrs.get(key, ''))
            for key in ('href', 'title', 'target', 'rel')
            if key in node.attrs
        )
        marks.append(Mark('link', attrs))

    unique = {}
    for mark in marks:
        unique[(mark.kind, mark.attrs)] = mark
    return tuple(
        sorted(unique.values(), key=lambda item: MARK_ORDER.index(item.kind))
    )


def _has_block_descendant(node: SourceNode) -> bool:
    for child in node.children:
        if not isinstance(child, SourceNode):
            continue
        if child.tag in BLOCK_TAGS:
            return True
        if _has_block_descendant(child):
            return True
    return False


def _is_empty_anchor(node: SourceNode) -> bool:
    if node.tag != 'a':
        return False
    if any(
        isinstance(child, SourceNode) and child.tag == 'img'
        for child in node.children
    ):
        return False
    return not _normalize_space(_plain_text(node)).strip()


def _is_spacer_source(node: SourceNode) -> bool:
    if _normalize_space(_plain_text(node)).strip():
        return False
    for child in node.children:
        if isinstance(child, SourceNode):
            if child.tag in {'img', 'hr', 'ul', 'ol'}:
                return False
            if child.tag == 'a' and child.attrs.get('href'):
                return False
    return True


def _trim_inline(tokens: list[InlineToken]) -> list[InlineToken]:
    tokens = [token for token in tokens if token.kind != 'text' or token.text]
    while tokens and tokens[0].kind == 'text':
        tokens[0].text = tokens[0].text.lstrip(' ')
        if tokens[0].text:
            break
        tokens.pop(0)
    while tokens and tokens[-1].kind == 'text':
        tokens[-1].text = tokens[-1].text.rstrip(' ')
        if tokens[-1].text:
            break
        tokens.pop()
    return tokens


def _merge_inline(tokens: list[InlineToken]) -> list[InlineToken]:
    result: list[InlineToken] = []
    previous_text: InlineToken | None = None
    for token in tokens:
        if token.kind == 'text':
            token.text = _normalize_space(token.text)
            if previous_text and previous_text.text.endswith(' ') and token.text.startswith(' '):
                token.text = token.text.lstrip(' ')
            if not token.text:
                continue
            if result and result[-1].kind == 'text' and result[-1].marks == token.marks:
                result[-1].text += token.text
                previous_text = result[-1]
                continue
            previous_text = token
        elif token.kind == 'br':
            previous_text = None
        result.append(token)
    return _trim_inline(result)


def _tokens_have_content(tokens: Iterable[InlineToken]) -> bool:
    return any(
        token.kind == 'empty_anchor'
        or (token.kind == 'text' and token.text.strip())
        for token in tokens
    )


class SemanticNormalizer:
    def __init__(self) -> None:
        self.metrics = NormalizationMetrics()

    def normalize(self, root: SourceNode) -> list[Block]:
        self.metrics.malformed_nesting_repairs = self._count_malformed_nesting(root)
        return self._children_to_blocks(root.children, (), default_tag='p')

    def _count_malformed_nesting(self, node: SourceNode) -> int:
        count = 0
        if node.tag in INLINE_TAGS and _has_block_descendant(node):
            count += 1
        for child in node.children:
            if isinstance(child, SourceNode):
                count += self._count_malformed_nesting(child)
        return count

    def _mixed_items(
        self,
        value: Any,
        inherited: tuple[Mark, ...],
    ) -> list[Any]:
        if isinstance(value, str):
            return [InlineToken('text', value, inherited)]
        if value.tag == 'br':
            return [InlineToken('br')]
        if value.tag == 'img':
            return [
                Block(
                    'img',
                    {
                        key: value.attrs.get(key, '')
                        for key in ('src', 'alt', 'title')
                        if key in value.attrs
                    },
                )
            ]
        if value.tag == 'hr':
            return [Block('hr')]
        if value.tag in BLOCK_TAGS:
            return self._node_to_blocks(value, inherited)

        marks = _node_marks(value, inherited)
        if _is_empty_anchor(value):
            return [
                InlineToken(
                    'empty_anchor',
                    attrs={
                        key: value.attrs.get(key, '')
                        for key in ('href', 'title', 'target', 'rel')
                        if key in value.attrs
                    },
                )
            ]

        result: list[Any] = []
        for child in value.children:
            result.extend(self._mixed_items(child, marks))
        return result

    def _flush_inline(
        self,
        target: list[Block],
        tokens: list[InlineToken],
        tag: str,
        from_div: bool = False,
        attrs: dict[str, str] | None = None,
    ) -> None:
        tokens = _merge_inline(tokens)
        segments: list[list[InlineToken]] = []
        current: list[InlineToken] = []
        index = 0
        while index < len(tokens):
            if tokens[index].kind != 'br':
                current.append(tokens[index])
                index += 1
                continue
            end = index
            while end < len(tokens) and tokens[end].kind == 'br':
                end += 1
            run_length = end - index
            if run_length >= 2:
                if current:
                    segments.append(current)
                    current = []
                self.metrics.empty_spacer_blocks_removed += 1
            else:
                current.append(tokens[index])
            index = end
        if current:
            segments.append(current)

        if not segments and tokens:
            self.metrics.empty_spacer_blocks_removed += 1
            return

        for segment in segments:
            segment = _trim_inline(segment)
            if not _tokens_have_content(segment):
                self.metrics.empty_spacer_blocks_removed += 1
                continue
            block = Block(tag, attrs or {}, segment)
            target.append(block)
            if tag == 'p':
                self.metrics.paragraphs_produced += 1
                if from_div:
                    self.metrics.div_paragraph_blocks_converted += 1

    def _children_to_blocks(
        self,
        children: Iterable[Any],
        inherited: tuple[Mark, ...],
        default_tag: str,
        from_div: bool = False,
        attrs: dict[str, str] | None = None,
    ) -> list[Block]:
        result: list[Block] = []
        inline: list[InlineToken] = []
        for child in children:
            for item in self._mixed_items(child, inherited):
                if isinstance(item, InlineToken):
                    inline.append(item)
                    continue
                self._flush_inline(result, inline, default_tag, from_div, attrs)
                inline = []
                result.append(item)
        self._flush_inline(result, inline, default_tag, from_div, attrs)
        return result

    def _node_to_blocks(
        self,
        node: SourceNode,
        inherited: tuple[Mark, ...],
    ) -> list[Block]:
        marks = _node_marks(node, inherited)
        if node.tag == 'div':
            blocks = self._children_to_blocks(
                node.children,
                marks,
                default_tag='p',
                from_div=True,
            )
            if not blocks and _is_spacer_source(node):
                self.metrics.empty_spacer_blocks_removed += 1
            return blocks
        if node.tag == 'p':
            return self._children_to_blocks(node.children, marks, default_tag='p')
        if node.tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            return self._children_to_blocks(node.children, marks, default_tag=node.tag)
        if node.tag == 'figure':
            return self._children_to_blocks(node.children, marks, default_tag='p')
        if node.tag == 'blockquote':
            children = self._children_to_blocks(node.children, marks, default_tag='p')
            return [Block('blockquote', children=children)] if children else []
        if node.tag == 'pre':
            value = _plain_text(node).replace('\xa0', ' ')
            return [Block('pre', children=[InlineToken('text', value)])] if value else []
        if node.tag in {'ul', 'ol'}:
            items = []
            for child in node.children:
                if isinstance(child, SourceNode) and child.tag == 'li':
                    item_blocks = self._children_to_blocks(
                        child.children,
                        _node_marks(child, marks),
                        default_tag='p',
                    )
                    items.append(Block('li', children=item_blocks or [Block('p')]))
            attrs = {}
            if node.tag == 'ol' and node.attrs.get('start'):
                attrs['start'] = node.attrs['start']
            return [Block(node.tag, attrs, items)] if items else []
        if node.tag == 'li':
            return self._children_to_blocks(node.children, marks, default_tag='p')
        return self._children_to_blocks(node.children, marks, default_tag='p')


def _serialize_attrs(attrs: dict[str, str], keys: Iterable[str]) -> str:
    parts = []
    for key in keys:
        if key in attrs:
            parts.append(f' {key}="{escape(attrs[key], quote=True)}"')
    return ''.join(parts)


def _serialize_inline(token: InlineToken) -> str:
    if token.kind == 'br':
        return '<br>'
    if token.kind == 'empty_anchor':
        attrs = _serialize_attrs(token.attrs, ('href', 'title', 'target', 'rel'))
        return f'<a{attrs}></a>'
    value = escape(token.text, quote=False)
    for mark in reversed(token.marks):
        if mark.kind == 'link':
            attrs = _serialize_attrs(mark.attr_dict(), ('href', 'title', 'target', 'rel'))
            value = f'<a{attrs}>{value}</a>'
        else:
            tag = {
                'bold': 'strong',
                'italic': 'em',
                'underline': 'u',
                'strike': 's',
                'superscript': 'sup',
                'code': 'code',
            }[mark.kind]
            value = f'<{tag}>{value}</{tag}>'
    return value


def _serialize_block(block: Block) -> str:
    if block.tag == 'img':
        return f'<img{_serialize_attrs(block.attrs, ("src", "alt", "title"))}>'
    if block.tag == 'hr':
        return '<hr>'
    if block.tag in {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre'}:
        value = ''.join(_serialize_inline(item) for item in block.children)
        return f'<{block.tag}>{value}</{block.tag}>'
    if block.tag == 'blockquote':
        return '<blockquote>' + ''.join(_serialize_block(item) for item in block.children) + '</blockquote>'
    if block.tag in {'ul', 'ol'}:
        attrs = _serialize_attrs(block.attrs, ('start',)) if block.tag == 'ol' else ''
        return f'<{block.tag}{attrs}>' + ''.join(_serialize_block(item) for item in block.children) + f'</{block.tag}>'
    if block.tag == 'li':
        if len(block.children) == 1 and block.children[0].tag == 'p':
            value = ''.join(_serialize_inline(item) for item in block.children[0].children)
        else:
            value = ''.join(_serialize_block(item) for item in block.children)
        return f'<li>{value}</li>'
    raise ValueError(f'Unsupported canonical block: {block.tag}')


def serialize_blocks(blocks: Iterable[Block]) -> str:
    return ''.join(_serialize_block(block) for block in blocks)


def _semantic_signature_from_root(root: SourceNode) -> SemanticSignature:
    links = []
    anchors = []
    empty_url_anchors = []
    images = []
    lists = []
    headings = []
    formatting_parts: dict[str, list[str]] = defaultdict(list)
    line_breaks = 0

    def visit(node: Any, active: frozenset[str] = frozenset()) -> None:
        nonlocal line_breaks
        if isinstance(node, str):
            for mark in active:
                formatting_parts[mark].append(node)
            return
        marks = set(active)
        tag_marks = {
            'strong': 'bold', 'em': 'italic', 'u': 'underline', 's': 'strike',
            'sup': 'superscript',
        }
        if node.tag in tag_marks:
            marks.add(tag_marks[node.tag])
        styles = _style_map(node.attrs.get('style', ''))
        weight = styles.get('font-weight', '')
        if weight == 'bold' or (weight.isdigit() and int(weight) >= 600):
            marks.add('bold')
        if styles.get('font-style') in {'italic', 'oblique'}:
            marks.add('italic')
        decoration = styles.get('text-decoration', '')
        if 'underline' in decoration:
            marks.add('underline')
        if 'line-through' in decoration:
            marks.add('strike')

        if node.tag == 'a':
            item = {
                key: node.attrs.get(key, '')
                for key in ('href', 'title', 'target', 'rel')
                if key in node.attrs
            }
            item['text'] = _normalize_space(_plain_text(node)).strip()
            anchors.append(item)
            if item.get('href') and item['text']:
                links.append((item['href'], item['text']))
            if item.get('href') and not item['text']:
                empty_url_anchors.append(item)
        elif node.tag == 'img':
            images.append(tuple(node.attrs.get(key, '') for key in ('src', 'alt', 'title')))
        elif node.tag in {'ul', 'ol'}:
            item_texts = []
            for child in node.children:
                if isinstance(child, SourceNode) and child.tag == 'li':
                    item_texts.append(_normalize_space(_plain_text(child, True)).strip())
            lists.append((node.tag, node.attrs.get('start', '1'), tuple(item_texts)))
        elif node.tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            headings.append((node.tag, _normalize_space(_plain_text(node)).strip()))
        elif node.tag == 'br':
            line_breaks += 1

        for child in node.children:
            visit(child, frozenset(marks))

    visit(root)
    formatting = {
        key: _normalize_space(''.join(values)).replace(' ', '')
        for key, values in formatting_parts.items()
    }
    for key in ('bold', 'italic', 'underline', 'strike', 'superscript'):
        formatting.setdefault(key, '')
    return SemanticSignature(
        visible_text=_normalized_visible_text(root),
        meaningful_links=links,
        all_anchors=anchors,
        empty_url_anchors=empty_url_anchors,
        images=images,
        lists=lists,
        headings=headings,
        formatting=formatting,
        line_breaks=line_breaks,
    )


def semantic_signature(value: str) -> SemanticSignature:
    root, _ = parse_html5_fragment(value)
    return _semantic_signature_from_root(root)


def _json_signature(document: dict[str, Any]) -> SemanticSignature:
    links = []
    images = []
    lists = []
    headings = []
    formatting_parts: dict[str, list[str]] = defaultdict(list)
    line_breaks = 0

    def node_text(node: dict[str, Any]) -> str:
        if node.get('type') == 'text':
            return node.get('text', '')
        if node.get('type') == 'hardBreak':
            return '\n'
        value = ''.join(node_text(child) for child in node.get('content', []))
        if node.get('type') in {
            'blockquote', 'bulletList', 'codeBlock', 'heading', 'listItem',
            'orderedList', 'paragraph',
        }:
            return f' {value} '
        return value

    def visit(node: dict[str, Any]) -> None:
        nonlocal line_breaks
        node_type = node.get('type')
        if node_type == 'text':
            value = node.get('text', '')
            for mark in node.get('marks', []):
                mark_type = mark.get('type')
                if mark_type == 'link':
                    href = (mark.get('attrs') or {}).get('href') or ''
                    if href and value.strip():
                        links.append((href, _normalize_space(value).strip()))
                elif mark_type in {'bold', 'italic', 'underline', 'strike', 'superscript'}:
                    formatting_parts[mark_type].append(value)
        elif node_type == 'hardBreak':
            line_breaks += 1
        elif node_type == 'image':
            attrs = node.get('attrs') or {}
            images.append(tuple(attrs.get(key) or '' for key in ('src', 'alt', 'title')))
        elif node_type in {'bulletList', 'orderedList'}:
            tag = 'ul' if node_type == 'bulletList' else 'ol'
            start = str((node.get('attrs') or {}).get('start') or 1)
            item_texts = tuple(
                _normalize_space(node_text(child)).strip()
                for child in node.get('content', [])
                if child.get('type') == 'listItem'
            )
            lists.append((tag, start, item_texts))
        elif node_type == 'heading':
            level = (node.get('attrs') or {}).get('level') or 1
            headings.append((f'h{level}', _normalize_space(node_text(node)).strip()))
        for child in node.get('content', []):
            visit(child)

    visit(document)
    formatting = {
        key: _normalize_space(''.join(values)).replace(' ', '')
        for key, values in formatting_parts.items()
    }
    for key in ('bold', 'italic', 'underline', 'strike', 'superscript'):
        formatting.setdefault(key, '')
    return SemanticSignature(
        visible_text=_normalize_space(node_text(document)).strip(),
        meaningful_links=links,
        all_anchors=[],
        empty_url_anchors=[],
        images=images,
        lists=lists,
        headings=headings,
        formatting=formatting,
        line_breaks=line_breaks,
    )


def _signature_errors(
    expected: SemanticSignature,
    actual: SemanticSignature,
    *,
    compare_anchors: bool,
    compare_line_breaks: bool,
) -> list[str]:
    errors = []
    if expected.visible_text != actual.visible_text:
        errors.append('visible text differs')
    if expected.meaningful_links != actual.meaningful_links:
        errors.append('meaningful linked text or href differs')
    if compare_anchors and expected.all_anchors != actual.all_anchors:
        errors.append('anchor attributes or positions differ')
    if expected.images != actual.images:
        errors.append('image count or src/alt/title attributes differ')
    if expected.lists != actual.lists:
        errors.append('ordered/unordered list structure differs')
    if expected.headings != actual.headings:
        errors.append('heading levels or text differ')
    for mark in ('bold', 'italic', 'underline', 'strike', 'superscript'):
        if expected.formatting.get(mark, '') != actual.formatting.get(mark, ''):
            errors.append(f'{mark} formatting differs')
    if compare_line_breaks and expected.line_breaks != actual.line_breaks:
        errors.append('meaningful line-break count differs')
    return errors


def _old_json_issues(original: SemanticSignature, value: str | None) -> list[str]:
    if not value:
        return ['resource_content_json is empty']
    try:
        document = json.loads(value)
        validate_tiptap_json(document)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return [f'resource_content_json is invalid: {exc}']
    return _signature_errors(
        original,
        _json_signature(document),
        compare_anchors=False,
        compare_line_breaks=False,
    )


def normalize_resource_html(
    resource_body: str,
    old_content_json: str | None = None,
) -> NormalizationResult:
    original_root, parse_errors = parse_html5_fragment(resource_body)
    original_signature = _semantic_signature_from_root(original_root)
    normalizer = SemanticNormalizer()
    normalizer.metrics.html5_parse_errors = parse_errors
    blocks = normalizer.normalize(original_root)
    normalized_html = serialize_blocks(blocks)
    normalized_signature = semantic_signature(normalized_html)

    validation_errors = _signature_errors(
        original_signature,
        normalized_signature,
        compare_anchors=True,
        compare_line_breaks=False,
    )
    normalizer.metrics.real_line_breaks_preserved = normalized_signature.line_breaks

    document = html_to_tiptap_json(normalized_html)
    validate_tiptap_json(document)
    json_signature = _json_signature(document)
    validation_errors.extend(
        f'Tiptap JSON: {item}'
        for item in _signature_errors(
            normalized_signature,
            json_signature,
            compare_anchors=False,
            compare_line_breaks=True,
        )
    )

    content_json = json.dumps(document, ensure_ascii=False, separators=(',', ':'))
    second_root, _ = parse_html5_fragment(normalized_html)
    second_normalizer = SemanticNormalizer()
    second_html = serialize_blocks(second_normalizer.normalize(second_root))
    if second_html != normalized_html:
        validation_errors.append('normalization is not idempotent')

    return NormalizationResult(
        html=normalized_html,
        content_json=content_json,
        metrics=normalizer.metrics,
        original_signature=original_signature,
        normalized_signature=normalized_signature,
        validation_errors=list(dict.fromkeys(validation_errors)),
        old_json_issues=_old_json_issues(original_signature, old_content_json),
    )


def _inline_text(tokens: Iterable[InlineToken]) -> str:
    parts = []
    for token in tokens:
        if token.kind == 'text':
            parts.append(token.text)
        elif token.kind == 'br':
            parts.append('\n')
    return ''.join(parts)


def _inline_has_visible_text(tokens: Iterable[InlineToken]) -> bool:
    return bool(_normalize_space(_inline_text(tokens)).strip())


def _split_paragraph_block(
    block: Block,
    paragraph_index: int,
) -> tuple[list[Block], list[dict[str, Any]], int]:
    """Split visible prose around direct hard breaks, retaining edge breaks."""
    tokens = block.children
    segments: list[list[InlineToken]] = []
    details: list[dict[str, Any]] = []
    current: list[InlineToken] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not isinstance(token, InlineToken) or token.kind != 'br':
            current.append(token)
            index += 1
            continue

        end = index + 1
        while end < len(tokens):
            candidate = tokens[end]
            if not isinstance(candidate, InlineToken):
                break
            if candidate.kind == 'br':
                end += 1
                continue
            if candidate.kind == 'text' and not candidate.text.strip():
                end += 1
                continue
            break

        run = tokens[index:end]
        break_count = sum(
            isinstance(item, InlineToken) and item.kind == 'br'
            for item in run
        )
        remaining = tokens[end:]
        if _inline_has_visible_text(current) and _inline_has_visible_text(remaining):
            before = _normalize_space(_inline_text(current)).strip()
            after = _normalize_space(_inline_text(remaining)).strip()
            segments.append(_trim_inline(current))
            current = []
            details.append({
                'paragraph_index': paragraph_index,
                'boundary_index': len(details) + 1,
                'break_elements': break_count,
                'before': before[-160:],
                'after': after[:160],
            })
        else:
            current.extend(run)
        index = end

    segments.append(_trim_inline(current))
    if not details:
        return [block], [], 0
    return (
        [Block('p', dict(block.attrs), segment) for segment in segments],
        details,
        sum(item['break_elements'] for item in details),
    )


def normalize_verified_prose_boundaries(
    resource_body: str,
    old_content_json: str | None,
    *,
    expected_boundaries: int,
) -> ProseBoundaryNormalizationResult:
    """Replace verified top-level prose hard breaks with semantic paragraphs.

    Only a direct ``br`` run inside a top-level paragraph with visible text on
    both sides is eligible. Leading/trailing breaks and every nested structure
    (including lists and blockquotes) are retained as-is.
    """
    root, parse_errors = parse_html5_fragment(resource_body)
    errors = []
    if parse_errors:
        errors.append(f'current HTML has {parse_errors} HTML5 parse errors')

    original_signature = _semantic_signature_from_root(root)
    normalizer = SemanticNormalizer()
    blocks = normalizer.normalize(root)
    canonical_before = serialize_blocks(blocks)
    if canonical_before != resource_body:
        errors.append('current resource_body is not canonical/idempotent before splitting')

    paragraphs_before = sum(block.tag == 'p' for block in blocks)
    transformed: list[Block] = []
    boundary_details: list[dict[str, Any]] = []
    break_elements_removed = 0
    paragraph_index = 0
    for block in blocks:
        if block.tag != 'p':
            transformed.append(block)
            continue
        paragraph_index += 1
        replacements, details, removed = _split_paragraph_block(block, paragraph_index)
        for detail in details:
            detail['boundary_index'] = len(boundary_details) + 1
        transformed.extend(replacements)
        boundary_details.extend(details)
        break_elements_removed += removed

    boundaries_split = len(boundary_details)
    if boundaries_split != expected_boundaries:
        errors.append(
            f'expected {expected_boundaries} verified prose boundaries, found {boundaries_split}'
        )

    normalized_html = serialize_blocks(transformed)
    normalized_signature = semantic_signature(normalized_html)
    errors.extend(_signature_errors(
        original_signature,
        normalized_signature,
        compare_anchors=True,
        compare_line_breaks=False,
    ))
    expected_line_breaks = original_signature.line_breaks - break_elements_removed
    if normalized_signature.line_breaks != expected_line_breaks:
        errors.append(
            'unexpected hard-break count after splitting: '
            f'expected {expected_line_breaks}, found {normalized_signature.line_breaks}'
        )

    paragraphs_after = sum(block.tag == 'p' for block in transformed)
    if paragraphs_after != paragraphs_before + boundaries_split:
        errors.append('paragraph count did not increase by the verified boundary count')

    document = html_to_tiptap_json(normalized_html)
    validate_tiptap_json(document)
    json_signature = _json_signature(document)
    errors.extend(
        f'Tiptap JSON: {item}'
        for item in _signature_errors(
            normalized_signature,
            json_signature,
            compare_anchors=False,
            compare_line_breaks=True,
        )
    )
    content_json = json.dumps(document, ensure_ascii=False, separators=(',', ':'))

    canonical_check = normalize_resource_html(normalized_html, content_json)
    if canonical_check.validation_errors:
        errors.extend(f'canonical output: {item}' for item in canonical_check.validation_errors)
    if canonical_check.html != normalized_html:
        errors.append('normalized HTML is not idempotent')
    if canonical_check.content_json != content_json:
        errors.append('normalized Tiptap JSON is not idempotent')

    return ProseBoundaryNormalizationResult(
        html=normalized_html,
        content_json=content_json,
        boundaries_split=boundaries_split,
        break_elements_removed=break_elements_removed,
        paragraphs_before=paragraphs_before,
        paragraphs_after=paragraphs_after,
        boundary_details=boundary_details,
        original_signature=original_signature,
        normalized_signature=normalized_signature,
        validation_errors=list(dict.fromkeys(errors)),
    )
