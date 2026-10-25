"""Зміст, узятий із самої розмітки `.md`.

Chromium уміє скласти зміст сам, але за це він будує ще й повне дерево
доступності (tagged PDF), а на великих нотатках із математикою це коштує
хвилини. Тому є другий шлях: PDF друкується звичайним експортом Obsidian,
а зміст будується тут — рівні й назви беруться з `#`-заголовків, сторінки
й координати шукаються за текстом у готовому файлі.

Назви в такому змісті точні за визначенням: вони з джерела, а не з здогадок
про вигляд тексту (цим займається `detect.py` — для чужих PDF, де джерела немає).
"""

from __future__ import annotations

import re
from pathlib import Path

from .model import OutlineNode

#: `## Заголовок` з необов'язковими решітками в кінці.
_ATX = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
#: Огорожа блоку коду: ``` або ~~~ (з будь-якою мовою після).
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
#: Межа блоку метаданих на початку файлу.
_FRONTMATTER = re.compile(r"^---[ \t]*$")

#: Розмітка, яку треба прибрати з назви, щоб текст збігся з надрукованим.
_CLEANERS = (
    (re.compile(r"!?\[\[([^\]|#^]*)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]"), lambda m: m.group(2) or m.group(1)),
    (re.compile(r"!?\[([^\]]*)\]\([^)]*\)"), lambda m: m.group(1)),
    (re.compile(r"`([^`]*)`"), lambda m: m.group(1)),
    (re.compile(r"\*\*([^*]+)\*\*"), lambda m: m.group(1)),
    (re.compile(r"__([^_]+)__"), lambda m: m.group(1)),
    (re.compile(r"\*([^*]+)\*"), lambda m: m.group(1)),
    (re.compile(r"==([^=]+)=="), lambda m: m.group(1)),
    (re.compile(r"~~([^~]+)~~"), lambda m: m.group(1)),
    (re.compile(r"\^\[[^\]]*\]"), lambda m: ""),          # виноска в тексті
    (re.compile(r"\[\^[^\]]*\]"), lambda m: ""),          # посилання на виноску
    (re.compile(r"<[^>]+>"), lambda m: ""),               # HTML-теґи
)

#: Інлайнова математика: у PDF вона набрана іншим шрифтом, шукати її марно.
_MATH = re.compile(r"\$[^$]+\$")


def clean_title(raw: str) -> str:
    """Прибирає розмітку з тексту заголовка: `**Розділ** *1*` -> `Розділ 1`."""
    text = raw.strip()
    for pattern, replace in _CLEANERS:
        text = pattern.sub(replace, text)
    return re.sub(r"\s+", " ", text).strip()


def parse_headings(markdown: str) -> list[tuple[int, str]]:
    """Повертає [(рівень, назва)] у порядку появи, минаючи код і метадані."""
    headings: list[tuple[int, str]] = []
    fence: str | None = None
    lines = markdown.splitlines()
    start = 0
    # Блок метаданих рахується лише тоді, коли він справді відкритий у першому
    # рядку: інакше горизонтальна лінія «---» з'їла б половину файлу.
    if lines and _FRONTMATTER.match(lines[0]):
        for index in range(1, len(lines)):
            if _FRONTMATTER.match(lines[index]):
                start = index + 1
                break
    for line in lines[start:]:
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0] * 3
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        match = _ATX.match(line)
        if match:
            title = clean_title(match.group(2))
            if title:
                headings.append((len(match.group(1)), title))
    return headings


def _searchable(title: str) -> str:
    """Частина назви, яку має сенс шукати в PDF (без формул)."""
    return re.sub(r"\s+", " ", _MATH.sub(" ", title)).strip()


def _find(document, title: str, from_page: int, from_y: float) -> tuple[int, float | None]:
    """Шукає заголовок у PDF, починаючи з указаного місця.

    Пошук послідовний: однакові назви («Висновки» в кожному розділі) мають
    діставатися різним сторінкам, а не одній і тій самій першій.
    """
    needle = _searchable(title)
    if not needle:
        return from_page, None
    # Довгі назви PyMuPDF не знаходить цілком, якщо вони перенесені на два
    # рядки, тож пробуємо ще й початок — його вистачає, щоб влучити у сторінку.
    attempts = [needle]
    if len(needle) > 40:
        attempts.append(needle[:40].rsplit(" ", 1)[0])
    for attempt in attempts:
        for page_number in range(from_page, document.page_count):
            page = document[page_number]
            for rect in page.search_for(attempt, quads=False):
                if page_number == from_page and rect.y0 <= from_y + 1.0:
                    continue
                return page_number, rect.y0
    return from_page, None


def build(document, markdown: str, *, title: str | None = None,
          max_depth: int = 6) -> list[OutlineNode]:
    """Будує дерево змісту для готового PDF за його ж джерелом.

    :param title: назва файлу, якщо її додано першим заголовком
        (пункт «Include name as title» у вікні експорту Obsidian).
    """
    headings = parse_headings(markdown)
    if title:
        # Рівень 0 — вищий за будь-який `#`, тож решта заголовків стає
        # всередині пункту з назвою файлу, а не поруч із ним.
        headings.insert(0, (0, title))
    if not headings:
        return []

    # Рівень «з'їжджає» до фактичної глибини: нотатка, де є лише ## і ###,
    # не повинна давати зміст, що починається з другого рівня.
    levels = sorted({level for level, _ in headings})
    depth_of = {level: index + 1 for index, level in enumerate(levels)}

    roots: list[OutlineNode] = []
    stack: list[tuple[int, OutlineNode]] = []
    page, y = 0, -1.0
    for level, text in headings:
        depth = depth_of[level]
        if depth > max_depth:
            continue
        page, found_y = _find(document, text, page, y)
        y = found_y if found_y is not None else y
        node = OutlineNode(title=text, page=page, y=found_y)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append(node)
        stack.append((depth, node))
    return roots


def build_for_file(document, source: Path, *, title: str | None = None,
                   max_depth: int = 6) -> list[OutlineNode]:
    """Те саме, але читає розмітку з файлу."""
    return build(document, source.read_text(encoding="utf-8"),
                 title=title, max_depth=max_depth)
