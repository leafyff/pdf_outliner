"""Запис готового змісту у PDF без спотворення вмісту сторінок."""

from __future__ import annotations

import gc
import os
import shutil
from pathlib import Path
from typing import Optional

import pymupdf

from .model import OutlineNode

#: Невеликий відступ над заголовком, щоб після переходу він не прилипав до краю.
_DEST_MARGIN = 8.0


class PdfWriteError(RuntimeError):
    """Не вдалося записати результат (файл зайнятий, немає прав тощо)."""


def _dest(doc: "pymupdf.Document", page: int, y: Optional[float],
          exact: bool = False) -> dict:
    """Точка переходу для закладки.

    Координати рядків PyMuPDF відлічує від верху *видимої* області (CropBox),
    а призначення в PDF записується відносно аркуша (MediaBox). Якщо сторінка
    обрізана, різницю треба компенсувати — інакше закладка з'їжджає на сотні
    пунктів (на скан-книжках це півсторінки).

    На сторінках, повернутих на 90/180/270 градусів, координату навмисно не
    вказуємо: узгодити систему координат тексту з системою координат
    призначення там не вдається надійно для всіх кутів, а закладка, що веде
    не туди, гірша за закладку на початок сторінки.
    """
    dest = {"kind": pymupdf.LINK_GOTO, "page": page, "zoom": 0.0}
    if y is None:
        return dest
    try:
        pdf_page = doc[page]
        if pdf_page.rotation:
            return dest
        crop, media = pdf_page.cropbox, pdf_page.mediabox
        visible = max(crop.height - 1.0, 0.0)
        margin = 0.0 if exact else _DEST_MARGIN
        target = min(max(y - margin, 0.0), visible)
        dest["to"] = pymupdf.Point(crop.x0, target - (media.height - crop.y1))
    except Exception:  # noqa: BLE001 - без точної координати закладка теж працює
        return {"kind": pymupdf.LINK_GOTO, "page": page, "zoom": 0.0}
    return dest


def build_toc(doc: "pymupdf.Document", nodes: list) -> list:
    """Перетворює дерево у формат PyMuPDF: [рівень, назва, сторінка(1-based), dest]."""
    toc: list = []
    last_page = doc.page_count - 1

    def walk(node: OutlineNode, level: int) -> None:
        page = min(max(node.page, 0), last_page)
        title = (node.title or "").strip() or "(без назви)"
        toc.append([level, title, page + 1, _dest(doc, page, node.y, node.exact)])
        for child in node.children:
            walk(child, level + 1)

    for node in nodes:
        walk(node, 1)
    return toc


def write_outline(
    source: Path,
    target: Path,
    nodes: list,
    *,
    open_bookmarks: bool = True,
    expand_levels: Optional[int] = None,
    incremental: bool = True,
) -> int:
    """Створює `target` — копію `source` з доданим змістом.

    :param expand_levels: до якого рівня розгортати панель закладок
        (None — розгорнути всі рівні).
    :param incremental: дописувати зміст у кінець файлу, не переписуючи його.
    :return: кількість записаних закладок.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".part")
    spare = target.with_name(target.name + ".part2")
    _cleanup(temp, spare)

    collapse = expand_levels  # семантика PyMuPDF: None — усе розгорнуто
    written = 0
    try:
        if incremental:
            shutil.copy2(source, temp)
            doc = pymupdf.open(temp)
        else:
            doc = pymupdf.open(source)

        saved_to = temp
        try:
            if doc.is_encrypted:
                doc.authenticate("")
            toc = build_toc(doc, nodes)
            written = doc.set_toc(toc, collapse=collapse)
            if open_bookmarks and toc:
                doc.set_pagemode("UseOutlines")

            if incremental:
                try:
                    doc.save(str(temp), incremental=True,
                             encryption=pymupdf.PDF_ENCRYPT_KEEP)
                except Exception:  # noqa: BLE001 - файл не приймає дозапис
                    # Повний запис мусить іти в інший файл: MuPDF не переписує
                    # той самий файл, з якого читає. Шифрування та права
                    # доступу зберігаємо — інакше з захищеного PDF вийшла б
                    # непомітно «відкрита» копія.
                    doc.save(str(spare), garbage=0, deflate=False, clean=False,
                             encryption=pymupdf.PDF_ENCRYPT_KEEP)
                    saved_to = spare
            else:
                doc.save(str(temp), garbage=0, deflate=False, clean=False,
                         encryption=pymupdf.PDF_ENCRYPT_KEEP)
        finally:
            doc.close()
            del doc

        if saved_to is spare:
            os.replace(spare, temp)
        os.replace(temp, target)
    except PermissionError as exc:
        _cleanup(temp, spare)
        raise PdfWriteError(
            f"немає доступу до «{target.name}» — можливо, файл відкритий у переглядачі"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - будь-яка помилка MuPDF/ФС
        _cleanup(temp, spare)
        raise PdfWriteError(f"не вдалося записати «{target.name}»: {exc}") from exc

    return written


def _cleanup(*paths: Path) -> None:
    """Прибирає тимчасові файли. Windows інколи ще тримає щойно закритий файл."""
    collected = False
    for path in paths:
        for attempt in range(2):
            try:
                if path.exists():
                    path.unlink()
                break
            except OSError:
                if collected or attempt:
                    break
                gc.collect()
                collected = True
