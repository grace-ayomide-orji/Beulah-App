#!/usr/bin/env python
"""Analyze, import, or roll back a Blogger Takeout feed.

Dry-run is the default. A commit requires the JSON report from a reviewed dry-run.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import dotenv_values
from sqlalchemy import create_engine

from beulah_pkg.blogger_import import (
    build_plan,
    database_target,
    import_posts,
    load_database_snapshot,
    parse_feed,
    rollback_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Dry-run-first Blogger Takeout importer for the Beulah blog.',
    )
    parser.add_argument('--feed', type=Path, help='Path to the Blogger feed.atom file.')
    parser.add_argument(
        '--blog-base-url',
        help='Override the old blog base URL used with Blogger filenames.',
    )
    parser.add_argument(
        '--database-url',
        help='Database URL override. Defaults to SQLALCHEMY_DATABASE_URI from .env.',
    )
    parser.add_argument(
        '--offline',
        action='store_true',
        help='Analyze only the feed; duplicate checks are not possible.',
    )
    parser.add_argument('--commit', action='store_true', help='Write the planned import or rollback.')
    parser.add_argument(
        '--verified-report',
        type=Path,
        help='Reviewed dry-run JSON report required by --commit imports.',
    )
    parser.add_argument(
        '--rollback-report',
        type=Path,
        help='Rollback Resource IDs recorded by a completed local import report.',
    )
    parser.add_argument('--report-json', type=Path, help='Write the complete report as JSON.')
    return parser.parse_args()


def database_url(args: argparse.Namespace) -> str:
    if args.database_url:
        return args.database_url
    configured = os.environ.get('SQLALCHEMY_DATABASE_URI')
    if configured:
        return configured
    return dotenv_values(PROJECT_ROOT / '.env').get('SQLALCHEMY_DATABASE_URI') or ''


def make_engine(args: argparse.Namespace):
    url = database_url(args)
    if not url:
        raise RuntimeError('SQLALCHEMY_DATABASE_URI is not configured.')
    return create_engine(url, pool_pre_ping=True)


def save_report(path: Path, report: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp')
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def write_report(path: Path | None, report: dict[str, Any]) -> None:
    if not path:
        return
    save_report(path, report)
    print(f'Full report: {path.resolve()}')


def print_summary(report: dict[str, Any]) -> None:
    counts = report.get('counts', {})
    mode = 'DRY RUN' if report.get('dry_run') else 'IMPORT'
    print(f'\nBlogger migration {mode}')
    print(f'Feed: {report.get("feed", {}).get("title") or "<unknown>"}')
    print(f'Database checked: {report.get("database", {}).get("checked")}')
    assigned_types = sorted({
        item.get('resource_type')
        for item in report.get('planned_resources', [])
        if item.get('resource_type')
    })
    if assigned_types:
        print(f'Resource type assigned: {", ".join(assigned_types)}')
    for label in (
        'total_entries', 'posts', 'pages', 'comments', 'drafts', 'published_posts',
        'soft_trashed_posts', 'skipped_items', 'duplicates', 'categories_labels',
        'label_assignments', 'images', 'errors', 'warnings', 'would_import', 'imported',
        'write_errors',
    ):
        if label in counts:
            print(f'{label.replace("_", " ").title()}: {counts[label]}')
    if report.get('database_error'):
        print(f'Database error: {report["database_error"]}')
    if report.get('errors'):
        print('\nContent errors:')
        for item in report['errors']:
            print(f'- {item.get("title") or item.get("blogger_entry_id")}: {item.get("error")}')
    if report.get('warnings'):
        print('\nWarnings:')
        for item in report['warnings']:
            print(f'- {item.get("title")}: {item.get("message")}')
    if report.get('duplicates'):
        print('\nDuplicates:')
        for item in report['duplicates']:
            print(
                f'- {item.get("title")} -> resource #{item.get("resource_id")} '
                f'({item.get("reason")})'
            )


def verify_reviewed_report(
    path: Path | None,
    feed_sha256: str,
    target: str,
    plan_sha256: str,
) -> None:
    if not path:
        raise RuntimeError('--commit requires --verified-report from the reviewed dry-run.')
    previous = json.loads(path.read_text(encoding='utf-8'))
    if previous.get('dry_run') is not True:
        raise RuntimeError('Verified report is not a dry-run report.')
    if previous.get('feed', {}).get('sha256') != feed_sha256:
        raise RuntimeError('Feed has changed since the verified dry-run.')
    if previous.get('database', {}).get('checked') is not True:
        raise RuntimeError('Verified report did not complete a database duplicate check.')
    if previous.get('database', {}).get('target') != target:
        raise RuntimeError('Database target differs from the verified dry-run.')
    if previous.get('plan_sha256') != plan_sha256:
        raise RuntimeError('Import plan differs from the verified dry-run.')


def main() -> int:
    args = parse_args()
    if args.rollback_report:
        if args.feed:
            raise RuntimeError('--feed cannot be combined with --rollback-report.')
        if not args.commit:
            raise RuntimeError('Rollback is destructive and requires --commit.')
        engine = make_engine(args)
        source_report = json.loads(args.rollback_report.read_text(encoding='utf-8'))
        result = rollback_report(engine, source_report)
        write_report(args.report_json, result)
        print(json.dumps(result, indent=2))
        return 0

    if not args.feed:
        raise RuntimeError('--feed is required unless --rollback-report is used.')
    if not args.feed.is_file():
        raise RuntimeError(f'Feed file does not exist: {args.feed}')
    if args.offline and args.commit:
        raise RuntimeError('--offline cannot be combined with --commit.')

    feed = parse_feed(args.feed.resolve(), args.blog_base_url)
    snapshot = None
    engine = None
    database_error = None
    if not args.offline:
        try:
            engine = make_engine(args)
            snapshot = load_database_snapshot(engine)
        except Exception as exc:
            database_error = str(exc)

    posts, report = build_plan(feed, snapshot)
    if database_error:
        report['database_error'] = database_error
    if args.commit:
        if database_error or engine is None or snapshot is None:
            raise RuntimeError('Import cannot commit because the database check failed.')
        if not args.report_json:
            raise RuntimeError('--commit requires --report-json for Resource IDs and rollback data.')
        verify_reviewed_report(
            args.verified_report,
            feed.sha256,
            database_target(engine),
            report['plan_sha256'],
        )
        report = import_posts(
            engine,
            posts,
            report,
            progress=lambda current: save_report(args.report_json, current),
        )

    write_report(args.report_json, report)
    print_summary(report)
    if database_error and not args.offline:
        return 2
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(2)
