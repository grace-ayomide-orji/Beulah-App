import unittest

from scripts.migrate_blogger_production import (
    HARD_BREAK_RULES,
    dash_preparation_diagnostic,
)


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

    def test_dash_validation_ignores_only_non_rendered_word_comments(self):
        diagnostic = dash_preparation_diagnostic(
            blogger_entry_id='entry-1',
            title='Word HTML',
            original_html=(
                '<style><!-- font-family:foo-bar; mso-font-alt:baz-qux; --></style>'
                '<p>Visible - text – remains — exact.</p>'
            ),
            prepared_html='<p>Visible - text – remains — exact.</p>',
        )

        self.assertEqual(
            diagnostic['visible_original_dash_code_points'],
            ['U+002D', 'U+2013', 'U+2014'],
        )
        self.assertEqual(
            diagnostic['prepared_dash_code_points'],
            ['U+002D', 'U+2013', 'U+2014'],
        )
        self.assertTrue(diagnostic['visible_sequence_preserved'])
        self.assertEqual(
            diagnostic['diagnosis'],
            'false_positive_from_non_rendered_word_style_comments',
        )

    def test_dash_validation_still_detects_a_real_visible_change(self):
        diagnostic = dash_preparation_diagnostic(
            blogger_entry_id='entry-2',
            title='Real mismatch',
            original_html='<p>Visible — text.</p>',
            prepared_html='<p>Visible - text.</p>',
        )

        self.assertFalse(diagnostic['visible_sequence_preserved'])
        self.assertEqual(diagnostic['diagnosis'], 'real_visible_dash_mismatch')
        self.assertEqual(diagnostic['visible_original_dash_code_points'], ['U+2014'])
        self.assertEqual(diagnostic['prepared_dash_code_points'], ['U+002D'])


if __name__ == '__main__':
    unittest.main()
