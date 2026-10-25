"""Вивід у консоль: налаштування кодування та звіт про обробку."""

from __future__ import annotations

import sys
from pathlib import Path

from . import tree
from .config import Settings
from .text import plural
from .pipeline import STATUS_EMPTY, STATUS_FAILED, STATUS_OK, STATUS_SKIPPED, Result

_LINE = "─" * 64


def setup_console() -> None:
    """Готує консоль Windows до українського тексту.

    Без цього `print` з кирилицею в cmd.exe падає з UnicodeEncodeError.
    """
    try:  # Перемикаємо кодову сторінку консолі на UTF-8.
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001 - не Windows або немає доступу
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - потік може бути перенаправлений
            pass


class Reporter:
    """Друкує хід роботи та підсумок."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # --- дрібні помічники ------------------------------------------------
    def _say(self, text: str = "") -> None:
        if not self.settings.quiet:
            print(text)

    def error(self, text: str) -> None:
        print(f"  ! {text}", file=sys.stderr)

    # --- частини звіту ---------------------------------------------------
    def header(self, input_dir: Path, total: int) -> None:
        self._say(_LINE)
        self._say("  PDF Outline Maker — додаємо зміст до PDF")
        self._say(_LINE)
        self._say(f"  Тека з файлами: {input_dir}")
        if self.settings.dry_run:
            self._say("  Режим: пробний запуск (файли не змінюються)")
        self._say(f"  Знайдено PDF до обробки: {total}")
        self._say()

    def skipped_inputs(self, skipped: list) -> None:
        if not skipped or not self.settings.verbose:
            return
        for path, reason in skipped:
            self._say(f"  · пропущено {path.name} — {reason}")
        self._say()

    def nothing_to_do(self, input_dir: Path) -> None:
        self._say(f"  У теці «{input_dir}» немає PDF для обробки.")
        self._say("  Покладіть туди файли (наприклад, lab1.pdf) і запустіть ще раз.")
        self._say("  Файли з назвою на «_outline» програма свідомо пропускає.")

    def file_result(self, index: int, total: int, result: Result) -> None:
        name = result.job.name
        prefix = f"  [{index}/{total}] {name}"
        if result.status == STATUS_OK:
            levels = f", рівнів: {result.depth}" if result.depth > 1 else ""
            pages = plural(result.pages, "сторінка", "сторінки", "сторінок")
            bookmarks = plural(result.headings, "закладка", "закладки", "закладок")
            self._say(
                f"{prefix}\n"
                f"      {pages} → {bookmarks}{levels} "
                f"({result.source}, {result.seconds:.1f} с)"
            )
            if self.settings.dry_run:
                self._say(f"      Буде записано: {result.job.target.name} "
                          f"(пробний запуск, файл не створено)")
            else:
                self._say(f"      Записано: {result.job.target.name}")
            if self.settings.show_tree and result.nodes:
                for line in tree.describe(result.nodes, limit=40 if self.settings.verbose else 12):
                    self._say(line)
        elif result.status == STATUS_SKIPPED:
            self._say(f"{prefix}\n      пропущено — {result.message}")
        elif result.status == STATUS_EMPTY:
            self._say(f"{prefix}\n      {result.message}; файл не створено")
            if self.settings.verbose:
                self._say("      Підказка: спробуйте ключ --fallback pages "
                          "або опишіть зміст у файлі *.outline.json")
        else:
            # Помилки йдуть у stderr і не глушаться ключем -q: саме заради них
            # «тихий» режим і потрібен.
            self.error(f"{name}: {result.message}")
        self._say()

    def summary(self, results: list) -> None:
        done = sum(1 for r in results if r.status == STATUS_OK)
        skipped = sum(1 for r in results if r.status == STATUS_SKIPPED)
        empty = sum(1 for r in results if r.status == STATUS_EMPTY)
        failed = sum(1 for r in results if r.status == STATUS_FAILED)
        bookmarks = sum(r.headings for r in results if r.status == STATUS_OK)

        self._say(_LINE)
        verb = "знайдено" if self.settings.dry_run else "оброблено"
        parts = [f"{verb}: {done}"]
        if bookmarks:
            made = "буде створено " if self.settings.dry_run else "створено "
            parts.append(made + plural(bookmarks, "закладку", "закладки", "закладок"))
        if skipped:
            parts.append(f"пропущено: {skipped}")
        if empty:
            parts.append(f"без заголовків: {empty}")
        if failed:
            parts.append(f"помилок: {failed}")
        text = ", ".join(parts)
        self._say("  Готово. " + text[0].upper() + text[1:] + ".")
        self._say(_LINE)
