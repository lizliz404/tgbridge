import unittest

from tgbridge_core.rendering import (
    _balanced,
    _strip_html_markup,
    _wrap_markdown_tables,
    md_to_html,
    split_chunks,
    utf16_len,
)


class RenderingTests(unittest.TestCase):
    def test_inline_entities_and_escaping(self):
        html, in_pre, _ = md_to_html(
            "code `<b>&</b>` and **bold** [t](http://x/y)"
        )
        self.assertEqual(
            html,
            "code <code>&lt;b&gt;&amp;&lt;/b&gt;</code> and <b>bold</b> "
            '<a href="http://x/y">t</a>',
        )
        self.assertFalse(in_pre)
        html, _, _ = md_to_html("*it* ~~gone~~ ||shh|| ***both***")
        self.assertEqual(
            html,
            "<i>it</i> <s>gone</s> <tg-spoiler>shh</tg-spoiler> "
            "<b><i>both</i></b>",
        )

    def test_lists_headers_and_quotes(self):
        html, _, _ = md_to_html("## **Title** here\n- one\n* two\n+ three")
        self.assertEqual(html, "<b>Title here</b>\n• one\n• two\n• three")
        html, _, _ = md_to_html("> line1\n> line2\nafter")
        self.assertEqual(html, "<blockquote>line1\nline2</blockquote>\nafter")
        html, _, _ = md_to_html("**> details\n> more||\nafter")
        self.assertEqual(
            html, "<blockquote expandable>details\nmore</blockquote>\nafter"
        )
        self.assertNotIn("<blockquote>", md_to_html("a > b implies")[0])

    def test_tables_render_as_bullet_groups_except_in_fences(self):
        rendered = _wrap_markdown_tables("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertIn("**1**", rendered)
        self.assertIn("• b: 2", rendered)
        self.assertNotIn("|", rendered)
        fenced = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
        self.assertIn("| 1 | 2 |", _wrap_markdown_tables(fenced))

    def test_fence_state_carries_between_chunks(self):
        first, in_pre, language = md_to_html("intro\n```py\nprint(1)")
        second, in_pre_after, language_after = md_to_html(
            "print(2)\n```", in_pre=in_pre, pre_lang=language
        )
        self.assertTrue(in_pre)
        self.assertFalse(in_pre_after)
        self.assertEqual(language, "py")
        self.assertEqual(language_after, "")
        self.assertIn("</code></pre>", first)
        self.assertIn("language-py", second)

    def test_balance_and_plain_fallback(self):
        self.assertTrue(_balanced("<b>a<code>b</code></b> &amp; <i>c</i>"))
        self.assertFalse(_balanced("<b>a<code>b</b>"))
        self.assertFalse(_balanced("</b>"))
        plain = _strip_html_markup(
            "# Head\n\n**hi** `x <y>` [t](http://e/x)\n> q\n```py\ncode\n```"
        )
        self.assertEqual(plain, "Head\n\nhi x <y> t (http://e/x)\nq\ncode")

    def test_utf16_chunking_preserves_content(self):
        emoji = "😀" * 10
        self.assertEqual(utf16_len(emoji), 20)
        self.assertEqual("".join(split_chunks(emoji + "x", limit=15)), emoji + "x")
        self.assertEqual(
            split_chunks("a" * 45, limit=20), ["a" * 20, "a" * 20, "a" * 5]
        )
        parts = split_chunks("word " + "z" * 40 + " `code span` tail", limit=20)
        self.assertEqual("\n".join(parts).count("`") % 2, 0)


if __name__ == "__main__":
    unittest.main()
