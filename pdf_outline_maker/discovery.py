"""Пошук вхідних PDF та побудова імен вихідних файлів."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

#: Суфікс, який програма додає до імені обробленого файлу.
OUTLINE_SUFFIX = "_outline"


def is_output_name(path: Path) -> bool:
    """True для файлів, які вже є результатом роботи програми.

    Такі файли ніколи не беруться в обробку — інакше на другому запуску
    з'явився б «lab1_outline_outline.pdf».
    """
    return path.stem.casefold().endswith(OUTLINE_SUFFIX)


def is_pdf(path: Path) -> bool:
    return path.suffix.casefold() == ".pdf"


def is_junk(path: Path) -> bool:
    """Тимчасові й службові файли, які створюють Word, Explorer тощо."""
    name = path.name
    return name.startswith(("~$", ".", "_~")) or name.casefold().endswith(".crdownload")


def output_path_for(source: Path, output_dir: Path | None = None) -> Path:
    """`lab1.pdf` -> `lab1_outline.pdf` (поруч із вхідним файлом за замовчуванням)."""
    target_dir = output_dir if output_dir is not None else source.parent
    return target_dir / f"{source.stem}{OUTLINE_SUFFIX}.pdf"


@dataclass
class Job:
    """Одна одиниця роботи: звідки читаємо і куди пишемо."""

    source: Path
    target: Path

    @property
    def name(self) -> str:
        return self.source.name


def _iter_pdfs(folder: Path, recursive: bool) -> Iterable[Path]:
    pattern = "**/*" if recursive else "*"
    for path in sorted(folder.glob(pattern), key=lambda p: str(p).casefold()):
        if path.is_file() and is_pdf(path):
            yield path


def collect_jobs(
    input_dir: Path,
    output_dir: Path | None = None,
    recursive: bool = False,
) -> tuple[list[Job], list[tuple[Path, str]]]:
    """Збирає завдання з теки.

    Повертає (завдання, пропущені), де пропущені — це пари (шлях, причина).
    """
    jobs: list[Job] = []
    skipped: list[tuple[Path, str]] = []

    for path in _iter_pdfs(input_dir, recursive):
        if is_junk(path):
            skipped.append((path, "службовий файл"))
            continue
        if is_output_name(path):
            skipped.append((path, f"вже є результатом ({OUTLINE_SUFFIX})"))
            continue
        target = output_path_for(path, output_dir)
        if recursive and output_dir is not None:
            # Зберігаємо структуру підтек при явно заданій теці виходу.
            relative = path.parent.relative_to(input_dir)
            target = output_dir / relative / f"{path.stem}{OUTLINE_SUFFIX}.pdf"
        jobs.append(Job(source=path, target=target))

    return jobs, skipped
