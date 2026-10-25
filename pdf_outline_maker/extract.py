"""Витягування тексту з PDF разом з типографічною інформацією."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import pymupdf

from .model import Line, Span

# get_text("dict") без зображень, зі збереженням лігатур та пробілів.
_TEXT_FLAGS = (
    pymupdf.TEXT_PRESERVE_LIGATURES
    | pymupdf.TEXT_PRESERVE_WHITESPACE
    | pymupdf.TEXT_MEDIABOX_CLIP
    | pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE
)

_WS = re.compile(r"[\s  -​  　]+")
_ZERO_WIDTH = re.compile(r"[​-‏‪-‮⁠﻿­]")
_DOT_LEADER = re.compile(r"\s*[.·•‧…]{4,}\s*\d*$")


class PdfReadError(RuntimeError):
    """PDF не вдалося прочитати (пошкоджений, зашифрований, зайнятий)."""


def normalize_text(raw: str) -> str:
    """Зводить рядок до охайного вигляду, придатного для назви закладки."""
    text = unicodedata.normalize("NFC", raw)
    text = _ZERO_WIDTH.sub("", text)
    text = "".join(ch for ch in text if ch >= " " or ch == "\t")
    text = _WS.sub(" ", text).strip()
    # Обрізаємо крапкові «хвости» зі змістових сторінок: «Розділ 1 ....... 12».
    text = _DOT_LEADER.sub("", text).strip()
    return text


@dataclass
class PageInfo:
    index: int
    width: float
    height: float
    rotation: int
    derotation: "pymupdf.Matrix"
    has_text: bool = False


@dataclass
class Document:
    """Усе, що конвеєр знає про відкритий PDF."""

    path: str
    doc: "pymupdf.Document"
    lines: list = field(default_factory=list)
    pages: list = field(default_factory=list)
    existing_toc: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return self.doc.page_count

    @property
    def text_page_count(self) -> int:
        return sum(1 for p in self.pages if p.has_text)

    @property
    def producer(self) -> str:
        meta = self.metadata or {}
        return f"{meta.get('producer') or ''} {meta.get('creator') or ''}".strip()

    def close(self) -> None:
        try:
            self.doc.close()
        except Exception:  # noqa: BLE001 - закриття не повинно валити програму
            pass


def open_document(path: str) -> "pymupdf.Document":
    """Відкриває PDF, розшифровує порожнім паролем, якщо це можливо."""
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001 - MuPDF кидає різні типи
        raise PdfReadError(f"не вдалося відкрити файл: {exc}") from exc

    if doc.is_encrypted and not doc.authenticate(""):
        doc.close()
        raise PdfReadError("файл захищено паролем")
    if doc.page_count == 0:
        doc.close()
        raise PdfReadError("у файлі немає сторінок")
    return doc


def _make_span(raw: dict) -> Span:
    font = raw.get("font") or ""
    return Span(
        text=raw.get("text", ""),
        font=font,
        size=float(raw.get("size", 0.0)),
        flags=int(raw.get("flags", 0)),
        color=int(raw.get("color", 0)),
        bbox=tuple(raw.get("bbox", (0.0, 0.0, 0.0, 0.0))),
        is_type3=font.startswith("Type3"),
    )


def _page_lines(page, info: PageInfo) -> list:
    data = page.get_text("dict", flags=_TEXT_FLAGS)
    # Координати тексту живуть у просторі *неповернутої* сторінки, а page.rect
    # для сторінки з /Rotate 90 показує вже повернутий прямокутник. Беремо
    # розміри звідти ж, звідки й координати, — інакше на повернутих сторінках
    # половина рядків опиняється «за межами аркуша» і мовчки зникає.
    info.width = float(data.get("width") or info.width)
    info.height = float(data.get("height") or info.height)
    lines: list = []
    for b_idx, block in enumerate(data.get("blocks", [])):
        if block.get("type") != 0:
            continue
        kept: list = []
        for l_idx, raw_line in enumerate(block.get("lines", [])):
            # Пробіли між словами PDF часто зберігає окремими спанами —
            # їх не можна відкидати, інакше слова злипнуться.
            spans = [_make_span(s) for s in raw_line.get("spans", []) if s.get("text")]
            if not any(s.text.strip() for s in spans):
                continue
            raw = "".join(s.text for s in spans)
            text = normalize_text(raw)
            if not text:
                continue
            kept.append(
                Line(
                    page=info.index,
                    text=text,
                    raw_text=raw.strip(),
                    bbox=tuple(raw_line.get("bbox", (0.0, 0.0, 0.0, 0.0))),
                    spans=spans,
                    block_index=b_idx,
                    line_index=l_idx,
                    page_width=info.width,
                    page_height=info.height,
                )
            )
        for line in kept:
            line.block_line_count = len(kept)
        lines.extend(kept)

    lines.sort(key=lambda l: (round(l.y0, 1), l.x0))
    for i, line in enumerate(lines):
        line.space_above = max(0.0, line.y0 - lines[i - 1].y1) if i else line.y0
        if i + 1 < len(lines):
            line.space_below = max(0.0, lines[i + 1].y0 - line.y1)
        else:
            line.space_below = max(0.0, info.height - line.y1)
    return lines


def load(path: str, max_pages: Optional[int] = None) -> Document:
    """Читає PDF і повертає всі текстові рядки з типографічними ознаками."""
    doc = open_document(path)
    pages: list = []
    all_lines: list = []
    limit = doc.page_count if max_pages is None else min(doc.page_count, max_pages)

    for index in range(limit):
        try:
            page = doc[index]
            rect = page.rect
            info = PageInfo(
                index=index,
                width=rect.width,
                height=rect.height,
                rotation=page.rotation,
                derotation=page.derotation_matrix,
            )
            lines = _page_lines(page, info)
        except Exception:  # noqa: BLE001 - зламана сторінка не має валити весь файл
            info = PageInfo(index=index, width=595.0, height=842.0, rotation=0,
                            derotation=pymupdf.Identity)
            lines = []
        info.has_text = bool(lines)
        pages.append(info)
        all_lines.extend(lines)

    existing_toc = read_outline(doc)

    document = Document(
        path=path,
        doc=doc,
        lines=all_lines,
        pages=pages,
        existing_toc=existing_toc,
        metadata=doc.metadata or {},
    )
    return document


def read_outline(doc) -> list:
    """Наявний у PDF зміст у вигляді [рівень, назва, сторінка(0-based), y].

    Координату переводимо в ту саму систему, якою користується детектор
    (відлік від верху видимої області), щоб під час запису вона пройшла той
    самий перерахунок і закладка лишилася там, де була.
    """
    try:
        raw = doc.get_toc(simple=False) or []
    except Exception:  # noqa: BLE001 - зміст може бути пошкоджений
        return []

    entries: list = []
    for item in raw:
        if len(item) < 3:
            continue
        level, title, page = int(item[0]), str(item[1]), int(item[2])
        y = None
        dest = item[3] if len(item) > 3 and isinstance(item[3], dict) else None
        point = dest.get("to") if dest else None
        if point is not None and 0 < page <= doc.page_count:
            try:
                pdf_page = doc[page - 1]
                crop, media = pdf_page.cropbox, pdf_page.mediabox
                y = float(point.y) + (media.height - crop.y1)
            except Exception:  # noqa: BLE001
                y = None
        entries.append([level, title, max(page - 1, 0), y])
    return entries


def size_histogram(lines: list) -> Counter:
    """Розподіл кеглів, зважений кількістю літер (математику ігноруємо)."""
    hist: Counter = Counter()
    for line in lines:
        if line.is_math_only:
            continue
        if line.letters:
            hist[round(line.size, 1)] += line.letters
    return hist
