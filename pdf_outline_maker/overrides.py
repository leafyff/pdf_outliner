"""Ручний зміст: якщо поруч із PDF лежить файл `<ім'я>.outline.json`.

Автоматичний детектор покриває звичайні випадки, але коли структуру треба
задати точно (скан без тексту, нестандартна верстка), простіше описати її
руками. Підтримуються дві форми запису.

Дерево:

    [
      {"title": "Практичне заняття 1", "page": 1, "children": [
        {"title": "Основні означення", "page": 1}
      ]}
    ]

Плаский список із рівнями:

    [
      {"level": 1, "title": "Практичне заняття 1", "page": 1},
      {"level": 2, "title": "Основні означення", "page": 1}
    ]

Номери сторінок — від 1, як їх бачить читач.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import OutlineNode

#: Розширення файлу з ручним змістом: `lab1.pdf` -> `lab1.outline.json`.
OVERRIDE_SUFFIX = ".outline.json"


class OverrideError(ValueError):
    """Файл ручного змісту існує, але прочитати його не вдалося."""


def override_path_for(source: Path) -> Path:
    return source.with_suffix("").with_name(source.stem + OVERRIDE_SUFFIX)


def load_override(source: Path, page_count: int) -> list | None:
    """Читає ручний зміст для вказаного PDF або повертає None, якщо його немає."""
    path = override_path_for(source)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OverrideError(f"{path.name}: {exc}") from exc

    if isinstance(raw, dict):
        raw = raw.get("outline", [])
    if not isinstance(raw, list):
        raise OverrideError(f"{path.name}: очікується список пунктів")

    flat = any(isinstance(item, dict) and "level" in item for item in raw)
    if flat:
        # Змішаний запис мовчки втратив би всіх нащадків, тому кажемо прямо.
        mixed = next((item for item in raw
                      if isinstance(item, dict) and "level" in item and item.get("children")), None)
        if mixed is not None:
            raise OverrideError(
                f"{path.name}: пункт «{mixed.get('title', '?')}» має водночас "
                f"«level» і «children» — оберіть один спосіб запису"
            )
        return _from_flat(raw, page_count, path.name)
    return _from_tree(raw, page_count, path.name)


def _page_index(item: dict, page_count: int, where: str) -> int:
    if "page" not in item:
        raise OverrideError(f"{where}: пункт «{item.get('title', '?')}» без номера сторінки")
    try:
        page = int(item["page"])
    except (TypeError, ValueError) as exc:
        raise OverrideError(f"{where}: номер сторінки має бути числом") from exc
    return min(max(page - 1, 0), max(page_count - 1, 0))


def _title(item: dict, where: str) -> str:
    title = str(item.get("title", "")).strip()
    if not title:
        raise OverrideError(f"{where}: пункт без назви")
    return title


def _from_tree(raw: list, page_count: int, where: str) -> list:
    nodes: list = []
    for item in raw:
        if not isinstance(item, dict):
            raise OverrideError(f"{where}: очікується об'єкт з полями title і page")
        node = OutlineNode(title=_title(item, where), page=_page_index(item, page_count, where))
        children = item.get("children") or []
        if children:
            node.children = _from_tree(children, page_count, where)
        nodes.append(node)
    return nodes


def _from_flat(raw: list, page_count: int, where: str) -> list:
    roots: list = []
    stack: list = []
    for item in raw:
        if not isinstance(item, dict):
            raise OverrideError(f"{where}: очікується об'єкт з полями level, title і page")
        level = max(1, int(item.get("level", 1)))
        node = OutlineNode(title=_title(item, where), page=_page_index(item, page_count, where))
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append(node)
        stack.append((level, node))
    return roots
