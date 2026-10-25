"""Типографічний аналіз документа: стилі, основний текст, службові зони.

Головна ідея: заголовок — це не «великий шрифт», а *окремий стиль*, яким
набрано мало рядків і які поводяться як заголовки (короткі, стоять окремо,
часто починають сторінку). Тому спершу будуємо таблицю стилів документа,
а вже потім вирішуємо, які з них є рівнями змісту.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .model import Line, Style

# Рядок, що починається з номера: «1.», «2.3.», «IV.», «3)».
NUMBERING = re.compile(
    r"^\s*(?:\d{1,3}(?:[.:]\d{1,3}){0,3}\.?|[IVXLCDM]{1,6}\.|[A-ZА-ЯІЇЄҐ]\.|\d{1,3}[)\]])(?=\s|$)"
)
# Ключові слова, з яких зазвичай починається справжній розділ.
SECTION_WORD = re.compile(
    r"^\s*(розділ|глава|частина|тема|лекці|практичн|лаборатор|заняття|семінар|"
    r"додаток|вступ|висновк|література|літератури|бібліографія|зміст|перелік|"
    r"завдання|домашнє|приклад|задача|вправ|перехід|крок|питання|"
    r"chapter|section|part|appendix|introduction|conclusion|references|"
    r"bibliography|contents|abstract|summary|lecture|exercise|example|problem)\b",
    re.I,
)
# Підписи до рисунків і таблиць — схожі на заголовки, але ними не є.
CAPTION = re.compile(
    r"^\s*(рис|рисунок|мал|малюнок|табл|таблиця|схема|діаграма|графік|"
    r"fig|figure|table|listing|chart)\b\.?\s*\d",
    re.I,
)
# Крапкові «хвости» змістової сторінки: «Розділ 1 ......... 12».
DOT_LEADER = re.compile(r"[.·•‧]{4,}\s*\d{1,4}\s*$")
SENTENCE_END = re.compile(r"[.!?;,:]$")
# Назва з ієрархічним номером: «3. Вступ», «2.1. Основні факти».
NUMBERED_TITLE = re.compile(r"^\s*\d{1,3}(?:\.\d{1,3})*\.?\s+\S")
# Рядок-порядковий номер: «1.», «2)» — трапляється як заголовок задачі.
BARE_ORDINAL = re.compile(r"^\d{1,3}\s*[.)]$")
# Рядок, що починається зі змінної з відношенням: «x4,5 >= 0», «y = f(x)».
MATH_OPENING = re.compile(r"^[A-Za-zА-Яа-яα-ωΑ-Ω][\d,\s]{0,8}[=≤≥<>≠∈∉→⟹]")
# Знаки, якими записують формули просто текстовим шрифтом.
MATH_CHARS = set("=<>≤≥≠≈±+*/^_|∑∏∫∈∉⊂⊆∪∩→⟹⟶∼≡∀∃∅∞·×÷√{}[]()")


def math_ratio(text: str) -> float:
    """Частка цифр і математичних знаків серед непробільних символів."""
    body = [ch for ch in text if not ch.isspace()]
    if not body:
        return 0.0
    hits = sum(1 for ch in body if ch.isdigit() or ch in MATH_CHARS)
    return hits / len(body)


def cluster_sizes(sizes: list[float]) -> dict:
    """Зводить близькі кеглі до спільного значення (20.0, 20.1, 20.2 -> 20.1).

    PDF-генератори часто дають той самий візуальний рівень з різницею 0.1-0.3pt.
    """
    mapping: dict = {}
    if not sizes:
        return mapping
    ordered = sorted(set(round(s, 1) for s in sizes))
    group: list[float] = [ordered[0]]
    for size in ordered[1:]:
        tolerance = max(0.35, group[-1] * 0.02)
        if size - group[-1] <= tolerance:
            group.append(size)
        else:
            centre = round(statistics.median(group), 1)
            mapping.update({s: centre for s in group})
            group = [size]
    centre = round(statistics.median(group), 1)
    mapping.update({s: centre for s in group})
    return mapping


@dataclass
class StyleStats:
    """Статистика одного стилю по всьому документу."""

    style: Style
    lines: list = field(default_factory=list)

    size: float = 0.0
    bold: bool = False
    italic: bool = False
    family: str = ""

    @property
    def count(self) -> int:
        return len(self.lines)

    @property
    def letters(self) -> int:
        return sum(l.letters for l in self.lines)

    @property
    def pages(self) -> int:
        return len({l.page for l in self.lines})

    def fraction(self, predicate) -> float:
        if not self.lines:
            return 0.0
        return sum(1 for l in self.lines if predicate(l)) / len(self.lines)

    def median_words(self) -> float:
        if not self.lines:
            return 0.0
        return statistics.median(l.words for l in self.lines)

    def x_consistency(self) -> float:
        """Наскільки однаково вирівняні рядки стилю — по лівому краю або по центру.

        Рівень заголовків завжди вирівняний однаково, а напівжирне виділення
        всередині абзацу починається де завгодно.
        """
        if len(self.lines) < 3:
            return 1.0
        best = 0.0
        for values in (
            [l.x0 for l in self.lines],
            [(l.x0 + l.x1) / 2 for l in self.lines],
        ):
            centre = statistics.median(values)
            close = sum(1 for v in values if abs(v - centre) <= 3.0)
            best = max(best, close / len(values))
        return best

    def median_letters(self) -> float:
        if not self.lines:
            return 0.0
        return statistics.median(l.letters for l in self.lines)

    def uniqueness(self) -> float:
        """Частка унікальних текстів. Шаблонні написи повторюються дослівно."""
        if not self.lines:
            return 1.0
        texts = [l.text.casefold() for l in self.lines]
        return len(set(texts)) / len(texts)

    def frac_spaced(self, body_size: float) -> float:
        """Частка рядків, над якими є помітний додатковий відступ."""
        if not self.lines or body_size <= 0:
            return 0.0
        return sum(1 for l in self.lines if l.space_above > body_size * 1.1) / len(self.lines)

    def purity(self) -> float:
        """Наскільки рядки цього стилю набрані ним цілком.

        Заголовок набраний одним стилем від початку до кінця; виділення
        всередині речення — ні.
        """
        if not self.lines:
            return 0.0
        total = 0.0
        for line in self.lines:
            chars = sum(len(s.text.strip()) for s in line.text_spans)
            if not chars:
                continue
            dominant = line.dominant_span()
            same = sum(
                len(s.text.strip())
                for s in line.text_spans
                if dominant is not None
                and s.bold == dominant.bold
                and s.italic == dominant.italic
                and abs(s.size - dominant.size) < 0.35
            )
            total += same / chars
        return total / len(self.lines)


@dataclass
class Analysis:
    """Результат типографічного аналізу документа."""

    lines: list
    page_count: int
    styles: dict = field(default_factory=dict)      # ключ стилю -> StyleStats
    body: Style | None = None
    body_size: float = 0.0
    first_on_page: set = field(default_factory=set)  # id(line)
    toc_pages: set = field(default_factory=set)
    running_heads: set = field(default_factory=set)
    cover_pages: set = field(default_factory=set)
    total_lines: int = 0

    def is_first_on_page(self, line: Line) -> bool:
        return id(line) in self.first_on_page


def _canonical_style(line: Line, size_map: dict) -> Style:
    """Стиль рядка після злиття близьких кеглів — саме ним оперує детектор."""
    style = line.style
    return Style(
        size=size_map.get(round(style.size, 1), round(style.size, 1)),
        bold=style.bold,
        italic=style.italic,
        family=style.family,
        color=0,  # колір навмисно ігноруємо: він надто часто «шумить»
    )


def _find_running_heads(lines: list, page_count: int, body_size: float) -> set:
    """Колонтитули: однаковий напис угорі або внизу багатьох сторінок.

    Номер сторінки в такому написі змінюється («Сторінка 3 з 12»), тому цифри
    замінюємо на «#». Але саме через це під правило потрапляли б і справжні
    заголовки виду «Практичне заняття 1», «Практичне заняття 2», якщо кожен
    починає свою сторінку. Рятує кегль: колонтитул ніколи не буває більшим за
    основний текст, а заголовок — майже завжди.
    """
    if page_count < 4:
        return set()
    buckets: dict = defaultdict(set)
    for line in lines:
        size = line.canonical_size or line.size
        if body_size and size > body_size * 1.05:
            continue
        rel = line.y0 / line.page_height if line.page_height else 0.0
        if 0.12 < rel < 0.86:
            continue
        key = (re.sub(r"\d+", "#", line.text.casefold()), int(rel * 20))
        buckets[key].add(line.page)
    threshold = max(3, int(page_count * 0.25))
    return {key[0] for key, pages in buckets.items() if len(pages) >= threshold}


def _detect_toc_pages(lines: list, page_count: int) -> set:
    """Сторінки з надрукованим змістом: там багато рядків з крапковими хвостами."""
    per_page: Counter = Counter()
    for line in lines:
        if DOT_LEADER.search(line.raw_text):
            per_page[line.page] += 1
    return {page for page, n in per_page.items() if n >= 4}


def _detect_cover_pages(lines: list, body: Style | None, page_count: int) -> set:
    """Титульні сторінки та обкладинки.

    Ознака — суцільна відсутність основного тексту: на титулці немає прози,
    лише назва, автори та вихідні дані, набрані «великими» стилями. Саме вони
    інакше масово потрапляють у зміст як хибні заголовки.
    """
    if body is None:
        return set()
    by_page: dict = defaultdict(list)
    for line in lines:
        by_page[line.page].append(line)

    covers = set()
    for page in range(min(2, page_count)):
        page_lines = by_page.get(page, [])
        if not page_lines:
            continue
        letters = sum(l.letters for l in page_lines)
        if letters > 1600:
            continue
        # Драматично великий напис на початку — це титулка, а не розділ.
        if body.size and max(l.canonical_size or l.size for l in page_lines) >= body.size * 2.5:
            covers.add(page)
            continue
        body_letters = sum(
            l.letters for l in page_lines if (l.canonical_style or l.style).key() == body.key()
        )
        if letters and body_letters / letters < 0.15:
            covers.add(page)
    return covers


def analyze(lines: list, page_count: int) -> Analysis:
    """Будує таблицю стилів і визначає основний текст документа."""
    usable = [l for l in lines if l.usable]
    size_map = cluster_sizes([l.size for l in usable])

    styles: dict = {}
    for line in usable:
        style = _canonical_style(line, size_map)
        line.canonical_size = style.size
        line.canonical_style = style
        stats = styles.get(style.key())
        if stats is None:
            stats = StyleStats(style=style, size=style.size, bold=style.bold,
                               italic=style.italic, family=style.family)
            styles[style.key()] = stats
        stats.lines.append(line)

    body = None
    if styles:
        # Основний текст — стиль з найбільшою кількістю літер.
        body_stats = max(styles.values(), key=lambda s: (s.letters, s.count))
        body = body_stats.style

    first_on_page: set = set()
    by_page: dict = defaultdict(list)
    for line in usable:
        by_page[line.page].append(line)
    for page_lines in by_page.values():
        top = min(page_lines, key=lambda l: (round(l.y0, 1), l.x0))
        first_on_page.add(id(top))

    body_size = body.size if body else 0.0
    analysis = Analysis(
        lines=usable,
        page_count=page_count,
        styles=styles,
        body=body,
        body_size=body_size,
        first_on_page=first_on_page,
        total_lines=len(usable),
    )
    analysis.toc_pages = _detect_toc_pages(usable, page_count)
    analysis.running_heads = _find_running_heads(usable, page_count, body_size)
    if body_size:
        analysis.cover_pages = _detect_cover_pages(usable, body, page_count)
    return analysis
