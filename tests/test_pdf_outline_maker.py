"""Тести. Запуск: `python -m unittest discover -s tests` з теки проєкту.

PDF для тестів створюються на льоту, тож тека data і зовнішні файли не потрібні.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf

from pdf_outline_maker import tree
from pdf_outline_maker.analyze import analyze, cluster_sizes, math_ratio
from pdf_outline_maker.config import DetectionSettings, Settings
from pdf_outline_maker.detect import detect
from pdf_outline_maker.discovery import collect_jobs, is_output_name, output_path_for
from pdf_outline_maker.extract import load, normalize_text
from pdf_outline_maker.model import OutlineNode, count_nodes, font_family
from pdf_outline_maker.overrides import OverrideError, load_override
from pdf_outline_maker.pipeline import STATUS_EMPTY, STATUS_OK, STATUS_SKIPPED, process
from pdf_outline_maker.discovery import Job
from pdf_outline_maker.writer import write_outline

# Вбудовані шрифти PDF (base-14) не мають кирилиці, тому сторінки для тестів
# малюємо латиницею — перевіряється механіка детектора, а не мова. Кирилицю
# перевіряємо там, де вона справді має значення: у назвах закладок.
HEADING = "hebo"   # Helvetica-Bold
BODY = "helv"      # Helvetica


def make_pdf(path: Path, pages: list, rotation: int = 0, crop: tuple = None) -> Path:
    """Малює PDF за описом сторінок: [[(текст, кегль, жирний), ...], ...]."""
    doc = pymupdf.open()
    for page_spec in pages:
        page = doc.new_page(width=595, height=842)
        if rotation:
            page.set_rotation(rotation)
        y = 70.0
        for text, size, bold in page_spec:
            page.insert_text(
                (72, y), text, fontsize=size,
                fontname=HEADING if bold else BODY, rotate=rotation,
            )
            y += size * 2.2
        if crop:
            page.set_cropbox(pymupdf.Rect(*crop))
    doc.save(str(path))
    doc.close()
    return path


def detect_titles(path: Path, settings=None) -> list:
    """Заголовки, які детектор знаходить у готовому файлі."""
    document = load(str(path))
    try:
        analysis = analyze(document.lines, document.page_count)
        return [c.title for c in detect(analysis, settings or DetectionSettings())]
    finally:
        document.close()


LOREM = ("This is an ordinary paragraph of running text that sets the body "
         "size of the document and must never end up in the outline.")


def sample_pages() -> list:
    body = [(LOREM, 11, False) for _ in range(6)]
    return [
        [("1. Introduction", 20, True)] + body + [("1.1. Problem statement", 15, True)] + body,
        [("2. Method", 20, True)] + body + [("2.1. Algorithm", 15, True)] + body,
    ]


class TestDiscovery(unittest.TestCase):
    def test_output_names_are_never_reprocessed(self):
        self.assertTrue(is_output_name(Path("lab1_outline.pdf")))
        self.assertTrue(is_output_name(Path("LAB1_OUTLINE.PDF")))
        self.assertFalse(is_output_name(Path("lab1.pdf")))
        self.assertFalse(is_output_name(Path("outline.pdf")))

    def test_output_path(self):
        self.assertEqual(output_path_for(Path("data/lab1.pdf")).name, "lab1_outline.pdf")

    def test_collect_skips_results_and_junk(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ("lab1.pdf", "lab1_outline.pdf", "~$draft.pdf", "нотатки.txt"):
                (folder / name).write_bytes(b"%PDF-1.4\n")
            jobs, skipped = collect_jobs(folder)
            self.assertEqual([j.source.name for j in jobs], ["lab1.pdf"])
            self.assertEqual(len(skipped), 2)

    def test_second_run_does_not_chain_suffixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            make_pdf(folder / "lab1.pdf", sample_pages())
            settings = Settings()
            jobs, _ = collect_jobs(folder)
            process(jobs[0], settings)
            self.assertTrue((folder / "lab1_outline.pdf").exists())
            jobs_again, skipped = collect_jobs(folder)
            self.assertEqual([j.source.name for j in jobs_again], ["lab1.pdf"])
            self.assertFalse((folder / "lab1_outline_outline.pdf").exists())


class TestTextHelpers(unittest.TestCase):
    def test_normalize_collapses_space_and_dot_leaders(self):
        self.assertEqual(normalize_text("  Розділ 1   Вступ  "), "Розділ 1 Вступ")
        self.assertEqual(normalize_text("Вступ ......... 12"), "Вступ")

    def test_font_family_drops_weight_and_tex_size(self):
        self.assertEqual(font_family("ABCDEF+NotoSerif-Bold"), "notoserif")
        self.assertEqual(font_family("SFRM1095"), "sfrm")
        self.assertEqual(font_family("SFRM1440"), "sfrm")
        self.assertNotEqual(font_family("SFCC1095"), font_family("SFRM1095"))

    def test_cluster_sizes_merges_near_values(self):
        mapping = cluster_sizes([20.0, 20.1, 20.2, 24.0])
        self.assertEqual(mapping[20.0], mapping[20.2])
        self.assertNotEqual(mapping[20.0], mapping[24.0])

    def test_math_ratio(self):
        self.assertGreater(math_ratio("x1 + 2x3 = 8"), 0.3)
        self.assertLess(math_ratio("Основні означення"), 0.1)


class TestDetection(unittest.TestCase):
    def test_finds_two_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "doc.pdf", sample_pages())
            document = load(str(path))
            try:
                analysis = analyze(document.lines, document.page_count)
                found = detect(analysis, DetectionSettings())
            finally:
                document.close()
            titles = [c.title for c in found]
            self.assertIn("1. Introduction", titles)
            self.assertIn("1.1. Problem statement", titles)
            self.assertNotIn(LOREM, titles)
            levels = {c.title: c.level for c in found}
            self.assertLess(levels["1. Introduction"], levels["1.1. Problem statement"])

    def test_body_text_alone_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            pages = [[(LOREM, 11, False) for _ in range(12)] for _ in range(3)]
            path = make_pdf(Path(tmp) / "flat.pdf", pages)
            document = load(str(path))
            try:
                analysis = analyze(document.lines, document.page_count)
                found = detect(analysis, DetectionSettings())
            finally:
                document.close()
            self.assertEqual(found, [])


class TestAwkwardPages(unittest.TestCase):
    """Випадки, на яких програма колись мовчки губила заголовки."""

    def _pages(self, titles):
        body = [(LOREM, 11, False) for _ in range(5)]
        return [[(t, 20, True)] + body + [("Low heading " + t[-1], 20, True)] for t in titles]

    def test_rotated_pages_keep_every_heading(self):
        titles = ["Alpha 1", "Beta 2", "Gamma 3"]
        with tempfile.TemporaryDirectory() as tmp:
            straight = make_pdf(Path(tmp) / "straight.pdf", self._pages(titles))
            expected = len(detect_titles(straight))
            self.assertEqual(expected, 6)
            for rotation in (90, 180, 270):
                path = make_pdf(Path(tmp) / ("rot%d.pdf" % rotation),
                                self._pages(titles), rotation=rotation)
                self.assertEqual(len(detect_titles(path)), expected,
                                 "втрачено заголовки при повороті %d" % rotation)

    def test_numbered_heading_on_every_page_is_not_a_running_head(self):
        titles = ["Practical work %d" % i for i in range(1, 6)]
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "numbered.pdf", self._pages(titles))
            found = detect_titles(path)
            for title in titles:
                self.assertIn(title, found)

    def test_page_footer_is_still_dropped(self):
        pages = []
        for i in range(1, 7):
            body = [(LOREM, 11, False) for _ in range(5)]
            pages.append([("Chapter " + "ABCDEF"[i - 1], 20, True)] + body
                         + [("Page %d of 6" % i, 8, False)])
        with tempfile.TemporaryDirectory() as tmp:
            path = make_pdf(Path(tmp) / "footer.pdf", pages)
            found = detect_titles(path)
            self.assertTrue(any(t.startswith("Chapter") for t in found))
            self.assertFalse([t for t in found if t.startswith("Page ")])

    def test_cropped_page_destination_lands_on_the_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = make_pdf(folder / "crop.pdf", sample_pages(),
                              crop=(40, 60, 555, 800))
            target = folder / "crop_outline.pdf"
            write_outline(source, target, [OutlineNode("Десь", 0, 300.0)])
            result = pymupdf.open(str(target))
            try:
                back = result.get_toc(simple=False)[0][3]["to"].y
                # 8pt — свідомий відступ над заголовком.
                self.assertAlmostEqual(back, 292.0, delta=1.0)
            finally:
                result.close()


class TestTree(unittest.TestCase):
    def test_level_jump_is_normalised(self):
        class FakeLine:
            y0 = 10.0

        class FakeCandidate:
            def __init__(self, title, level, page):
                self.title, self.level, self.page = title, level, page
                self.line = FakeLine()

        nodes = tree.build_tree([
            FakeCandidate("A", 1, 0),
            FakeCandidate("B", 3, 0),   # стрибок 1 -> 3
            FakeCandidate("C", 1, 1),
        ])
        self.assertEqual(len(nodes), 2)
        self.assertEqual(len(nodes[0].children), 1)
        self.assertEqual(nodes[0].children[0].title, "B")

    def test_count_nodes(self):
        nodes = [OutlineNode("A", 0, children=[OutlineNode("B", 0)])]
        self.assertEqual(count_nodes(nodes), 2)


class TestWriter(unittest.TestCase):
    def test_outline_is_written_and_content_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = make_pdf(folder / "doc.pdf", sample_pages())
            before = pymupdf.open(str(source))
            text_before = before[0].get_text()
            before.close()

            target = folder / "doc_outline.pdf"
            nodes = [OutlineNode("Розділ 1", 0, 70.0, [OutlineNode("1.1", 0, 200.0)])]
            written = write_outline(source, target, nodes)
            self.assertEqual(written, 2)

            result = pymupdf.open(str(target))
            try:
                self.assertEqual(result.get_toc(), [[1, "Розділ 1", 1], [2, "1.1", 1]])
                self.assertEqual(
                    result.xref_get_key(result.pdf_catalog(), "PageMode"),
                    ("name", "/UseOutlines"),
                )
                self.assertEqual(result[0].get_text(), text_before)
                self.assertEqual(result.page_count, 2)
            finally:
                result.close()

    def test_destination_points_at_the_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = make_pdf(folder / "doc.pdf", sample_pages())
            target = folder / "doc_outline.pdf"
            write_outline(source, target, [OutlineNode("Десь нижче", 1, 400.0)])
            result = pymupdf.open(str(target))
            try:
                dest = result.get_toc(simple=False)[0][3]
                self.assertAlmostEqual(dest["to"].y, 392.0, delta=1.0)
            finally:
                result.close()


class TestWriterEdgeCases(unittest.TestCase):
    def test_repaired_file_still_gets_an_outline(self):
        """PDF зі зламаним xref MuPDF лагодить — результат має бути записаний."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            good = make_pdf(folder / "good.pdf", sample_pages())
            data = good.read_bytes()
            broken = folder / "broken.pdf"
            cut = data.rfind(b"startxref")
            broken.write_bytes(data[:cut] + b"startxref\n999999\n%%EOF\n")

            job = Job(source=broken, target=output_path_for(broken))
            result = process(job, Settings())
            self.assertEqual(result.status, STATUS_OK, result.message)
            self.assertTrue(job.target.exists())
            self.assertEqual(list(folder.glob("*.part*")), [])

    def test_encryption_and_permissions_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            plain = make_pdf(folder / "plain.pdf", sample_pages())
            source = folder / "secure.pdf"
            doc = pymupdf.open(str(plain))
            doc.save(str(source), encryption=pymupdf.PDF_ENCRYPT_AES_256,
                     owner_pw="owner", user_pw="",
                     permissions=int(pymupdf.PDF_PERM_ACCESSIBILITY | pymupdf.PDF_PERM_PRINT))
            doc.close()

            for incremental in (True, False):
                target = folder / ("secure_%s_outline.pdf" % incremental)
                settings = Settings()
                settings.incremental = incremental
                result = process(Job(source=source, target=target), settings)
                self.assertEqual(result.status, STATUS_OK, result.message)
                out = pymupdf.open(str(target))
                try:
                    self.assertTrue(out.authenticate(""))
                    self.assertIn("AES", str(out.metadata.get("encryption")))
                finally:
                    out.close()

    def test_existing_outline_keeps_its_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = make_pdf(folder / "withtoc.pdf", sample_pages())
            doc = pymupdf.open(str(source))
            doc.set_toc([[1, "Перший", 1, {"kind": 1, "page": 0,
                                           "to": pymupdf.Point(0, 500), "zoom": 0}]])
            doc.save(str(source), incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
            doc.close()

            job = Job(source=source, target=output_path_for(source))
            result = process(job, Settings())
            self.assertEqual(result.source, "наявний зміст")
            out = pymupdf.open(str(job.target))
            try:
                self.assertAlmostEqual(out.get_toc(simple=False)[0][3]["to"].y, 500.0, delta=0.5)
            finally:
                out.close()


class TestDepth(unittest.TestCase):
    def _three_level_pdf(self, folder: Path) -> Path:
        body = [(LOREM, 11, False) for _ in range(4)]
        pages = []
        for i in (1, 2):
            pages.append([("%d. Chapter" % i, 22, True)] + body
                         + [("%d.1. Section" % i, 16, True)] + body
                         + [("%d.1.1. Subsection" % i, 13, True)] + body)
        return make_pdf(folder / "deep.pdf", pages)

    def test_max_depth_only_trims_the_deepest_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = self._three_level_pdf(folder)
            counts = {}
            for depth in (1, 2, 3):
                settings = Settings()
                settings.dry_run = True
                settings.detection.max_depth = depth
                job = Job(source=source, target=output_path_for(source))
                result = process(job, settings)
                counts[depth] = (result.headings, result.depth)
            self.assertEqual(counts[1][0], 2)          # лише розділи
            self.assertEqual(counts[2][0], 4)          # + підрозділи
            self.assertEqual(counts[3][0], 6)          # + пункти
            for depth in (1, 2, 3):
                self.assertLessEqual(counts[depth][1], depth)


class TestOverrides(unittest.TestCase):
    def test_tree_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = make_pdf(folder / "lab1.pdf", sample_pages())
            (folder / "lab1.outline.json").write_text(
                json.dumps([{"title": "Ручний розділ", "page": 2,
                             "children": [{"title": "Підпункт", "page": 2}]}],
                           ensure_ascii=False),
                encoding="utf-8",
            )
            nodes = load_override(source, 2)
            self.assertEqual(nodes[0].title, "Ручний розділ")
            self.assertEqual(nodes[0].page, 1)          # у файлі 1-based, всередині 0-based
            self.assertEqual(nodes[0].children[0].title, "Підпункт")

    def test_mixed_form_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = make_pdf(folder / "lab1.pdf", sample_pages())
            (folder / "lab1.outline.json").write_text(
                json.dumps([{"level": 1, "title": "A", "page": 1,
                             "children": [{"level": 2, "title": "A1", "page": 1}]}]),
                encoding="utf-8")
            with self.assertRaises(OverrideError):
                load_override(source, 2)

    def test_flat_form_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = make_pdf(folder / "lab1.pdf", sample_pages())
            path = folder / "lab1.outline.json"
            path.write_text(json.dumps([{"level": 1, "title": "A", "page": 1},
                                        {"level": 2, "title": "B", "page": 1}]), encoding="utf-8")
            nodes = load_override(source, 2)
            self.assertEqual(nodes[0].children[0].title, "B")

            path.write_text("{ це не json", encoding="utf-8")
            with self.assertRaises(OverrideError):
                load_override(source, 2)


class TestPipeline(unittest.TestCase):
    def _job(self, folder: Path, name: str = "lab1.pdf") -> Job:
        source = make_pdf(folder / name, sample_pages())
        return Job(source=source, target=output_path_for(source))

    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp))
            result = process(job, Settings())
            self.assertEqual(result.status, STATUS_OK)
            self.assertTrue(job.target.exists())
            self.assertGreaterEqual(result.headings, 4)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp))
            settings = Settings()
            settings.dry_run = True
            result = process(job, settings)
            self.assertEqual(result.status, STATUS_OK)
            self.assertFalse(job.target.exists())

    def test_document_without_headings_is_reported_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            pages = [[(LOREM, 11, False) for _ in range(12)] for _ in range(2)]
            source = make_pdf(folder / "flat.pdf", pages)
            job = Job(source=source, target=output_path_for(source))
            result = process(job, Settings())
            self.assertEqual(result.status, STATUS_EMPTY)
            self.assertFalse(job.target.exists())

    def test_fallback_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            pages = [[(LOREM, 11, False) for _ in range(12)] for _ in range(3)]
            source = make_pdf(folder / "flat.pdf", pages)
            job = Job(source=source, target=output_path_for(source))
            settings = Settings()
            settings.fallback = "pages"
            result = process(job, settings)
            self.assertEqual(result.status, STATUS_OK)
            self.assertEqual(result.headings, 3)

    def test_existing_outline_is_kept_then_regenerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = make_pdf(folder / "lab1.pdf", sample_pages())
            doc = pymupdf.open(str(source))
            doc.set_toc([[1, "Готовий зміст", 1]])
            doc.save(str(source), incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
            doc.close()

            job = Job(source=source, target=output_path_for(source))
            kept = process(job, Settings())
            self.assertEqual(kept.source, "наявний зміст")
            self.assertEqual(kept.headings, 1)

            settings = Settings()
            settings.existing = "regenerate"
            fresh = process(job, settings)
            self.assertEqual(fresh.source, "аналіз тексту")
            self.assertGreater(fresh.headings, 1)

            settings.existing = "skip"
            skipped = process(job, settings)
            self.assertEqual(skipped.status, STATUS_SKIPPED)

    def test_broken_file_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "broken.pdf"
            source.write_bytes(b"not a pdf at all")
            job = Job(source=source, target=output_path_for(source))
            result = process(job, Settings())
            self.assertEqual(result.status, "failed")
            self.assertTrue(result.message)


class TestConfig(unittest.TestCase):
    def test_unknown_key_is_rejected(self):
        settings = Settings()
        with self.assertRaises(ValueError):
            settings.apply({"такого_немає": 1})

    def test_bom_and_wrong_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
                {"detection": {"max_depth": 3}}).encode("utf-8"))
            self.assertEqual(Settings.load(path).detection.max_depth, 3)

            path.write_text(json.dumps({"detection": {"max_depth": "3"}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                Settings.load(path)

    def test_comment_keys_are_ignored(self):
        settings = Settings()
        settings.apply({"_comment": "текст", "detection": {"_comment": "x", "max_depth": 2}})
        self.assertEqual(settings.detection.max_depth, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
