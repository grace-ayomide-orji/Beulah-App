from pathlib import Path
from dataclasses import replace
import unittest

from sqlalchemy import create_engine, text

from beulah_pkg.blogger_import import (
    build_plan,
    html_to_tiptap_json,
    import_posts,
    load_database_snapshot,
    normalize_blogger_html,
    parse_feed,
    rollback_report,
)


FIXTURE = __file__.replace('test_blogger_import.py', 'fixtures/blogger_sample.atom')


def create_schema(engine):
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE resources (
                resource_id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_title VARCHAR(255) NOT NULL,
                resource_body TEXT NOT NULL,
                resource_type VARCHAR(20) NOT NULL,
                resource_slug VARCHAR(255) UNIQUE,
                resource_featured_image VARCHAR(255),
                resource_status VARCHAR(20) NOT NULL,
                resource_published_date DATETIME,
                resource_category VARCHAR(100),
                resource_content_json TEXT,
                resource_date DATETIME,
                resource_updated_date DATETIME,
                resource_is_deleted BOOLEAN
            )
        """))
        connection.execute(text("""
            CREATE TABLE comments (
                comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_id INTEGER NOT NULL
            )
        """))


class BloggerMigrationTests(unittest.TestCase):
    def test_parse_and_tiptap_conversion(self):
        feed = parse_feed(Path(FIXTURE))
        self.assertEqual(len(feed.entries), 5)
        first = feed.entries[0]
        self.assertEqual(first.original_url, 'https://old.example/2020/01/shared.html')
        self.assertEqual(first.labels, ('Faith',))
        html = normalize_blogger_html(first.content_html)
        self.assertIn('<strong>Bold</strong>', html)
        self.assertIn('<em>italic</em>', html)
        document = html_to_tiptap_json(html)
        node_types = []
        mark_types = []

        def visit(node):
            node_types.append(node['type'])
            mark_types.extend(mark['type'] for mark in node.get('marks', []))
            for child in node.get('content', []):
                visit(child)

        visit(document)
        self.assertIn('heading', node_types)
        self.assertIn('bulletList', node_types)
        self.assertIn('bold', mark_types)
        self.assertIn('italic', mark_types)

    def test_resource_only_import_is_idempotent_and_report_rolls_back(self):
        engine = create_engine('sqlite+pysqlite:///:memory:')
        create_schema(engine)
        feed = parse_feed(Path(FIXTURE))
        snapshot = load_database_snapshot(engine)
        posts, report = build_plan(feed, snapshot)
        self.assertEqual(report['counts']['total_entries'], 5)
        self.assertEqual(report['counts']['would_import'], 2)
        self.assertEqual(report['counts']['skipped_items'], 3)
        self.assertEqual([post.slug for post in posts], ['shared', 'shared-2'])

        imported = import_posts(engine, posts, report)
        self.assertEqual(imported['counts']['imported'], 2)
        self.assertEqual(len(imported['imported']), 2)
        self.assertTrue(imported['imported'][0]['resource_fingerprint'])

        snapshot = load_database_snapshot(engine)
        posts, rerun_report = build_plan(feed, snapshot)
        self.assertEqual(posts, [])
        self.assertEqual(rerun_report['counts']['duplicates'], 2)

        result = rollback_report(engine, imported)
        self.assertEqual(result['rolled_back'], 2)
        with engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT COUNT(*) FROM resources')).scalar(), 0)

    def test_detects_a_manually_added_post_without_a_ledger_row(self):
        engine = create_engine('sqlite+pysqlite:///:memory:')
        create_schema(engine)
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO resources (
                    resource_title, resource_body, resource_type, resource_slug,
                    resource_status, resource_published_date, resource_date,
                    resource_updated_date, resource_is_deleted
                ) VALUES (
                    'First post', '<p>Manual copy</p>', 'blog', 'manual-first-post',
                    'published', '2020-01-01 10:00:00', '2020-01-01 10:00:00',
                    '2020-01-01 10:00:00', 0
                )
            """))

        feed = parse_feed(Path(FIXTURE))
        posts, report = build_plan(feed, load_database_snapshot(engine))
        self.assertEqual(len(posts), 1)
        self.assertEqual(report['counts']['duplicates'], 1)
        self.assertEqual(report['duplicates'][0]['reason'], 'title+published_date')

    def test_non_blog_resource_never_counts_as_a_duplicate(self):
        engine = create_engine('sqlite+pysqlite:///:memory:')
        create_schema(engine)
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO resources (
                    resource_title, resource_body, resource_type, resource_slug,
                    resource_status, resource_published_date, resource_date,
                    resource_updated_date, resource_is_deleted
                ) VALUES (
                    'First post', '<p>Text resource</p>', 'text', 'shared',
                    'published', '2020-01-01 10:00:00', '2020-01-01 10:00:00',
                    '2020-01-01 10:00:00', 0
                )
            """))

        feed = parse_feed(Path(FIXTURE))
        snapshot = load_database_snapshot(engine)
        posts, report = build_plan(feed, snapshot)
        self.assertEqual(snapshot.resources, [])
        self.assertEqual(snapshot.resource_counts_by_type, {'text': 1})
        self.assertEqual(report['counts']['duplicates'], 0)
        self.assertEqual(report['counts']['would_import'], 2)
        self.assertEqual(posts[0].slug, 'shared-2')
        self.assertEqual(report['planned_resources'][0]['resource_type'], 'blog')

    def test_live_only_plan_skips_drafts_without_changing_default_behavior(self):
        engine = create_engine('sqlite+pysqlite:///:memory:')
        create_schema(engine)
        feed = parse_feed(Path(FIXTURE))
        feed.entries.append(replace(
            feed.entries[0],
            entry_id='post-draft',
            title='Draft post',
            filename='/2020/01/draft-post.html',
            status='DRAFT',
        ))

        default_posts, _ = build_plan(feed, load_database_snapshot(engine))
        live_posts, live_report = build_plan(
            feed,
            load_database_snapshot(engine),
            allowed_statuses={'LIVE'},
        )

        self.assertEqual(len(default_posts), 3)
        self.assertEqual(len(live_posts), 2)
        self.assertEqual(live_report['counts']['drafts'], 1)
        self.assertEqual(live_report['counts']['would_import'], 2)


if __name__ == '__main__':
    unittest.main()
