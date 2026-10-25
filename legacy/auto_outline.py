#!/usr/bin/env python3
"""
Універсальний утилітарний модуль для додавання структурованого змісту (Outline)
до будь-якого PDF за допомогою pypdf.
"""

import pypdf
from typing import List, Dict, Any, Optional


def embed_outline(
    input_pdf_path: str,
    output_pdf_path: str,
    outline_tree: List[Dict[str, Any]],
    open_bookmarks: bool = True
) -> None:
    """
    Додає закладки до PDF-файлу без перекодування сторінок.

    :param input_pdf_path: шлях до вихідного PDF
    :param output_pdf_path: шлях для збереження результуючого PDF
    :param outline_tree: список вузлів вигляду:
        [
            {
                "title": "# Розділ 1",
                "page": 0,
                "children": [
                    {"title": "## Підрозділ 1.1", "page": 1, "children": [...]},
                ]
            }
        ]
    :param open_bookmarks: якщо True, панель закладок розкривається при відкритті
    """
    reader = pypdf.PdfReader(input_pdf_path)
    writer = pypdf.PdfWriter()

    for page in reader.pages:
        writer.add_page(page)

    def _insert_level(items: List[Dict[str, Any]], parent_ref: Optional[Any] = None):
        for item in items:
            title = item.get("title", "")
            page_idx = int(item.get("page", 0))
            children = item.get("children", [])

            # Додаємо елемент
            ref = writer.add_outline_item(title=title, page_number=page_idx, parent=parent_ref)

            # Рекурсивно додаємо дочірні рівні
            if children:
                _insert_level(children, parent_ref=ref)

    _insert_level(outline_tree)

    if open_bookmarks:
        writer.page_mode = "/UseOutlines"

    with open(output_pdf_path, "wb") as f:
        writer.write(f)


if __name__ == "__main__":
    print("Модуль готовий до імпорту. Використовуйте функцію embed_outline().")
