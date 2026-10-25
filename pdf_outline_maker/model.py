"""Дані, з якими працює конвеєр: рядок тексту, стиль, заголовок, вузол змісту."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator, Optional

# Прапорці шрифту, які PyMuPDF повертає у span["flags"].
FLAG_SUPERSCRIPT = 1 << 0
FLAG_ITALIC = 1 << 1
FLAG_SERIF = 1 << 2
FLAG_MONOSPACE = 1 << 3
FLAG_BOLD = 1 << 4

_BOLD_NAME = re.compile(r"(bold|black|heavy|semib|demib|extrab|ultrab|[-,_]bd\b)", re.I)
_ITALIC_NAME = re.compile(r"(italic|oblique|[-,_]it\b)", re.I)

# Шрифти, якими TeX, Word та веб-рушії малюють формули: їхній кегль нічого
# не означає, а текст із них — не проза. Увага: сюди не можна вписувати
# текстові гарнітури TeX (SFRM, SFBX, SFSS, SFCC, CMR) — SFCC, наприклад, це
# капітель, якою в статтях набирають саме заголовки.
_MATH_FONT = re.compile(
    r"^(cmmi|cmsy|cmex|cmbsy|cmbxti|msam|msbm|eus[mb]|euf[mb]|eurm|rsfs|"
    r"mtsy|mtex|mtmi|mtextra|esint|wasy|dsrom|bbold|symbol|mathjax|katex|"
    r"(stix|xits|asana|neoeuler|cambria|latinmodern|texgyre\w*)math)",
    re.I,
)


def _base_font_name(font: str) -> str:
    """'ABCDEF+NotoSerif-Bold' -> 'NotoSerif-Bold'."""
    if len(font) > 7 and font[6] == "+":
        font = font[7:]
    return font


def font_family(font: str) -> str:
    """Родина шрифту без накреслення й кегля: 'NotoSerif-Bold' -> 'notoserif'.

    У TeX кегль зашитий в ім'я шрифту (SFRM1095, SFRM1440 — та сама гарнітура
    у двох розмірах), тому кінцеві цифри обов'язково відкидаємо: інакше кожен
    розмір виглядав би як окрема гарнітура.
    """
    name = _base_font_name(font)
    name = re.split(r"[-,]", name, maxsplit=1)[0]
    name = re.sub(r"(MT|PS|PSMT|Regular|Roman)$", "", name)
    name = re.sub(r"[^a-z0-9]", "", name.lower())
    stripped = re.sub(r"\d+$", "", name)
    return stripped or name


@dataclass(frozen=True)
class Style:
    """Візуальний стиль рядка — те, за чим ми відрізняємо заголовок від тексту."""

    size: float          # кегль, заокруглений до 0.1
    bold: bool
    italic: bool
    family: str          # нормалізована родина шрифту
    color: int = 0

    def key(self) -> tuple:
        return (round(self.size, 1), self.bold, self.italic, self.family, self.color)

    def __str__(self) -> str:
        bits = f"{self.size:g}pt {self.family or '?'}"
        if self.bold:
            bits += " bold"
        if self.italic:
            bits += " italic"
        return bits


@dataclass
class Span:
    """Фрагмент тексту з єдиним шрифтом (як його віддає PyMuPDF)."""

    text: str
    font: str
    size: float
    flags: int
    color: int
    bbox: tuple[float, float, float, float]
    is_type3: bool = False

    @property
    def bold(self) -> bool:
        return bool(self.flags & FLAG_BOLD) or bool(_BOLD_NAME.search(_base_font_name(self.font)))

    @property
    def italic(self) -> bool:
        return bool(self.flags & FLAG_ITALIC) or bool(_ITALIC_NAME.search(_base_font_name(self.font)))

    @property
    def is_math_font(self) -> bool:
        return self.is_type3 or bool(_MATH_FONT.match(_base_font_name(self.font)))

    @property
    def family(self) -> str:
        return font_family(self.font)


@dataclass
class Line:
    """Рядок тексту на сторінці разом з усім, що потрібно детектору."""

    page: int                                   # 0-based
    text: str
    bbox: tuple[float, float, float, float]
    spans: list[Span]
    block_index: int
    line_index: int
    page_width: float
    page_height: float
    raw_text: str = ""                          # текст до нормалізації
    space_above: float = 0.0                    # відступ від попереднього рядка сторінки
    space_below: float = 0.0
    block_line_count: int = 1
    canonical_size: float = 0.0                 # кегль після злиття близьких значень
    canonical_style: Optional["Style"] = None   # стиль після злиття близьких кеглів
    merged_from: int = 1                        # скільки рядків злито в цей

    # --- геометрія -----------------------------------------------------
    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def centered(self) -> bool:
        left = self.x0
        right = self.page_width - self.x1
        if right <= 0 or left <= 0:
            return False
        return abs(left - right) < 0.06 * self.page_width and left > 0.12 * self.page_width

    # --- текстові властивості -------------------------------------------
    @property
    def letters(self) -> int:
        return sum(1 for ch in self.text if ch.isalpha())

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def text_spans(self) -> list[Span]:
        """Спани звичайним (не математичним) шрифтом."""
        return [s for s in self.spans if not s.is_math_font and s.text.strip()]

    @property
    def is_math_only(self) -> bool:
        return not self.text_spans

    @property
    def on_page(self) -> bool:
        """Чи лежить рядок у межах сторінки.

        Формульні гліфи (великі дужки KaTeX) інколи мають координати далеко
        за межами аркуша — це вірна ознака службового елемента.
        """
        if self.page_height <= 0 or self.page_width <= 0:
            return True
        return (
            self.y1 > 0
            and self.y0 < self.page_height
            and self.x1 > 0
            and self.x0 < self.page_width
        )

    @property
    def usable(self) -> bool:
        """Чи має сенс узагалі розглядати цей рядок як текст документа."""
        if not self.on_page or self.is_math_only:
            return False
        return self.letters >= 1 or bool(re.match(r"^\d{1,3}\s*[.)]$", self.text))

    def dominant_span(self) -> Optional[Span]:
        """Спан, який визначає стиль рядка — найдовший серед текстових."""
        candidates = self.text_spans or [s for s in self.spans if s.text.strip()]
        if not candidates:
            return None
        return max(candidates, key=lambda s: len(s.text.strip()))

    @property
    def style(self) -> Style:
        span = self.dominant_span()
        if span is None:
            return Style(size=0.0, bold=False, italic=False, family="")
        return Style(
            size=round(span.size, 1),
            bold=span.bold,
            italic=span.italic,
            family=span.family,
            color=span.color,
        )

    @property
    def size(self) -> float:
        return self.style.size

    @property
    def bold_ratio(self) -> float:
        spans = self.text_spans
        total = sum(len(s.text.strip()) for s in spans)
        if not total:
            return 0.0
        return sum(len(s.text.strip()) for s in spans if s.bold) / total


@dataclass
class Candidate:
    """Рядок, який детектор розглядає як заголовок."""

    line: Line
    score: float = 0.0
    level: Optional[int] = None
    reasons: list[str] = field(default_factory=list)
    title: str = ""

    def __post_init__(self) -> None:
        if not self.title:
            self.title = self.line.text

    @property
    def page(self) -> int:
        return self.line.page


@dataclass
class OutlineNode:
    """Вузол готового дерева змісту."""

    title: str
    page: int                                   # 0-based
    y: Optional[float] = None                   # координата для точного переходу
    children: list["OutlineNode"] = field(default_factory=list)
    exact: bool = False                         # y узятий з готового змісту, не зсувати

    def walk(self, level: int = 1) -> Iterator[tuple[int, "OutlineNode"]]:
        yield level, self
        for child in self.children:
            yield from child.walk(level + 1)


def count_nodes(nodes: list[OutlineNode]) -> int:
    return sum(1 for node in nodes for _ in node.walk())


def max_depth(nodes: list[OutlineNode]) -> int:
    depth = 0
    for node in nodes:
        for level, _ in node.walk():
            depth = max(depth, level)
    return depth
