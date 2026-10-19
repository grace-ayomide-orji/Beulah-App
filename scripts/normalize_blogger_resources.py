"""Normalize the one-time Blogger import with guarded dry-run/commit/rollback modes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
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
from beulah_pkg.blogger_normalization import normalize_resource_html  # noqa: E402


SAMPLE_IDS = {
    'ordinary text-heavy post': 235,
    'malformed nested HTML': 353,
    'italics': 248,
    'superscript': 312,
    'meaningful link': 377,
    'list': 264,
    'image': 411,
    'previously incomplete Tiptap JSON': 764,
}

EXPECTED_RESOURCE_COUNT = 553


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Canonical HTML/Tiptap normalization for imported Blogger Resources.'
    )
    parser.add_argument(
        '--import-report',
        type=Path,
        default=PROJECT_ROOT / 'logs' / 'blogger-import.json',
        help='Committed Blogger import report containing the exact Resource IDs.',
    )
    parser.add_argument(
        '--report-json',
        type=Path,
        default=PROJECT_ROOT / 'logs' / 'blogger-normalization-dry-run.json',
        help='Local dry-run backup and audit report to create.',
    )
    parser.add_argument(
        '--verified-report',
        type=Path,
        help='Previously verified dry-run report. Required for --commit and --rollback.',
    )
    parser.add_argument(
        '--commit',
        action='store_true',
        help='Explicitly authorize database writes using --verified-report.',
    )
    parser.add_argument(
        '--rollback',
        action='store_true',
        help='Restore both fields from --verified-report; also requires --commit.',
    )
    parser.add_argument(
        '--commit-report',
        type=Path,
        help='Local commit/rollback audit report path.',
    )
    return parser.parse_args()


def value_hash(value: str | None) -> str:
    """Hash an exact database value while keeping NULL distinct from an empty string."""
    payload = b'null' if value is None else b'string\0' + value.encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report_error(message: str) -> RuntimeError:
    return RuntimeError(f'Verified report refused: {message}')


def load_verified_report(path: Path) -> dict[str, Any]:
    """Load and structurally verify the immutable input to a write operation."""
    if not path.is_file():
        raise _report_error(f'{path} does not exist.')
    try:
        report = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise _report_error(f'cannot read valid JSON: {exc}') from exc

    if report.get('dry_run') is not True:
        raise _report_error('dry_run must be true.')
    if report.get('database_writes_performed') is not False:
        raise _report_error('database_writes_performed must be false.')
    counts = report.get('counts') or {}
    if counts.get('resources_expected') != EXPECTED_RESOURCE_COUNT:
        raise _report_error(f'expected count must be {EXPECTED_RESOURCE_COUNT}.')
    if counts.get('resources_scanned') != EXPECTED_RESOURCE_COUNT:
        raise _report_error(f'scanned count must be {EXPECTED_RESOURCE_COUNT}.')
    if counts.get('resources_successfully_normalized') != EXPECTED_RESOURCE_COUNT:
        raise _report_error(f'success count must be {EXPECTED_RESOURCE_COUNT}.')
    if counts.get('resources_rejected_by_validation') != 0 or report.get('errors'):
        raise _report_error('the dry run contains rejected resources or errors.')

    resources = report.get('resources')
    if not isinstance(resources, list) or len(resources) != EXPECTED_RESOURCE_COUNT:
        raise _report_error(f'resources must contain exactly {EXPECTED_RESOURCE_COUNT} entries.')
    seen: set[int] = set()
    for item in resources:
        if not isinstance(item, dict):
            raise _report_error('each resource entry must be an object.')
        try:
            resource_id = int(item['resource_id'])
        except (KeyError, TypeError, ValueError) as exc:
            raise _report_error('each resource must have an integer resource_id.') from exc
        if resource_id in seen:
            raise _report_error(f'duplicate resource ID #{resource_id}.')
        seen.add(resource_id)
        if item.get('resource_type') != 'blog':
            raise _report_error(f'resource #{resource_id} is not a blog.')
        if item.get('status') != 'safe_to_normalize' or item.get('validation_errors'):
            raise _report_error(f'resource #{resource_id} was not validated as safe.')
        for field in ('original_resource_body', 'normalized_resource_body',
                      'normalized_resource_content_json'):
            if not isinstance(item.get(field), str):
                raise _report_error(f'resource #{resource_id} has invalid {field}.')
        if item.get('original_resource_content_json') is not None and not isinstance(
            item.get('original_resource_content_json'), str
        ):
            raise _report_error(
                f'resource #{resource_id} has invalid original_resource_content_json.'
            )
    return report


def load_imported_ids(path: Path) -> list[int]:
    report = json.loads(path.read_text(encoding='utf-8'))
    imported = report.get('imported')
    if not isinstance(imported, list):
        raise RuntimeError('Import report has no imported Resource list.')
    ids = [int(item['resource_id']) for item in imported]
    if len(ids) != len(set(ids)):
        raise RuntimeError('Import report contains duplicate Resource IDs.')
    if not ids:
        raise RuntimeError('Import report contains no imported Resource IDs.')
    return ids


def _rows_statement(for_update: bool = False) -> Any:
    sql = """
        SELECT resource_id, resource_title, resource_type, resource_body,
               resource_content_json
        FROM resources
        WHERE resource_id IN :resource_ids
        ORDER BY resource_id
    """
    if for_update:
        sql += ' FOR UPDATE'
    return text(sql).bindparams(bindparam('resource_ids', expanding=True))


def load_rows_from_connection(
    connection: Any,
    resource_ids: list[int],
    *,
    for_update: bool = False,
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        _rows_statement(for_update), {'resource_ids': resource_ids}
    ).mappings().all()]


def load_rows(engine: Any, resource_ids: list[int]) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return load_rows_from_connection(connection, resource_ids)


def _resource_values(item: dict[str, Any], normalized: bool) -> tuple[str, str | None]:
    prefix = 'normalized' if normalized else 'original'
    return (
        item[f'{prefix}_resource_body'],
        item[f'{prefix}_resource_content_json'],
    )


def _value_mismatch_errors(
    rows: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    *,
    expect_normalized: bool,
) -> list[dict[str, Any]]:
    row_by_id = {int(row['resource_id']): row for row in rows}
    errors = []
    expected_label = 'normalized' if expect_normalized else 'original'
    for item in resources:
        resource_id = int(item['resource_id'])
        row = row_by_id.get(resource_id)
        if row is None:
            errors.append({'resource_id': resource_id, 'reason': 'Resource is missing.'})
            continue
        if row.get('resource_type') != 'blog':
            errors.append({
                'resource_id': resource_id,
                'reason': f'Resource type changed to {row.get("resource_type")!r}.',
            })
            continue
        expected_body, expected_json = _resource_values(item, expect_normalized)
        changed_fields = []
        if row.get('resource_body') != expected_body:
            changed_fields.append('resource_body')
        if row.get('resource_content_json') != expected_json:
            changed_fields.append('resource_content_json')
        if changed_fields:
            errors.append({
                'resource_id': resource_id,
                'reason': (
                    f'Current {", ".join(changed_fields)} no longer matches the '
                    f'{expected_label} value in the verified report.'
                ),
            })
    return errors


def validate_verified_outputs(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Re-run normalization so a stale or edited report can never be committed."""
    errors = []
    for item in report['resources']:
        resource_id = int(item['resource_id'])
        try:
            result = normalize_resource_html(
                item['original_resource_body'],
                item.get('original_resource_content_json'),
            )
            if result.validation_errors:
                errors.append({
                    'resource_id': resource_id,
                    'reason': '; '.join(result.validation_errors),
                })
                continue
            if result.html != item['normalized_resource_body']:
                errors.append({
                    'resource_id': resource_id,
                    'reason': 'Recomputed normalized resource_body differs from the report.',
                })
            if result.content_json != item['normalized_resource_content_json']:
                errors.append({
                    'resource_id': resource_id,
                    'reason': 'Recomputed normalized resource_content_json differs from the report.',
                })
        except Exception as exc:
            errors.append({
                'resource_id': resource_id,
                'reason': f'{type(exc).__name__}: {exc}',
            })
    return errors


def validate_normalized_rows(
    rows: list[dict[str, Any]],
    resources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    errors = _value_mismatch_errors(rows, resources, expect_normalized=True)
    invalid_ids = {item['resource_id'] for item in errors}
    row_by_id = {int(row['resource_id']): row for row in rows}
    for item in resources:
        resource_id = int(item['resource_id'])
        if resource_id in invalid_ids:
            continue
        row = row_by_id[resource_id]
        try:
            result = normalize_resource_html(
                row['resource_body'], row['resource_content_json']
            )
            if result.validation_errors:
                errors.append({
                    'resource_id': resource_id,
                    'reason': '; '.join(result.validation_errors),
                })
            elif result.html != row['resource_body']:
                errors.append({
                    'resource_id': resource_id,
                    'reason': 'Stored normalized HTML is not idempotent.',
                })
            elif result.content_json != row['resource_content_json']:
                errors.append({
                    'resource_id': resource_id,
                    'reason': 'Stored Tiptap JSON does not match normalized HTML.',
                })
        except Exception as exc:
            errors.append({
                'resource_id': resource_id,
                'reason': f'{type(exc).__name__}: {exc}',
            })
    return errors


def _audit_resources(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in resources:
        original_body, original_json = _resource_values(item, False)
        normalized_body, normalized_json = _resource_values(item, True)
        result.append({
            'resource_id': int(item['resource_id']),
            'original_hashes': {
                'resource_body_sha256': value_hash(original_body),
                'resource_content_json_sha256': value_hash(original_json),
            },
            'normalized_hashes': {
                'resource_body_sha256': value_hash(normalized_body),
                'resource_content_json_sha256': value_hash(normalized_json),
            },
        })
    return result


def write_operation_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def perform_write(
    engine: Any,
    verified_path: Path,
    verified_report: dict[str, Any],
    *,
    rollback: bool,
) -> dict[str, Any]:
    """Apply or restore all 553 rows atomically, then verify from a new connection."""
    resources = sorted(verified_report['resources'], key=lambda item: int(item['resource_id']))
    resource_ids = [int(item['resource_id']) for item in resources]
    operation = 'rollback' if rollback else 'normalize'
    operation_report = {
        'operation': operation,
        'verified_report': str(verified_path.resolve()),
        'verified_report_sha256': file_hash(verified_path),
        'database': database_target(engine),
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'transaction_committed': False,
        'post_commit_verified': False,
        'update_count': 0,
        'resource_ids_updated': [],
        'resources': _audit_resources(resources),
        'errors': [],
    }

    if verified_report.get('database') != database_target(engine):
        operation_report['errors'].append({
            'reason': (
                f'Database target {database_target(engine)!r} does not match verified '
                f'report target {verified_report.get("database")!r}.'
            ),
        })
        return operation_report

    report_errors = validate_verified_outputs(verified_report)
    if report_errors:
        operation_report['errors'].extend(report_errors)
        return operation_report

    update_statement = text("""
        UPDATE resources
        SET resource_body = :resource_body,
            resource_content_json = :resource_content_json
        WHERE resource_id = :resource_id
    """)

    connection = engine.connect()
    transaction = connection.begin()
    try:
        locked_rows = load_rows_from_connection(
            connection, resource_ids, for_update=True
        )
        drift_errors = _value_mismatch_errors(
            locked_rows,
            resources,
            expect_normalized=rollback,
        )
        if drift_errors:
            raise RuntimeError(json.dumps(drift_errors, ensure_ascii=False))

        for item in resources:
            resource_id = int(item['resource_id'])
            target_body, target_json = _resource_values(item, normalized=not rollback)
            result = connection.execute(update_statement, {
                'resource_id': resource_id,
                'resource_body': target_body,
                'resource_content_json': target_json,
            })
            if result.rowcount != 1:
                raise RuntimeError(
                    f'Resource #{resource_id} update affected {result.rowcount} rows.'
                )

        stored_rows = load_rows_from_connection(connection, resource_ids)
        if rollback:
            verification_errors = _value_mismatch_errors(
                stored_rows, resources, expect_normalized=False
            )
        else:
            verification_errors = validate_normalized_rows(stored_rows, resources)
        if verification_errors:
            raise RuntimeError(json.dumps(verification_errors, ensure_ascii=False))

        transaction.commit()
        operation_report['transaction_committed'] = True
        operation_report['update_count'] = len(resource_ids)
        operation_report['resource_ids_updated'] = resource_ids
    except Exception as exc:
        transaction.rollback()
        try:
            details = json.loads(str(exc))
            if isinstance(details, list):
                operation_report['errors'].extend(details)
            else:
                operation_report['errors'].append({'reason': str(exc)})
        except json.JSONDecodeError:
            operation_report['errors'].append({'reason': f'{type(exc).__name__}: {exc}'})
        return operation_report
    finally:
        connection.close()

    post_commit_rows = load_rows(engine, resource_ids)
    if rollback:
        post_errors = _value_mismatch_errors(
            post_commit_rows, resources, expect_normalized=False
        )
    else:
        post_errors = validate_normalized_rows(post_commit_rows, resources)
    if post_errors:
        operation_report['errors'].extend(post_errors)
    else:
        operation_report['post_commit_verified'] = True
    return operation_report


def run_dry_run(args: argparse.Namespace, engine: Any) -> int:
    resource_ids = load_imported_ids(args.import_report)
    rows = load_rows(engine, resource_ids)
    row_by_id = {int(row['resource_id']): row for row in rows}

    counts = {
        'resources_expected': len(resource_ids),
        'resources_scanned': 0,
        'resources_successfully_normalized': 0,
        'resources_rejected_by_validation': 0,
        'resources_skipped': 0,
        'div_paragraph_blocks_converted': 0,
        'empty_spacer_blocks_removed': 0,
        'paragraph_elements_produced': 0,
        'malformed_nesting_repairs': 0,
        'html5_parse_errors_repaired': 0,
        'links_preserved': 0,
        'images_preserved': 0,
        'lists_preserved': 0,
        'real_line_breaks_preserved': 0,
        'formatting_discrepancies': 0,
        'errors': 0,
    }
    resources = []
    trans_gif_images = []
    non_visible_url_anchors = []
    errors = []

    missing_ids = [resource_id for resource_id in resource_ids if resource_id not in row_by_id]
    for resource_id in missing_ids:
        counts['resources_skipped'] += 1
        counts['errors'] += 1
        errors.append({'resource_id': resource_id, 'reason': 'Resource ID is missing from the database.'})

    for resource_id in resource_ids:
        row = row_by_id.get(resource_id)
        if not row:
            continue
        counts['resources_scanned'] += 1
        original_body = row.get('resource_body') or ''
        original_json = row.get('resource_content_json')
        record = {
            'resource_id': resource_id,
            'title': row.get('resource_title') or '',
            'resource_type': row.get('resource_type') or '',
            'original_resource_body': original_body,
            'original_resource_content_json': original_json,
            'normalized_resource_body': None,
            'normalized_resource_content_json': None,
            'status': 'rejected',
            'validation_errors': [],
            'old_content_json_issues': [],
            'metrics': {},
        }
        if row.get('resource_type') != 'blog':
            reason = f'Expected blog Resource, found {row.get("resource_type")!r}.'
            record['validation_errors'] = [reason]
            counts['resources_rejected_by_validation'] += 1
            counts['errors'] += 1
            errors.append({'resource_id': resource_id, 'reason': reason})
            resources.append(record)
            continue

        try:
            result = normalize_resource_html(original_body, original_json)
            record['normalized_resource_body'] = result.html
            record['normalized_resource_content_json'] = result.content_json
            record['validation_errors'] = result.validation_errors
            record['old_content_json_issues'] = result.old_json_issues
            record['metrics'] = {
                'div_paragraph_blocks_converted': result.metrics.div_paragraph_blocks_converted,
                'empty_spacer_blocks_removed': result.metrics.empty_spacer_blocks_removed,
                'paragraph_elements_produced': result.metrics.paragraphs_produced,
                'malformed_nesting_repairs': result.metrics.malformed_nesting_repairs,
                'html5_parse_errors_repaired': result.metrics.html5_parse_errors,
                'links_preserved': len(result.normalized_signature.all_anchors),
                'images_preserved': len(result.normalized_signature.images),
                'lists_preserved': len(result.normalized_signature.lists),
                'real_line_breaks_preserved': result.metrics.real_line_breaks_preserved,
            }
            for key in (
                'div_paragraph_blocks_converted', 'empty_spacer_blocks_removed',
                'paragraph_elements_produced', 'malformed_nesting_repairs',
                'html5_parse_errors_repaired', 'links_preserved', 'images_preserved',
                'lists_preserved', 'real_line_breaks_preserved',
            ):
                counts[key] += record['metrics'][key]

            for src, alt, title in result.normalized_signature.images:
                if src.lower().endswith('/trans.gif') and title == 'More...':
                    trans_gif_images.append({
                        'resource_id': resource_id,
                        'title': record['title'],
                        'src': src,
                        'alt': alt,
                        'image_title': title,
                    })
            for anchor in result.normalized_signature.empty_url_anchors:
                non_visible_url_anchors.append({
                    'resource_id': resource_id,
                    'title': record['title'],
                    **anchor,
                })

            if result.validation_errors:
                counts['resources_rejected_by_validation'] += 1
                counts['formatting_discrepancies'] += sum(
                    'formatting differs' in item for item in result.validation_errors
                )
                errors.append({
                    'resource_id': resource_id,
                    'reason': '; '.join(result.validation_errors),
                })
            else:
                record['status'] = 'safe_to_normalize'
                counts['resources_successfully_normalized'] += 1
        except Exception as exc:  # keep auditing the remaining one-time records
            reason = f'{type(exc).__name__}: {exc}'
            record['validation_errors'] = [reason]
            counts['resources_rejected_by_validation'] += 1
            counts['errors'] += 1
            errors.append({'resource_id': resource_id, 'reason': reason})
        resources.append(record)

    samples = []
    resources_by_id = {item['resource_id']: item for item in resources}
    for category, resource_id in SAMPLE_IDS.items():
        item = resources_by_id.get(resource_id)
        if not item:
            continue
        samples.append({
            'category': category,
            'resource_id': resource_id,
            'title': item['title'],
            'before': item['original_resource_body'],
            'after': item['normalized_resource_body'],
            'status': item['status'],
            'validation_errors': item['validation_errors'],
        })

    report = {
        'dry_run': True,
        'database_writes_performed': False,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'database': database_target(engine),
        'import_report': str(args.import_report.resolve()),
        'counts': counts,
        'legacy_items_preserved_for_review': {
            'trans_gif_images': trans_gif_images,
            'non_visible_url_anchors': non_visible_url_anchors,
        },
        'errors': errors,
        'samples': samples,
        'resources': resources,
    }
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    print('Blogger HTML normalization dry run')
    print(f'Database: {report["database"]}')
    print('Database writes performed: no')
    for key, value in counts.items():
        print(f'{key.replace("_", " ").title()}: {value}')
    print(f'Trans.gif images retained: {len(trans_gif_images)}')
    print(f'Non-visible URL anchors retained: {len(non_visible_url_anchors)}')
    print(f'Report: {args.report_json.resolve()}')
    if errors:
        print('Rejected records:')
        for item in errors:
            print(f'  #{item["resource_id"]}: {item["reason"]}')
        return 1
    return 0


def main() -> int:
    args = parse_args()
    if args.rollback and not args.commit:
        raise RuntimeError('--rollback performs database writes and requires --commit.')
    if args.commit and args.verified_report is None:
        raise RuntimeError('--commit requires --verified-report.')
    if not args.commit and args.verified_report is not None:
        raise RuntimeError('--verified-report is only used with --commit.')
    if not args.commit and args.commit_report is not None:
        raise RuntimeError('--commit-report is only used with --commit.')

    load_dotenv(PROJECT_ROOT / '.env')
    database_uri = os.getenv('SQLALCHEMY_DATABASE_URI')
    if not database_uri:
        raise RuntimeError('SQLALCHEMY_DATABASE_URI is not configured.')
    engine = create_engine(database_uri)

    if not args.commit:
        return run_dry_run(args, engine)

    verified_path = args.verified_report.resolve()
    verified_report = load_verified_report(verified_path)
    operation_report_path = args.commit_report
    if operation_report_path is None:
        filename = (
            'blogger-normalization-rollback.json'
            if args.rollback
            else 'blogger-normalization-commit.json'
        )
        operation_report_path = PROJECT_ROOT / 'logs' / filename

    result = perform_write(
        engine,
        verified_path,
        verified_report,
        rollback=args.rollback,
    )
    write_operation_report(operation_report_path, result)

    print(f'Blogger normalization {result["operation"]}')
    print(f'Database: {result["database"]}')
    print(f'Transaction committed: {"yes" if result["transaction_committed"] else "no"}')
    print(f'Post-commit verified: {"yes" if result["post_commit_verified"] else "no"}')
    print(f'Update count: {result["update_count"]}')
    print(f'Errors: {len(result["errors"])}')
    print(f'Report: {operation_report_path.resolve()}')
    for item in result['errors']:
        resource_id = item.get('resource_id')
        prefix = f'#{resource_id}: ' if resource_id is not None else ''
        print(f'  {prefix}{item["reason"]}')
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
