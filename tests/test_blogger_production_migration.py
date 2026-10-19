import unittest

from scripts.migrate_blogger_production import HARD_BREAK_RULES


class BloggerProductionMigrationTests(unittest.TestCase):
    def test_hard_break_rules_use_stable_blogger_identity(self):
        self.assertEqual(len(HARD_BREAK_RULES), 13)
        self.assertEqual(
            sum(int(rule['boundaries']) for rule in HARD_BREAK_RULES.values()),
            36,
        )
        for entry_id, rule in HARD_BREAK_RULES.items():
            self.assertIsInstance(entry_id, str)
            self.assertTrue(entry_id.startswith('tag:blogger.com,'))
            self.assertNotIn('resource_id', rule)
            self.assertTrue(rule['title'])
            self.assertTrue(rule['requested_slug'])
            self.assertTrue(rule['published_at'])


if __name__ == '__main__':
    unittest.main()
