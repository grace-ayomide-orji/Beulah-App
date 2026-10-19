"""Dry-run-only correction of verified Blogger prose paragraph boundaries."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json
import os
from pathlib import Path
import sys
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import bindparam, create_engine, text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from beulah_pkg.blogger_import import database_target  # noqa: E402
from beulah_pkg.blogger_normalization import (  # noqa: E402
    normalize_verified_prose_boundaries,
)


TARGET_BOUNDARY_COUNTS = {
    235: 1,
    301: 1,
    361: 3,
    418: 1,
    451: 1,
    475: 1,
    509: 4,
    571: 4,
    777: 1,
    778: 2,
    780: 9,
    783: 4,
    787: 4,
}
EXPECTED_RESOURCE_COUNT = 13
EXPECTED_BOUNDARY_COUNT = 36


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            'Dry-run-only split of 36 verified prose hard-break boundaries in '
            '13 imported Blogger Resources. This command has no database write mode.'
        )
    )
    parser.add_argument(
        '--base-report',
        type=Path,
        default=PROJECT_ROOT / 'logs' / 'blogger-normalization-dry-run.json',
        help='Verified 553-resource normalization report used for drift protection.',
    )
    parser.add_argument(
        '--report-json',
        type=Path,
        default=PROJECT_ROOT / 'logs' / 'blogger-paragraph-normalization-dry-run.json',
        help='JSON audit report containing complete before/after HTML and JSON.',
    )
    parser.add_argument(
        '--comparison-html',
        type=Path,
        default=PROJECT_ROOT / 'logs' / 'blogger-paragraph-normalization-before-after.html',
        help='Human-readable complete before/after HTML comparison.',
    )
    return parser.parse_args()


def value_hash(value: str | None) -> str:
    payload = b'null' if value is None else b'string\0' + value.encode('utf-8')
    return sha256(payload).hexdigest()


def load_base_resources(path: Path) -> dict[int, dict[str, Any]]:
    report = json.loads(path.read_text(encoding='utf-8'))
    if report.get('dry_run') is not True or report.get('database_writes_performed') is not False:
        raise RuntimeError('Base report is not a valid read-only normalization dry run.')
    if (report.get('counts') or {}).get('resources_successfully_normalized') != 553:
        raise RuntimeError('Base report does not contain 553 successfully normalized resources.')
    resources = report.get('resources') or []
    by_id = {int(item['resource_id']): item for item in resources}
    missing = sorted(set(TARGET_BOUNDARY_COUNTS) - set(by_id))
    if missing:
        raise RuntimeError(f'Base report is missing target Resource IDs: {missing}.')
    for resource_id in TARGET_BOUNDARY_COUNTS:
        item = by_id[resource_id]
        if item.get('resource_type') != 'blog' or item.get('status') != 'safe_to_normalize':
            raise RuntimeError(f'Base report does not mark Resource #{resource_id} as a safe blog.')
    return by_id


def load_rows(engine: Any) -> list[dict[str, Any]]:
    statement = text("""
        SELECT resource_id, resource_title, resource_type, resource_body,
               resource_content_json
        FROM resources
        WHERE resource_id IN :resource_ids
        ORDER BY resource_id
    """).bindparams(bindparam('resource_ids', expanding=True))
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(
            statement,
            {'resource_ids': sorted(TARGET_BOUNDARY_COUNTS)},
        ).mappings().all()]


def comparison_document(resources: list[dict[str, Any]]) -> str:
    sections = []
    for item in resources:
        sections.append(
            '<section>'
            f'<h2>#{item["resource_id"]} — {escape(item["title"])}</h2>'
            f'<p>Verified prose boundaries split: {item["boundaries_split"]}</p>'
            '<div class="comparison"><article><h3>Before</h3><pre>'
            f'{escape(item["original_resource_body"])}</pre></article>'
            '<article><h3>After</h3><pre>'
            f'{escape(item["normalized_resource_body"] or "")}</pre></article></div>'
            '</section>'
        )
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<title>Blogger paragraph normalization before/after</title>'
        '<style>body{font:16px/1.5 system-ui;margin:2rem;color:#222}'
        'section{border-top:2px solid #999;padding:1rem 0 2rem}'
        '.comparison{display:grid;grid-template-columns:1fr 1fr;gap:1rem}'
        'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f6f6;padding:1rem}'
        '@media(max-width:900px){.comparison{grid-template-columns:1fr}}</style>'
        '<h1>Blogger paragraph normalization dry-run comparison</h1>'
        '<p>No database writes were performed.</p>'
        + ''.join(sections)
        + '</html>'
    )


def main() -> int:
    args = parse_args()
    if len(TARGET_BOUNDARY_COUNTS) != EXPECTED_RESOURCE_COUNT:
        raise RuntimeError('Target allowlist must contain exactly 13 Resource IDs.')
    if sum(TARGET_BOUNDARY_COUNTS.values()) != EXPECTED_BOUNDARY_COUNT:
        raise RuntimeError('Target allowlist must contain exactly 36 verified boundaries.')

    load_dotenv(PROJECT_ROOT / '.env')
    database_uri = os.getenv('SQLALCHEMY_DATABASE_URI')
    if not database_uri:
        raise RuntimeError('SQLALCHEMY_DATABASE_URI is not configured.')
    engine = create_engine(database_uri)
    base_resources = load_base_resources(args.base_report)
    rows = load_rows(engine)
    row_by_id = {int(row['resource_id']): row for row in rows}

    resources = []
    errors = []
    counts = {
        'resources_expected': EXPECTED_RESOURCE_COUNT,
        'resources_scanned': 0,
        'resources_safe_to_normalize': 0,
        'resources_rejected': 0,
        'verified_prose_boundaries_expected': EXPECTED_BOUNDARY_COUNT,
        'verified_prose_boundaries_split': 0,
        'break_elements_removed': 0,
        'paragraph_elements_before': 0,
        'paragraph_elements_after': 0,
        'database_writes': 0,
    }

    for resource_id, expected_boundaries in TARGET_BOUNDARY_COUNTS.items():
        row = row_by_id.get(resource_id)
        base = base_resources[resource_id]
        record = {
            'resource_id': resource_id,
            'title': (row or {}).get('resource_title') or base.get('title') or '',
            'resource_type': (row or {}).get('resource_type'),
            'expected_boundaries': expected_boundaries,
            'original_resource_body': (row or {}).get('resource_body'),
            'original_resource_content_json': (row or {}).get('resource_content_json'),
            'normalized_resource_body': None,
            'normalized_resource_content_json': None,
            'original_hashes': {},
            'normalized_hashes': {},
            'boundaries_split': 0,
            'break_elements_removed': 0,
            'paragraphs_before': 0,
            'paragraphs_after': 0,
            'boundary_details': [],
            'validation': {},
            'validation_errors': [],
            'status': 'rejected',
        }
        if row is None:
            record['validation_errors'].append('Resource is missing from the database.')
        elif row.get('resource_type') != 'blog':
            record['validation_errors'].append(
                f'Expected blog Resource, found {row.get("resource_type")!r}.'
            )
        elif row.get('resource_body') != base.get('normalized_resource_body'):
            record['validation_errors'].append(
                'Current resource_body has drifted from the verified base report.'
            )
        elif row.get('resource_content_json') != base.get('normalized_resource_content_json'):
            record['validation_errors'].append(
                'Current resource_content_json has drifted from the verified base report.'
            )
        else:
            counts['resources_scanned'] += 1
            try:
                result = normalize_verified_prose_boundaries(
                    row['resource_body'],
                    row.get('resource_content_json'),
                    expected_boundaries=expected_boundaries,
                )
                record.update({
                    'normalized_resource_body': result.html,
                    'normalized_resource_content_json': result.content_json,
                    'boundaries_split': result.boundaries_split,
                    'break_elements_removed': result.break_elements_removed,
                    'paragraphs_before': result.paragraphs_before,
                    'paragraphs_after': result.paragraphs_after,
                    'boundary_details': result.boundary_details,
                    'validation_errors': result.validation_errors,
                    'validation': {
                        'visible_text_preserved': (
                            result.original_signature.visible_text
                            == result.normalized_signature.visible_text
                        ),
                        'meaningful_links_preserved': (
                            result.original_signature.meaningful_links
                            == result.normalized_signature.meaningful_links
                        ),
                        'anchors_preserved': (
                            result.original_signature.all_anchors
                            == result.normalized_signature.all_anchors
                        ),
                        'images_preserved': (
                            result.original_signature.images
                            == result.normalized_signature.images
                        ),
                        'lists_preserved': (
                            result.original_signature.lists
                            == result.normalized_signature.lists
                        ),
                        'headings_preserved': (
                            result.original_signature.headings
                            == result.normalized_signature.headings
                        ),
                        'inline_formatting_preserved': (
                            result.original_signature.formatting
                            == result.normalized_signature.formatting
                        ),
                        'remaining_line_breaks_preserved': (
                            result.normalized_signature.line_breaks
                            == result.original_signature.line_breaks
                            - result.break_elements_removed
                        ),
                    },
                })
                if not result.validation_errors:
                    record['status'] = 'safe_to_normalize'
                    counts['resources_safe_to_normalize'] += 1
                    counts['verified_prose_boundaries_split'] += result.boundaries_split
                    counts['break_elements_removed'] += result.break_elements_removed
                    counts['paragraph_elements_before'] += result.paragraphs_before
                    counts['paragraph_elements_after'] += result.paragraphs_after
            except Exception as exc:
                record['validation_errors'].append(f'{type(exc).__name__}: {exc}')

        original_body = record['original_resource_body']
        original_json = record['original_resource_content_json']
        normalized_body = record['normalized_resource_body']
        normalized_json = record['normalized_resource_content_json']
        record['original_hashes'] = {
            'resource_body_sha256': value_hash(original_body),
            'resource_content_json_sha256': value_hash(original_json),
        }
        record['normalized_hashes'] = {
            'resource_body_sha256': value_hash(normalized_body),
            'resource_content_json_sha256': value_hash(normalized_json),
        }
        if record['status'] != 'safe_to_normalize':
            counts['resources_rejected'] += 1
            errors.append({
                'resource_id': resource_id,
                'title': record['title'],
                'reasons': record['validation_errors'],
            })
        resources.append(record)

    if counts['verified_prose_boundaries_split'] != EXPECTED_BOUNDARY_COUNT:
        errors.append({
            'reason': (
                f'Expected {EXPECTED_BOUNDARY_COUNT} total verified boundaries, '
                f'validated {counts["verified_prose_boundaries_split"]}.'
            ),
        })

    report = {
        'dry_run': True,
        'database_writes_performed': False,
        'write_mode_available': False,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'database': database_target(engine),
        'base_report': str(args.base_report.resolve()),
        'target_resource_ids': list(TARGET_BOUNDARY_COUNTS),
        'counts': counts,
        'errors': errors,
        'resources': resources,
    }
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    args.comparison_html.parent.mkdir(parents=True, exist_ok=True)
    args.comparison_html.write_text(comparison_document(resources), encoding='utf-8')

    print('Targeted Blogger paragraph normalization dry run')
    print(f'Database: {report["database"]}')
    print('Database writes performed: no')
    print('Write mode available: no')
    for key, value in counts.items():
        print(f'{key.replace("_", " ").title()}: {value}')
    print(f'Errors: {len(errors)}')
    print(f'JSON report: {args.report_json.resolve()}')
    print(f'Before/after comparison: {args.comparison_html.resolve()}')
    for item in errors:
        prefix = f'#{item["resource_id"]}: ' if item.get('resource_id') else ''
        reasons = '; '.join(item.get('reasons') or [item.get('reason', '')])
        print(f'  {prefix}{reasons}')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
