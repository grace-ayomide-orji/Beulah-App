#!/usr/bin/env python
"""One-command, dry-run-first Blogger production migration orchestration."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any
import unicodedata

from dotenv import load_dotenv
from sqlalchemy import bindparam, create_engine, text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from beulah_pkg.blogger_import import (  # noqa: E402
    DatabaseSnapshot,
    FeedData,
    PreparedPost,
    blogger_slug,
    build_plan,
    database_target,
    insert_post,
    load_database_snapshot,
    load_database_snapshot_from_connection,
    parse_feed,
    resource_fingerprint,
    resource_row_fingerprint,
    rollback_report,
)
from beulah_pkg.blogger_normalization import (  # noqa: E402
    normalize_resource_html,
    normalize_verified_prose_boundaries,
    semantic_signature,
)


LIVE_ONLY = {'LIVE'}
OTHER_DASHES = {'\u00ad', '\u2043', '\u2212'}

# Stable Blogger identities derived from feed.atom. Titles, requested slugs, and
# publication timestamps are guardrails; production Resource IDs are never used.
HARD_BREAK_RULES: dict[str, dict[str, Any]] = {
    'tag:blogger.com,1999:blog-8000191563955632975.post-2112388292422786795': {
        'title': 'Stay Focused!', 'requested_slug': 'stay-focused',
        'published_at': '2011-08-16T10:31:00', 'boundaries': 1,
    },
    'tag:blogger.com,1999:blog-8000191563955632975.post-1152010417052458794': {
        'title': 'Be Encouraged! 2', 'requested_slug': 'be-encouraged-2',
        'published_at': '2011-12-08T11:01:00', 'boundaries': 1,
    },
    'tag:blogger.com,1999:blog-8000191563955632975.post-2672845964288575023': {
        'title': 'Resetting The Settings Of The Old Mind',
        'requested_slug': 'resetting-settings-of-old-mind',
        'published_at': '2012-08-02T12:00:00', 'boundaries': 3,
    },
    'tag:blogger.com,1999:blog-8000191563955632975.post-5709511746009465519': {
        'title': 'Old Time Religion', 'requested_slug': 'old-time-religion',
        'published_at': '2013-09-19T11:15:00.001000', 'boundaries': 1,
    },
    'tag:blogger.com,1999:blog-8000191563955632975.post-4292780207132593048': {
        'title': 'The Prize', 'requested_slug': 'the-prize',
        'published_at': '2014-03-19T08:33:00.001000', 'boundaries': 1,
    },
    'tag:blogger.com,1999:blog-8000191563955632975.post-1416041163374360664': {
        'title': 'What’s in it for Me?', 'requested_slug': 'whats-in-it-for-me',
        'published_at': '2014-11-03T09:28:00', 'boundaries': 1,
    },
    'tag:blogger.com,1999:blog-8000191563955632975.post-5230310224136540876': {
        'title': "St Valentines's Day Celebration: Begging The Question!",
        'requested_slug': 'st-valentiness-day-celebration-begging',
        'published_at': '2015-02-14T08:30:00', 'boundaries': 4,
    },
    'tag:blogger.com,1999:blog-8000191563955632975.post-8783760577097832585': {
        'title': 'Finding True Love', 'requested_slug': 'finding-true-love',
        'published_at': '2016-02-12T12:11:00.001000', 'boundaries': 4,
    },
    'tag:blogger.com,1999:blog-8000191563955632975.post-3311743061672002336': {
        'title': "An Answer To Almighty God's Question",
        'requested_slug': 'an-answer-to-almighty-gods-question',
        'published_at': '2025-08-19T14:46:00', 'boundaries': 1,
    },
    'tag:blogger.com,1999:blog-8000191563955632975.post-2623063897555532414': {
        'title': 'Judge, Which Judge? King, Which King?',
        'requested_slug': 'judge-which-judge-king-which-king',
        'published_at': '2025-10-09T09:47:00', 'boundaries': 2,
    },
    'tag:blogger.com,1999:blog-8000191563955632975.post-8644486098879031906': {
        'title': 'You Are What Matters Most To Jesus Christ',
        'requested_slug': 'you-are-what-matters-most-to-jesus',
        'published_at': '2026-01-06T00:53:00.001000', 'boundaries': 9,
    },
    'tag:blogger.com,1999:blog-8000191563955632975.post-8015777323214738189': {
        'title': 'Finding True Love', 'requested_slug': 'finding-true-love',
        'published_at': '2026-02-09T14:13:00.002000', 'boundaries': 4,
    },
    'tag:blogger.com,1999:blog-8000191563955632975.post-3747323710414916965': {
        'title': 'Living In The Presence of God',
        'requested_slug': 'living-in-presence-of-god',
        'published_at': '2026-10-05T15:21:42.940000', 'boundaries': 4,
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Production-safe, dry-run-first Blogger migration orchestration.',
    )
    parser.add_argument('--feed', type=Path, help='Absolute path to Blogger feed.atom.')
    parser.add_argument('--blog-base-url', help='Optional original Blogger base URL override.')
    parser.add_argument('--dry-run', action='store_true', help='Plan and validate without writes.')
    parser.add_argument('--commit', action='store_true', help='Authorize import or rollback writes.')
    parser.add_argument('--rollback', action='store_true', help='Roll back a completed migration.')
    parser.add_argument(
        '--verified-report',
        type=Path,
        help='Reviewed dry-run report for commit, or completed commit report for rollback.',
    )
    parser.add_argument('--report-json', type=Path, required=True, help='Combined audit report path.')
    return parser.parse_args()


def save_report(path: Path, report: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp')
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    temporary.replace(path)


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def value_hash(value: str | None) -> str:
    payload = b'null' if value is None else b'string\0' + value.encode('utf-8')
    return sha256(payload).hexdigest()


def make_engine() -> Any:
    load_dotenv(PROJECT_ROOT / '.env', override=False)
    database_uri = os.getenv('SQLALCHEMY_DATABASE_URI')
    if not database_uri:
        raise RuntimeError('SQLALCHEMY_DATABASE_URI is not configured.')
    return create_engine(database_uri, pool_pre_ping=True)


def is_dash(character: str) -> bool:
    return (
        character == '-'
        or unicodedata.category(character) == 'Pd'
        or character in OTHER_DASHES
    )


def dash_sequence(value: str) -> list[str]:
    return [character for character in value if is_dash(character)]


def dash_counts(value: str) -> dict[str, int]:
    counts = Counter(dash_sequence(value))
    return {f'U+{ord(character):04X}': count for character, count in sorted(counts.items())}


def validate_hard_break_rules(feed: FeedData) -> list[dict[str, Any]]:
    by_id = {entry.entry_id: entry for entry in feed.entries}
    errors = []
    for entry_id, rule in HARD_BREAK_RULES.items():
        entry = by_id.get(entry_id)
        if entry is None:
            errors.append({'blogger_entry_id': entry_id, 'error': 'Entry is missing from feed.'})
            continue
        actual = {
            'title': entry.title,
            'requested_slug': blogger_slug(entry.filename, entry.title),
            'published_at': entry.published_at.isoformat() if entry.published_at else None,
            'entry_type': entry.entry_type,
            'status': entry.status,
        }
        expected = {
            'title': rule['title'],
            'requested_slug': rule['requested_slug'],
            'published_at': rule['published_at'],
            'entry_type': 'POST',
            'status': 'LIVE',
        }
        if actual != expected:
            errors.append({
                'blogger_entry_id': entry_id,
                'title': entry.title,
                'error': 'Stable hard-break identity guard failed.',
                'expected': expected,
                'actual': actual,
            })
    return errors


def skipped_entries(feed: FeedData, duplicates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    skipped = []
    for entry in feed.entries:
        reason = None
        if entry.entry_type == 'COMMENT':
            reason = 'blogger_comment'
        elif entry.entry_type == 'PAGE':
            reason = 'blogger_page'
        elif entry.entry_type != 'POST':
            reason = f'unsupported_entry_type:{entry.entry_type or "<missing>"}'
        elif entry.status in {'DRAFT', 'SCHEDULED'}:
            reason = 'draft_or_scheduled_post'
        elif entry.status == 'SOFT_TRASHED':
            reason = 'soft_trashed_post'
        elif entry.status != 'LIVE':
            reason = f'unsupported_post_status:{entry.status or "<missing>"}'
        if reason:
            skipped.append({
                'blogger_entry_id': entry.entry_id,
                'title': entry.title,
                'entry_type': entry.entry_type,
                'status': entry.status,
                'reason': reason,
            })
    for duplicate in duplicates:
        skipped.append({
            'blogger_entry_id': duplicate.get('blogger_entry_id'),
            'title': duplicate.get('title'),
            'entry_type': 'POST',
            'status': 'LIVE',
            'reason': 'existing_blog_duplicate',
            'existing_resource_id': duplicate.get('resource_id'),
            'duplicate_match': duplicate.get('reason'),
        })
    return skipped


def normalized_plan_item(
    post: PreparedPost,
    *,
    hard_break: dict[str, Any] | None,
    original_dash_counts: dict[str, int],
) -> dict[str, Any]:
    source = post.source
    return {
        'blogger_entry_id': source.entry_id,
        'blogger_original_url': source.original_url,
        'title': post.title,
        'requested_slug': post.requested_slug,
        'assigned_slug': post.slug,
        'resource_type': 'blog',
        'status': post.status,
        'published_at': source.published_at.isoformat() if source.published_at else None,
        'created_at': (source.created_at or source.published_at).isoformat()
        if source.created_at or source.published_at else None,
        'updated_at': (source.updated_at or source.published_at or source.created_at).isoformat()
        if source.updated_at or source.published_at or source.created_at else None,
        'category': post.category,
        'resource_body_sha256': value_hash(post.body_html),
        'resource_content_json_sha256': value_hash(post.content_json),
        'resource_fingerprint': resource_fingerprint(post),
        'dash_counts': original_dash_counts,
        'hard_break_correction': hard_break,
    }


def prepare_final_plan(
    feed: FeedData,
    snapshot: DatabaseSnapshot,
) -> tuple[list[PreparedPost], dict[str, Any]]:
    posts, import_report = build_plan(
        feed,
        snapshot,
        allowed_statuses=LIVE_ONLY,
    )
    errors = list(import_report.get('errors') or [])
    errors.extend(validate_hard_break_rules(feed))
    normalized_posts: list[PreparedPost] = []
    planned_resources = []
    hard_break_corrections = []

    for post in posts:
        source = post.source
        try:
            source_signature = semantic_signature(source.content_html)
            before_signature = semantic_signature(post.body_html)
            source_dash_sequence = dash_sequence(source_signature.visible_text)
            before_dash_sequence = dash_sequence(before_signature.visible_text)
            if source_dash_sequence != before_dash_sequence:
                raise ValueError('Importer preparation changed original Blogger dash characters.')
            full = normalize_resource_html(post.body_html, post.content_json)
            if full.validation_errors:
                raise ValueError('; '.join(full.validation_errors))

            final_html = full.html
            final_json = full.content_json
            correction = None
            rule = HARD_BREAK_RULES.get(source.entry_id)
            if rule:
                result = normalize_verified_prose_boundaries(
                    final_html,
                    final_json,
                    expected_boundaries=int(rule['boundaries']),
                )
                if result.validation_errors:
                    raise ValueError('; '.join(result.validation_errors))
                final_html = result.html
                final_json = result.content_json
                correction = {
                    'blogger_entry_id': source.entry_id,
                    'title': post.title,
                    'requested_slug': post.requested_slug,
                    'assigned_slug': post.slug,
                    'boundaries_split': result.boundaries_split,
                    'break_elements_removed': result.break_elements_removed,
                    'paragraphs_before': result.paragraphs_before,
                    'paragraphs_after': result.paragraphs_after,
                    'boundary_details': result.boundary_details,
                }

            after_signature = semantic_signature(final_html)
            if before_signature.visible_text != after_signature.visible_text:
                raise ValueError('Visible text changed during finalized normalization.')
            if before_dash_sequence != dash_sequence(after_signature.visible_text):
                raise ValueError('Dash characters changed during finalized normalization.')

            post.body_html = final_html
            post.content_json = final_json
            normalized_posts.append(post)
            if correction:
                hard_break_corrections.append(correction)
            planned_resources.append(normalized_plan_item(
                post,
                hard_break=correction,
                original_dash_counts=dash_counts(source_signature.visible_text),
            ))
        except Exception as exc:
            errors.append({
                'blogger_entry_id': source.entry_id,
                'title': post.title,
                'error': f'{type(exc).__name__}: {exc}',
            })

    duplicates = list(import_report.get('duplicates') or [])
    skipped = skipped_entries(feed, duplicates)
    plan_payload = {
        'feed_sha256': feed.sha256,
        'database_target': snapshot.target,
        'planned_resources': planned_resources,
        'duplicates': duplicates,
        'skipped_entries': skipped,
    }
    plan_sha256 = sha256(
        json.dumps(plan_payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    ).hexdigest()
    report = {
        'mode': 'dry-run',
        'dry_run': True,
        'database_writes_performed': False,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'feed': {
            'id': feed.feed_id,
            'title': feed.title,
            'sha256': feed.sha256,
        },
        'database': {
            'target': snapshot.target,
            'resources_by_type': snapshot.resource_counts_by_type,
            'existing_blog_posts': len(snapshot.resources),
        },
        'plan_sha256': plan_sha256,
        'counts': {
            **import_report.get('counts', {}),
            'existing_blog_posts': len(snapshot.resources),
            'would_import': len(posts),
            'imported': 0,
            'normalized': len(normalized_posts),
            'hard_break_corrected_posts': len(hard_break_corrections),
            'hard_break_boundaries_split': sum(
                item['boundaries_split'] for item in hard_break_corrections
            ),
            'skipped_entries': len(skipped),
            'errors': len(errors),
            'warnings': len(import_report.get('warnings') or []),
        },
        'existing_duplicates': duplicates,
        'skipped_entries': skipped,
        'warnings': import_report.get('warnings') or [],
        'errors': errors,
        'planned_resources': planned_resources,
        'hard_break_corrections': hard_break_corrections,
        'created_production_resource_ids': [],
        'imported': [],
        'rollback': {
            'available_after_commit': bool(normalized_posts),
            'strategy': 'Delete only created Resource IDs after fingerprint/comment guards pass.',
        },
    }
    return normalized_posts, report


def verify_dry_run_report(path: Path, current: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f'Verified dry-run report does not exist: {path}')
    verified = json.loads(path.read_text(encoding='utf-8'))
    if verified.get('mode') != 'dry-run' or verified.get('dry_run') is not True:
        raise RuntimeError('Verified report is not a production migration dry run.')
    if verified.get('database_writes_performed') is not False:
        raise RuntimeError('Verified dry-run report claims database writes.')
    if verified.get('errors'):
        raise RuntimeError('Verified dry-run report contains errors.')
    checks = {
        'feed SHA-256': (
            verified.get('feed', {}).get('sha256'), current.get('feed', {}).get('sha256')
        ),
        'database target': (
            verified.get('database', {}).get('target'), current.get('database', {}).get('target')
        ),
        'plan SHA-256': (verified.get('plan_sha256'), current.get('plan_sha256')),
    }
    mismatches = [name for name, values in checks.items() if values[0] != values[1]]
    if mismatches:
        raise RuntimeError(
            'Commit refused because the verified dry run no longer matches: '
            + ', '.join(mismatches)
        )
    return verified


def resource_rows(connection: Any, ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    statement = text("""
        SELECT resource_id, resource_title, resource_body, resource_type, resource_slug,
               resource_status, resource_published_date, resource_category,
               resource_content_json, resource_is_deleted
        FROM resources
        WHERE resource_id IN :resource_ids
        ORDER BY resource_id
    """).bindparams(bindparam('resource_ids', expanding=True))
    return [dict(row) for row in connection.execute(
        statement,
        {'resource_ids': ids},
    ).mappings().all()]


def validate_inserted_rows(
    rows: list[dict[str, Any]],
    imported: list[dict[str, Any]],
    posts: list[PreparedPost],
) -> list[dict[str, Any]]:
    errors = []
    row_by_id = {int(row['resource_id']): row for row in rows}
    post_by_entry = {post.source.entry_id: post for post in posts}
    for item in imported:
        resource_id = int(item['resource_id'])
        row = row_by_id.get(resource_id)
        post = post_by_entry[item['blogger_entry_id']]
        if row is None:
            errors.append({'resource_id': resource_id, 'error': 'Inserted Resource is missing.'})
            continue
        if row.get('resource_type') != 'blog':
            errors.append({'resource_id': resource_id, 'error': 'Inserted Resource is not a blog.'})
            continue
        if resource_row_fingerprint(row) != resource_fingerprint(post):
            errors.append({'resource_id': resource_id, 'error': 'Stored Resource fingerprint differs.'})
            continue
        normalized = normalize_resource_html(row['resource_body'], row['resource_content_json'])
        if normalized.validation_errors:
            errors.append({
                'resource_id': resource_id,
                'error': '; '.join(normalized.validation_errors),
            })
        elif normalized.html != row['resource_body']:
            errors.append({'resource_id': resource_id, 'error': 'Stored HTML is not canonical.'})
        elif normalized.content_json != row['resource_content_json']:
            errors.append({'resource_id': resource_id, 'error': 'Stored Tiptap JSON is not canonical.'})
        if post.source.entry_id in HARD_BREAK_RULES:
            check = normalize_verified_prose_boundaries(
                row['resource_body'],
                row['resource_content_json'],
                expected_boundaries=0,
            )
            if check.validation_errors or check.html != row['resource_body']:
                errors.append({
                    'resource_id': resource_id,
                    'error': 'Verified hard-break correction is not complete/idempotent.',
                })
    extra = sorted(set(row_by_id) - {int(item['resource_id']) for item in imported})
    if extra:
        errors.append({'error': f'Unexpected inserted Resource rows returned: {extra}.'})
    return errors


def commit_migration(
    engine: Any,
    feed: FeedData,
    args: argparse.Namespace,
) -> dict[str, Any]:
    connection = engine.connect()
    transaction = connection.begin()
    report: dict[str, Any] | None = None
    imported: list[dict[str, Any]] = []
    posts: list[PreparedPost] = []
    try:
        snapshot = load_database_snapshot_from_connection(connection, for_update=True)
        posts, report = prepare_final_plan(feed, snapshot)
        verify_dry_run_report(args.verified_report.resolve(), report)
        if report['errors']:
            raise RuntimeError('Current production plan contains validation errors.')

        for post in posts:
            imported.append(insert_post(connection, post))
        ids = [int(item['resource_id']) for item in imported]
        validation_errors = validate_inserted_rows(
            resource_rows(connection, ids),
            imported,
            posts,
        )
        if validation_errors:
            raise RuntimeError(json.dumps(validation_errors, ensure_ascii=False))

        report.update({
            'mode': 'commit',
            'dry_run': False,
            # The rows exist only inside the open transaction at this point.
            # Saving their guarded identities first makes an immediately-after-
            # commit process failure recoverable without claiming they committed.
            'database_writes_performed': False,
            'verified_dry_run_report': str(args.verified_report.resolve()),
            'verified_dry_run_report_sha256': file_hash(args.verified_report.resolve()),
            'transaction_state': 'pending_commit',
            'transaction_committed': False,
            'post_commit_verified': False,
            'imported': imported,
            'created_production_resource_ids': ids,
            'rollback': {
                'available': bool(ids),
                'created_resource_ids': ids,
                'guard': 'resource fingerprint unchanged and no comments',
                'source_report_required': str(args.report_json.resolve()),
            },
        })
        report['counts']['imported'] = len(imported)
        report['counts']['write_errors'] = 0
        save_report(args.report_json, report)
        transaction.commit()
        report['database_writes_performed'] = bool(imported)
        report['transaction_state'] = 'committed'
        report['transaction_committed'] = True
        # Minimize the recovery window between commit and the durable report
        # reflecting that commit. Post-commit content verification follows.
        save_report(args.report_json, report)
    except Exception as exc:
        transaction.rollback()
        if report is None:
            report = {
                'mode': 'commit',
                'dry_run': False,
                'database_writes_performed': False,
                'database': {'target': database_target(engine)},
                'counts': {'imported': 0, 'errors': 1},
                'errors': [],
                'imported': [],
                'created_production_resource_ids': [],
            }
        try:
            details = json.loads(str(exc))
            if isinstance(details, list):
                report.setdefault('errors', []).extend(details)
            else:
                report.setdefault('errors', []).append({'error': str(exc)})
        except json.JSONDecodeError:
            report.setdefault('errors', []).append({'error': f'{type(exc).__name__}: {exc}'})
        report['database_writes_performed'] = False
        report['transaction_state'] = 'rolled_back'
        report['transaction_committed'] = False
        report['imported'] = []
        report['created_production_resource_ids'] = []
        report['rollback'] = {
            'available': False,
            'reason': 'The import transaction was rolled back.',
        }
        report['counts']['imported'] = 0
        report['counts']['errors'] = len(report['errors'])
        save_report(args.report_json, report)
        return report
    finally:
        connection.close()

    with engine.connect() as verification_connection:
        post_errors = validate_inserted_rows(
            resource_rows(
                verification_connection,
                [int(item['resource_id']) for item in imported],
            ),
            imported,
            posts,
        )
    if post_errors:
        report['errors'].extend(post_errors)
        try:
            compensating = rollback_report(engine, report)
            report['automatic_rollback'] = {
                'performed': True,
                'resource_ids': compensating['resource_ids'],
                'reason': 'Post-commit verification failed.',
            }
            report['database_writes_performed'] = False
            report['transaction_state'] = 'committed_then_rolled_back'
            report['rollback']['available'] = False
            report['rollback']['reason'] = 'Automatic rollback already removed the created rows.'
        except Exception as rollback_exc:
            report['automatic_rollback'] = {
                'performed': False,
                'reason': f'{type(rollback_exc).__name__}: {rollback_exc}',
            }
            report['errors'].append({
                'error': 'Automatic rollback after post-commit verification failure also failed.',
                'detail': f'{type(rollback_exc).__name__}: {rollback_exc}',
            })
    else:
        report['post_commit_verified'] = True
    report['counts']['errors'] = len(report['errors'])
    save_report(args.report_json, report)
    return report


def rollback_migration(engine: Any, args: argparse.Namespace) -> dict[str, Any]:
    source_path = args.verified_report.resolve()
    source = json.loads(source_path.read_text(encoding='utf-8'))
    recoverable_pending_report = (
        source.get('mode') == 'commit'
        and source.get('transaction_state') == 'pending_commit'
        and bool(source.get('imported'))
    )
    if source.get('mode') != 'commit' or not (
        source.get('transaction_committed') is True or recoverable_pending_report
    ):
        raise RuntimeError(
            'Rollback requires a completed production commit report (or a guarded '
            'pending-commit recovery report).'
        )
    result = rollback_report(engine, source)
    ids = [int(value) for value in result['resource_ids']]
    with engine.connect() as connection:
        remaining = resource_rows(connection, ids)
    report = {
        'mode': 'rollback',
        'dry_run': False,
        'database_writes_performed': True,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'database': {'target': database_target(engine)},
        'source_commit_report': str(source_path),
        'source_commit_report_sha256': file_hash(source_path),
        'rolled_back_resource_ids': ids,
        'counts': {
            'rolled_back': result['rolled_back'],
            'remaining_created_resources': len(remaining),
            'errors': 0 if not remaining else 1,
        },
        'errors': [] if not remaining else [{
            'error': 'One or more created Resources still exist after rollback.',
            'resource_ids': [row['resource_id'] for row in remaining],
        }],
    }
    save_report(args.report_json, report)
    return report


def print_summary(report: dict[str, Any], report_path: Path) -> None:
    print(f'Blogger production migration: {report.get("mode", "unknown")}')
    print(f'Database: {report.get("database", {}).get("target", "<unknown>")}')
    print(f'Database writes performed: {"yes" if report.get("database_writes_performed") else "no"}')
    for key in (
        'existing_blog_posts', 'duplicates', 'would_import', 'imported', 'normalized',
        'hard_break_corrected_posts', 'hard_break_boundaries_split', 'skipped_entries',
        'warnings', 'errors', 'rolled_back',
    ):
        if key in report.get('counts', {}):
            print(f'{key.replace("_", " ").title()}: {report["counts"][key]}')
    print(f'Report: {report_path.resolve()}')


def validate_args(args: argparse.Namespace) -> None:
    if args.dry_run == args.commit:
        raise RuntimeError('Choose exactly one of --dry-run or --commit.')
    if args.rollback and not args.commit:
        raise RuntimeError('--rollback performs writes and requires --commit.')
    if args.rollback:
        if args.feed:
            raise RuntimeError('--feed cannot be used with --rollback.')
        if not args.verified_report:
            raise RuntimeError('--rollback requires --verified-report with the commit report.')
    else:
        if not args.feed or not args.feed.is_file():
            raise RuntimeError('--feed must name an existing feed.atom file.')
        if args.commit and not args.verified_report:
            raise RuntimeError('--commit requires the reviewed --verified-report dry run.')
        if args.dry_run and args.verified_report:
            raise RuntimeError('--verified-report is only valid with --commit.')
    if args.verified_report and args.verified_report.resolve() == args.report_json.resolve():
        raise RuntimeError('--report-json must not overwrite --verified-report.')


def main() -> int:
    args = parse_args()
    validate_args(args)
    engine = make_engine()

    if args.rollback:
        report = rollback_migration(engine, args)
    else:
        feed = parse_feed(args.feed.resolve(), args.blog_base_url)
        if args.dry_run:
            snapshot = load_database_snapshot(engine)
            _, report = prepare_final_plan(feed, snapshot)
            save_report(args.report_json, report)
        else:
            report = commit_migration(engine, feed, args)

    print_summary(report, args.report_json)
    return 1 if report.get('errors') else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(2)
