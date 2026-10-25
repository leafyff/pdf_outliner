"""Обробка одного PDF: від читання до записаного файлу зі змістом."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import tree
from .analyze import analyze
from .config import (
    EXISTING_KEEP,
    EXISTING_REGENERATE,
    EXISTING_SKIP,
    FALLBACK_PAGES,
    Settings,
)
from .detect import detect
from .discovery import Job
from .extract import PdfReadError, load
from .model import count_nodes, max_depth
from .overrides import OverrideError, load_override
from .writer import PdfWriteError, write_outline

#: Стани, у яких може завершитися обробка файлу.
STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"


@dataclass
class Result:
    """Підсумок обробки одного файлу."""

    job: Job
    status: str = STATUS_OK
    message: str = ""
    pages: int = 0
    headings: int = 0
    depth: int = 0
    source: str = ""          # звідки взявся зміст: аналіз, ручний файл, наявний
    nodes: list = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def _document_title(document, job: Job) -> str:
    meta_title = (document.metadata or {}).get("title") or ""
    meta_title = meta_title.strip()
    if 3 <= len(meta_title) <= 120:
        return meta_title
    return job.source.stem.replace("_", " ").strip()


def build_outline(document, settings: Settings, job: Job) -> tuple[list, str]:
    """Повертає дерево змісту та джерело, з якого його отримано."""
    override = load_override(job.source, document.page_count)
    if override:
        return override, "ручний файл"

    if document.existing_toc and settings.existing == EXISTING_KEEP:
        nodes = tree.from_existing_toc(document.existing_toc, document.page_count)
        if nodes:
            return nodes, "наявний зміст"

    analysis = analyze(document.lines, document.page_count)
    candidates = detect(analysis, settings.detection)

    # Запобіжник від «вибуху»: якщо заголовків підозріло багато, лишаємо
    # верхні рівні — краще коротший зміст, ніж сотні сміттєвих пунктів.
    limit = max(10, int(document.page_count * settings.detection.max_headings_per_page))
    if len(candidates) > limit:
        for level in range(settings.detection.max_depth, 1, -1):
            candidates = [c for c in candidates if (c.level or 1) < level]
            if len(candidates) <= limit:
                break

    nodes = tree.build_tree(candidates, settings.detection.max_depth)
    if nodes:
        if settings.add_title_node and len(nodes) > 1:
            nodes = tree.title_node(_document_title(document, job), nodes)
        return nodes, "аналіз тексту"

    if settings.fallback == FALLBACK_PAGES and document.page_count > 1:
        return tree.page_nodes(document.page_count), "посторінково"
    return [], "аналіз тексту"


def process(job: Job, settings: Settings) -> Result:
    """Обробляє один файл і повертає підсумок. Винятків назовні не випускає."""
    started = time.perf_counter()
    result = Result(job=job)

    if settings.skip_existing and job.target.exists():
        result.status = STATUS_SKIPPED
        result.message = "результат уже існує"
        return result

    try:
        document = load(str(job.source))
    except PdfReadError as exc:
        result.status = STATUS_FAILED
        result.message = str(exc)
        result.seconds = time.perf_counter() - started
        return result

    try:
        result.pages = document.page_count
        if document.existing_toc and settings.existing == EXISTING_SKIP:
            result.status = STATUS_SKIPPED
            result.message = f"у файлі вже є зміст ({len(document.existing_toc)} пунктів)"
            return result

        try:
            nodes, source = build_outline(document, settings, job)
        except OverrideError as exc:
            result.status = STATUS_FAILED
            result.message = str(exc)
            return result

        result.nodes = nodes
        result.source = source
        result.headings = count_nodes(nodes)
        result.depth = max_depth(nodes)

        if not nodes:
            result.status = STATUS_EMPTY
            if not document.lines:
                result.message = "у файлі немає текстового шару (скан)"
            else:
                result.message = "заголовків не знайдено"
            return result

        if settings.dry_run:
            result.message = "пробний запуск — файл не записано"
            return result

        try:
            write_outline(
                job.source,
                job.target,
                nodes,
                open_bookmarks=settings.open_bookmarks,
                expand_levels=settings.expand_levels,
                incremental=settings.incremental,
            )
        except PdfWriteError as exc:
            result.status = STATUS_FAILED
            result.message = str(exc)
        return result
    finally:
        document.close()
        result.seconds = time.perf_counter() - started
