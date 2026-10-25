"""Побудова дерева змісту зі списку знайдених заголовків."""

from __future__ import annotations

from .model import Candidate, OutlineNode, count_nodes
from .text import plural


def build_tree(candidates: list, max_depth: int = 4) -> list:
    """Перетворює плаский список заголовків на ієрархію.

    Рівень кожного заголовка не може перевищувати рівень попереднього більш
    ніж на одиницю: інакше в PDF утворилася б «дірка» (розділ 3-го рівня без
    розділу 2-го), і переглядачі показують таке дерево як зіпсоване.

    `max_depth` обмежує саме глибину готового дерева: пункти, що після такого
    вирівнювання опинилися глибше, у зміст не потрапляють. Важливо рахувати
    глибину вже після вирівнювання — стилів у документі може бути більше, ніж
    реальних рівнів вкладеності.
    """
    roots: list = []
    stack: list = []  # пари (рівень, вузол); вузол None — пункт відкинуто

    for candidate in candidates:
        # Перший пункт завжди стає коренем, навіть якщо його стиль має ранг 3.
        level = max(1, candidate.level or 1)
        level = min(level, stack[-1][0] + 1 if stack else 1)
        while stack and stack[-1][0] >= level:
            stack.pop()

        if level > max_depth:
            # Нащадки такого пункту завжди ще глибші, тож теж відпадуть.
            stack.append((level, None))
            continue

        node = OutlineNode(
            title=candidate.title,
            page=candidate.page,
            y=candidate.line.y0,
        )
        parent = stack[-1][1] if stack else None
        if parent is not None:
            parent.children.append(node)
        else:
            roots.append(node)
        stack.append((level, node))

    return roots


def from_existing_toc(toc: list, page_count: int) -> list:
    """Відновлює дерево з наявного у PDF змісту (див. extract.read_outline).

    Точки переходу переносимо як є: у чужому змісті вони вже вказують на
    потрібні місця, і втрачати їх, зводячи всі закладки до верху сторінки,
    не можна.
    """
    roots: list = []
    stack: list = []
    for entry in toc:
        if len(entry) < 3:
            continue
        level, title, page = max(1, int(entry[0])), str(entry[1]), int(entry[2])
        y = entry[3] if len(entry) > 3 else None
        page_index = min(max(page, 0), max(page_count - 1, 0))
        while stack and stack[-1][0] >= level:
            stack.pop()
        node = OutlineNode(title=title.strip() or "(без назви)", page=page_index,
                           y=y, exact=y is not None)
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append(node)
        stack.append((level, node))
    return roots


def page_nodes(page_count: int, label: str = "Сторінка") -> list:
    """Запасний варіант: по закладці на сторінку."""
    return [OutlineNode(title=f"{label} {i + 1}", page=i) for i in range(page_count)]


def title_node(title: str, children: list) -> list:
    """Загортає дерево в один кореневий вузол з назвою документа."""
    if not children:
        return children
    root = OutlineNode(title=title, page=children[0].page, y=children[0].y)
    root.children = children
    return [root]


def describe(nodes: list, limit: int = 40) -> list:
    """Рядки для друку дерева у консолі."""
    lines: list = []
    total = count_nodes(nodes)
    for node in nodes:
        for level, item in node.walk():
            if len(lines) >= limit:
                rest = plural(total - limit, "пункт", "пункти", "пунктів")
                lines.append(f"    … ще {rest}")
                return lines
            indent = "  " * (level - 1)
            lines.append(f"    {indent}{item.title}  [с. {item.page + 1}]")
    return lines
