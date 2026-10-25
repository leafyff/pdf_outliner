"""Тести перетворювача md -> PDF.

Сам експорт робить Obsidian, тож тут перевіряється все, що навколо нього:
читання налаштувань, вибір сховища, встановлення плагіна, побудова
посилання obsidian:// та очікування відповіді.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdf_outline_maker import md_export
from pdf_outline_maker.config import Settings


class ExportConfigTests(unittest.TestCase):
    """Секція "export" у config.json."""

    def test_defaults(self):
        settings = Settings()
        self.assertEqual(settings.export.page_size, "A4")
        self.assertTrue(settings.export.include_name)
        self.assertEqual(settings.export.downscale, 100)
        self.assertEqual(settings.export.input_dir, Path("md"))

    def test_apply_known_keys(self):
        settings = Settings()
        settings.apply({"export": {"page_size": "A3", "downscale": 80,
                                   "include_name": False, "input_dir": "нотатки"}})
        self.assertEqual(settings.export.page_size, "A3")
        self.assertEqual(settings.export.downscale, 80)
        self.assertFalse(settings.export.include_name)
        self.assertEqual(settings.export.input_dir, Path("нотатки"))

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            Settings().apply({"export": {"page_sizes": "A3"}})

    def test_bad_values_rejected(self):
        with self.assertRaises(ValueError):
            Settings().apply({"export": {"page_size": "A6"}})
        with self.assertRaises(ValueError):
            Settings().apply({"export": {"downscale": 5}})
        with self.assertRaises(ValueError):
            Settings().apply({"export": {"margin": "wide"}})
        with self.assertRaises(ValueError):
            Settings().apply({"export": {"outline": "магія"}})

    def test_detection_settings_still_work(self):
        settings = Settings()
        settings.apply({"detection": {"score_threshold": 4.5}})
        self.assertEqual(settings.detection.score_threshold, 4.5)


class UriTests(unittest.TestCase):
    """Посилання obsidian://, яким програма гукає плагін."""

    def setUp(self):
        self.vault = {"id": "abc", "path": Path(r"C:\Vault"), "open": True, "ts": 1}
        self.export = Settings().export

    def _params(self, source: Path, target: Path) -> dict:
        uri = md_export.build_uri(self.vault, source, target, self.export)
        parsed = urlparse(uri)
        self.assertEqual(parsed.scheme, "obsidian")
        self.assertEqual(parsed.netloc, md_export.PROTOCOL_ACTION)
        return {key: value[0] for key, value in parse_qs(parsed.query).items()}

    def test_carries_every_setting(self):
        params = self._params(Path(r"C:\in\note.md"), Path(r"C:\out\note.pdf"))
        self.assertEqual(params["vault"], "Vault")
        self.assertEqual(params["src"], r"C:\in\note.md")
        self.assertEqual(params["out"], r"C:\out\note.pdf")
        self.assertEqual(params["status"], r"C:\out\note.pdf.status")
        self.assertEqual(params["pageSize"], "A4")
        self.assertEqual(params["includeName"], "1")
        self.assertEqual(params["downscale"], "100")

    def test_spaces_survive(self):
        """Пробіл має їхати як %20: «+» розбирач посилань не розкодовує."""
        source = Path(r"C:\in\Лабораторна робота 5.md")
        uri = md_export.build_uri(self.vault, source, source.with_suffix(".pdf"), self.export)
        self.assertNotIn("+", uri)
        params = self._params(source, source.with_suffix(".pdf"))
        self.assertEqual(params["src"], str(source))

    def test_outline_mode_md_asks_for_plain_print(self):
        """Типово просимо звичайний друк: зміст допишемо самі."""
        params = self._params(Path(r"C:\in\n.md"), Path(r"C:\out\n.pdf"))
        self.assertEqual(params["outline"], "0")
        self.assertEqual(params["tagged"], "0")

    def test_outline_mode_chromium(self):
        self.export.outline = "chromium"
        params = self._params(Path(r"C:\in\n.md"), Path(r"C:\out\n.pdf"))
        self.assertEqual(params["outline"], "1")
        self.assertEqual(params["tagged"], "1")

    def test_flags_follow_settings(self):
        self.export.include_name = False
        self.export.landscape = True
        self.export.margin = "none"
        self.export.downscale = 60
        params = self._params(Path(r"C:\in\n.md"), Path(r"C:\out\n.pdf"))
        self.assertEqual(params["includeName"], "0")
        self.assertEqual(params["landscape"], "1")
        self.assertEqual(params["margin"], "none")
        self.assertEqual(params["downscale"], "60")


class VaultTests(unittest.TestCase):
    """Вибір сховища серед тих, про які знає Obsidian."""

    def setUp(self):
        self.vaults = [
            {"id": "1", "path": Path(r"C:\Users\X\Notes"), "open": True, "ts": 10},
            {"id": "2", "path": Path(r"D:\Архів"), "open": False, "ts": 20},
        ]
        self._original = md_export.known_vaults
        md_export.known_vaults = lambda: list(self.vaults)

    def tearDown(self):
        md_export.known_vaults = self._original

    def test_prefers_open_vault(self):
        self.assertEqual(md_export.pick_vault(None)["id"], "1")

    def test_by_name(self):
        self.assertEqual(md_export.pick_vault("Архів")["id"], "2")

    def test_by_path(self):
        self.assertEqual(md_export.pick_vault(r"D:\Архів")["id"], "2")

    def test_unknown_name(self):
        with self.assertRaises(md_export.ExportError):
            md_export.pick_vault("Чуже сховище")


class PluginInstallTests(unittest.TestCase):
    """Копіювання плагіна у сховище."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_install(self):
        state = md_export.install_plugin(self.vault)
        self.assertTrue(state.first_install)
        self.assertTrue(state.copied)
        self.assertTrue(state.just_enabled)
        self.assertTrue(state.needs_reload)
        folder = self.vault / ".obsidian" / "plugins" / md_export.PLUGIN_ID
        for name in md_export.PLUGIN_FILES:
            self.assertTrue((folder / name).exists(), name)
        enabled = json.loads((self.vault / ".obsidian" / "community-plugins.json").read_text("utf-8"))
        self.assertIn(md_export.PLUGIN_ID, enabled)

    def test_second_run_is_quiet(self):
        md_export.install_plugin(self.vault)
        state = md_export.install_plugin(self.vault)
        self.assertFalse(state.copied)
        self.assertFalse(state.just_enabled)
        self.assertFalse(state.needs_reload)

    def test_keeps_other_plugins_enabled(self):
        path = self.vault / ".obsidian" / "community-plugins.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(["obsidian-git"]), encoding="utf-8")
        md_export.install_plugin(self.vault)
        enabled = json.loads(path.read_text("utf-8"))
        self.assertEqual(enabled, ["obsidian-git", md_export.PLUGIN_ID])

    def test_updates_changed_file(self):
        md_export.install_plugin(self.vault)
        target = self.vault / ".obsidian" / "plugins" / md_export.PLUGIN_ID / "main.js"
        target.write_text("// застаріла версія", encoding="utf-8")
        state = md_export.install_plugin(self.vault)
        self.assertTrue(state.copied)
        self.assertFalse(state.first_install)
        self.assertNotEqual(target.read_text("utf-8"), "// застаріла версія")


class StatusTests(unittest.TestCase):
    """Відповідь плагіна, яку програма чекає у файлі *.status."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "note.pdf.status"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_ready_answer(self):
        self.path.write_text(json.dumps({"ok": True, "out": "note.pdf"}), encoding="utf-8")
        self.assertTrue(md_export.wait_for_status(self.path, 1.0)["ok"])

    def test_waits_for_late_answer(self):
        def write_later():
            time.sleep(0.3)
            self.path.write_text(json.dumps({"ok": False, "error": "нема файлу"}), encoding="utf-8")

        thread = threading.Thread(target=write_later)
        thread.start()
        try:
            answer = md_export.wait_for_status(self.path, 5.0)
        finally:
            thread.join()
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"], "нема файлу")

    def test_timeout(self):
        with self.assertRaises(md_export.ExportError):
            md_export.wait_for_status(self.path, 0.4)


class PromptTests(unittest.TestCase):
    """Три питання, які програма ставить при запуску подвійним клацанням."""

    def setUp(self):
        self.export = Settings().export
        # Питання й підказки не повинні засмічувати вивід тестів.
        silence = contextlib.redirect_stdout(io.StringIO())
        silence.__enter__()
        self.addCleanup(lambda: silence.__exit__(None, None, None))

    def _answers(self, *answers):
        """Підставляє відповіді замість клавіатури."""
        queue = list(answers)
        # Глобальна змінна модуля затуляє вбудований input() у ask().
        md_export.input = lambda _prompt="": queue.pop(0)  # type: ignore[attr-defined]
        self.addCleanup(lambda: delattr(md_export, "input"))

    def test_enter_keeps_defaults(self):
        self._answers("", "", "")
        md_export.ask_settings(self.export)
        self.assertEqual(self.export.page_size, "A4")
        self.assertTrue(self.export.include_name)
        self.assertEqual(self.export.downscale, 100)

    def test_custom_answers(self):
        self._answers("a3", "ні", "80")
        md_export.ask_settings(self.export)
        self.assertEqual(self.export.page_size, "A3")
        self.assertFalse(self.export.include_name)
        self.assertEqual(self.export.downscale, 80)

    def test_asks_again_after_nonsense(self):
        self._answers("A9", "Letter", "можливо", "так", "999", "50")
        md_export.ask_settings(self.export)
        self.assertEqual(self.export.page_size, "Letter")
        self.assertTrue(self.export.include_name)
        self.assertEqual(self.export.downscale, 50)


class SourceTests(unittest.TestCase):
    """Пошук вхідних .md і підбір імені результату."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.export = Settings().export

    def tearDown(self):
        self.tmp.cleanup()

    def test_folder_argument(self):
        (self.folder / "б.md").write_text("# б", encoding="utf-8")
        (self.folder / "а.md").write_text("# а", encoding="utf-8")
        (self.folder / "нотатка.txt").write_text("не md", encoding="utf-8")
        found = md_export.collect_sources([self.folder], self.export, self.folder)
        self.assertEqual([path.name for path in found], ["а.md", "б.md"])

    def test_relative_path_from_caller_dir(self):
        (self.folder / "нотатка.md").write_text("# тест", encoding="utf-8")
        found = md_export.collect_sources([Path("нотатка.md")], self.export, self.folder)
        self.assertEqual(found[0], (self.folder / "нотатка.md").resolve())

    def test_empty_folder_explains(self):
        self.export.input_dir = self.folder
        with self.assertRaises(md_export.ExportError):
            md_export.collect_sources([], self.export, self.folder)

    def test_target_next_to_source(self):
        source = self.folder / "нотатка.md"
        self.assertEqual(md_export.target_for(source, self.export), self.folder / "нотатка.pdf")

    def test_target_in_output_dir(self):
        self.export.output_dir = self.folder / "pdf"
        target = md_export.target_for(self.folder / "нотатка.md", self.export)
        self.assertEqual(target, self.folder / "pdf" / "нотатка.pdf")
        self.assertTrue(target.parent.is_dir())


if __name__ == "__main__":
    unittest.main()
