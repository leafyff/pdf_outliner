"""Тести змісту, побудованого з розмітки `.md`.

PDF для перевірки пошуку малюються на льоту вбудованими шрифтами (кирилиці
в них немає, тож заголовки тут латиницею — перевіряється механіка пошуку).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf

from pdf_outline_maker import md_outline


def make_pdf(path: Path, pages: list[list[str]]) -> Path:
    """Малює PDF: по рядку тексту на кожен елемент сторінки."""
    document = pymupdf.open()
    for lines in pages:
        page = document.new_page(width=595, height=842)
        y = 80.0
        for line in lines:
            page.insert_text((72, y), line, fontsize=14, fontname="helv")
            y += 40
    document.save(path)
    document.close()
    return path


class ParseTests(unittest.TestCase):
    def test_levels_and_order(self):
        markdown = "# Один\n\nтекст\n\n## Два\n\n### Три\n\n## Ще два\n"
        self.assertEqual(
            md_outline.parse_headings(markdown),
            [(1, "Один"), (2, "Два"), (3, "Три"), (2, "Ще два")],
        )

    def test_skips_code_blocks(self):
        markdown = "# Назва\n\n```python\n# це коментар, не заголовок\n```\n\n## Далі\n"
        self.assertEqual(md_outline.parse_headings(markdown), [(1, "Назва"), (2, "Далі")])

    def test_skips_tilde_fence(self):
        markdown = "~~~\n# не заголовок\n~~~\n\n# заголовок\n"
        self.assertEqual(md_outline.parse_headings(markdown), [(1, "заголовок")])

    def test_skips_frontmatter(self):
        markdown = "---\ntitle: # не заголовок\ntags: [a]\n---\n\n# Справжній\n"
        self.assertEqual(md_outline.parse_headings(markdown), [(1, "Справжній")])

    def test_horizontal_rule_is_not_frontmatter(self):
        markdown = "# Перший\n\n---\n\n# Другий\n"
        self.assertEqual(md_outline.parse_headings(markdown), [(1, "Перший"), (2 - 1, "Другий")])

    def test_closing_hashes_and_hash_without_space(self):
        markdown = "## Назва ##\n\n#тег не заголовок\n"
        self.assertEqual(md_outline.parse_headings(markdown), [(2, "Назва")])


class CleanTitleTests(unittest.TestCase):
    def test_strips_emphasis_and_code(self):
        self.assertEqual(md_outline.clean_title("**Розділ** *1* `code`"), "Розділ 1 code")

    def test_wikilink_alias(self):
        self.assertEqual(md_outline.clean_title("[[нотатка|Назва]]"), "Назва")
        self.assertEqual(md_outline.clean_title("[[Нотатка]]"), "Нотатка")
        self.assertEqual(md_outline.clean_title("[[Нотатка#розділ]]"), "Нотатка")

    def test_markdown_link(self):
        self.assertEqual(md_outline.clean_title("[Текст](https://example.com)"), "Текст")

    def test_highlight_strike_footnote(self):
        self.assertEqual(md_outline.clean_title("==Важливо==[^1] ~~ні~~"), "Важливо ні")

    def test_keeps_math_text(self):
        self.assertEqual(md_outline.clean_title("Задача $z \\to \\max$"), "Задача $z \\to \\max$")


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.opened: list = []

    def tearDown(self):
        # Windows не дає прибрати теку, доки MuPDF тримає файл відкритим.
        for document in self.opened:
            document.close()
        self.tmp.cleanup()

    def _document(self, pages: list[list[str]]):
        path = make_pdf(self.folder / "test.pdf", pages)
        document = pymupdf.open(path)
        self.opened.append(document)
        return document

    def test_pages_and_nesting(self):
        document = self._document([
            ["Alpha", "Beta"],
            ["Gamma"],
        ])
        nodes = md_outline.build(document, "# Alpha\n\n## Beta\n\n## Gamma\n")
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].title, "Alpha")
        self.assertEqual(nodes[0].page, 0)
        self.assertEqual([child.title for child in nodes[0].children], ["Beta", "Gamma"])
        self.assertEqual([child.page for child in nodes[0].children], [0, 1])
        self.assertIsNotNone(nodes[0].children[1].y)

    def test_repeated_titles_go_forward(self):
        document = self._document([["Intro", "Summary"], ["Second", "Summary"]])
        nodes = md_outline.build(document, "# Intro\n\n## Summary\n\n# Second\n\n## Summary\n")
        self.assertEqual([node.page for node in nodes], [0, 1])
        self.assertEqual([node.children[0].page for node in nodes], [0, 1])

    def test_missing_heading_keeps_previous_page(self):
        document = self._document([["Alpha"], ["Gamma"]])
        nodes = md_outline.build(document, "# Alpha\n\n## Beta\n\n## Gamma\n")
        beta = nodes[0].children[0]
        self.assertEqual(beta.page, 0)
        self.assertIsNone(beta.y)

    def test_levels_are_compacted(self):
        document = self._document([["Alpha", "Beta"]])
        nodes = md_outline.build(document, "## Alpha\n\n### Beta\n")
        self.assertEqual(nodes[0].title, "Alpha")
        self.assertEqual(nodes[0].children[0].title, "Beta")

    def test_max_depth(self):
        document = self._document([["Alpha", "Beta", "Gamma"]])
        nodes = md_outline.build(document, "# Alpha\n\n## Beta\n\n### Gamma\n", max_depth=2)
        self.assertEqual(len(nodes[0].children), 1)
        self.assertEqual(nodes[0].children[0].children, [])

    def test_title_node_wraps_the_rest(self):
        document = self._document([["Lab 5", "Alpha"], ["Beta"]])
        nodes = md_outline.build(document, "# Alpha\n\n# Beta\n", title="Lab 5")
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].title, "Lab 5")
        self.assertEqual([child.title for child in nodes[0].children], ["Alpha", "Beta"])

    def test_empty_markdown(self):
        document = self._document([["Alpha"]])
        self.assertEqual(md_outline.build(document, "просто текст без заголовків"), [])


if __name__ == "__main__":
    unittest.main()
