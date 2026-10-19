import json
import unittest

from beulah_pkg.blogger_normalization import (
    normalize_resource_html,
    normalize_verified_prose_boundaries,
)


class BloggerNormalizationTests(unittest.TestCase):
    def test_repairs_nested_blocks_and_preserves_semantics(self):
        source = (
            '<span><div>First <em>italic</em>.</div><div><br></div>'
            '<div>Second <sup>2</sup>.</div></span>'
            '<strong><div>Bold ending.</div></strong>'
            '<a href="https://example.com"></a>'
            '<img src="https://example.com/image.jpg" alt="Alt" title="Title">'
        )

        result = normalize_resource_html(source)

        self.assertEqual(result.validation_errors, [])
        self.assertEqual(
            result.html,
            '<p>First <em>italic</em>.</p><p>Second <sup>2</sup>.</p>'
            '<p><strong>Bold ending.</strong></p><p><a href="https://example.com"></a></p>'
            '<img src="https://example.com/image.jpg" alt="Alt" title="Title">',
        )
        self.assertEqual(result.metrics.div_paragraph_blocks_converted, 3)
        self.assertGreaterEqual(result.metrics.empty_spacer_blocks_removed, 1)
        self.assertGreaterEqual(result.metrics.malformed_nesting_repairs, 2)

        document = json.loads(result.content_json)
        self.assertEqual(document['type'], 'doc')
        self.assertTrue(any(node['type'] == 'image' for node in document['content']))

    def test_is_idempotent_and_preserves_lists_and_links(self):
        source = (
            '<div>Intro<br>continued</div>'
            '<ol start="2"><li><a href="https://example.com">Linked item</a></li>'
            '<li><u>Underlined</u></li></ol>'
        )

        first = normalize_resource_html(source)
        second = normalize_resource_html(first.html, first.content_json)

        self.assertEqual(first.validation_errors, [])
        self.assertEqual(second.validation_errors, [])
        self.assertEqual(first.html, second.html)
        self.assertEqual(first.content_json, second.content_json)

    def test_splits_only_verified_top_level_prose_boundaries(self):
        source = (
            '<p><br>Opening <em>italic</em> text.<br>Next <strong>bold</strong> text.<br></p>'
            '<ul><li>Intentional line one<br>Intentional line two</li></ul>'
            '<blockquote><p>Quoted line one<br>Quoted line two</p></blockquote>'
        )
        canonical = normalize_resource_html(source)
        self.assertEqual(canonical.validation_errors, [])

        result = normalize_verified_prose_boundaries(
            canonical.html,
            canonical.content_json,
            expected_boundaries=1,
        )

        self.assertEqual(result.validation_errors, [])
        self.assertEqual(result.boundaries_split, 1)
        self.assertEqual(result.break_elements_removed, 1)
        self.assertIn(
            '<p><br>Opening <em>italic</em> text.</p>'
            '<p>Next <strong>bold</strong> text.<br></p>',
            result.html,
        )
        self.assertIn(
            '<ul><li>Intentional line one<br>Intentional line two</li></ul>',
            result.html,
        )
        self.assertIn(
            '<blockquote><p>Quoted line one<br>Quoted line two</p></blockquote>',
            result.html,
        )
        self.assertEqual(
            result.original_signature.visible_text,
            result.normalized_signature.visible_text,
        )
        self.assertEqual(
            result.original_signature.formatting,
            result.normalized_signature.formatting,
        )

    def test_treats_whitespace_separated_double_break_as_one_boundary(self):
        canonical = '<p>First paragraph.<br> <br>Second paragraph.</p>'

        result = normalize_verified_prose_boundaries(
            canonical,
            None,
            expected_boundaries=1,
        )

        self.assertEqual(result.validation_errors, [])
        self.assertEqual(result.boundaries_split, 1)
        self.assertEqual(result.break_elements_removed, 2)
        self.assertEqual(
            result.html,
            '<p>First paragraph.</p><p>Second paragraph.</p>',
        )

    def test_rejects_a_changed_verified_boundary_count(self):
        canonical = '<p>First paragraph.<br>Second paragraph.</p>'

        result = normalize_verified_prose_boundaries(
            canonical,
            None,
            expected_boundaries=2,
        )

        self.assertIn(
            'expected 2 verified prose boundaries, found 1',
            result.validation_errors,
        )


if __name__ == '__main__':
    unittest.main()
