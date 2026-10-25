"""Пошук заголовків: вибір «заголовкових» стилів і відсів хибних спрацювань."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .analyze import (
    BARE_ORDINAL,
    CAPTION,
    DOT_LEADER,
    MATH_CHARS,
    MATH_OPENING,
    NUMBERED_TITLE,
    NUMBERING,
    SECTION_WORD,
    SENTENCE_END,
    Analysis,
    StyleStats,
    math_ratio,
)
from .config import DetectionSettings
from .model import Candidate, Line, Style

# Підписи-мітки, з яких у конспектах починається окремий смисловий блок.
RUN_IN_LABEL = re.compile(
    r"^(приклад|задача|вправа|завдання|теорема|лема|означення|визначення|наслідок|"
    r"твердження|властивість|алгоритм|крок|перехід|випадок|розділ|питання|"
    r"example|theorem|lemma|definition|corollary|problem|exercise|step|case)"
    r"\s*[№]?\s*\d{0,3}\s*[.):]?",
    re.I,
)


#: Запобіжник: більше цієї кількості накреслень не розглядаємо в жодному разі.
#: Реальне обмеження — кількість ярусів кегля (DetectionSettings.max_depth).
STYLE_LIMIT = 12


@dataclass
class StyleVerdict:
    """Оцінка стилю як кандидата в рівень змісту."""

    stats: StyleStats
    score: float
    reasons: list


def _size_points(ratio: float) -> tuple[float, str]:
    if ratio >= 1.6:
        return 3.0, "набагато більший кегль"
    if ratio >= 1.3:
        return 2.5, "помітно більший кегль"
    if ratio >= 1.15:
        return 2.0, "більший кегль"
    if ratio >= 1.045:
        return 1.2, "трохи більший кегль"
    if ratio >= 0.97:
        return 0.0, "кегль як в основному тексті"
    return -2.5, "менший кегль"


def score_style(stats: StyleStats, analysis: Analysis, settings: DetectionSettings) -> StyleVerdict:
    """Наскільки стиль схожий на рівень заголовків, а не на основний текст."""
    body = analysis.body
    reasons: list = []
    score = 0.0

    ratio = stats.size / analysis.body_size if analysis.body_size else 1.0
    points, why = _size_points(ratio)
    score += points
    reasons.append(f"{why} (x{ratio:.2f})")

    if ratio > settings.max_size_ratio:
        return StyleVerdict(stats, -10.0, ["аномальний кегль — це не текст"])

    if stats.bold and not (body and body.bold):
        score += 1.6
        reasons.append("напівжирний")
    if body and stats.family and stats.family != body.family:
        score += 0.9
        reasons.append("інша гарнітура")
    if stats.italic and not stats.bold and ratio < 1.15:
        score -= 0.8
        reasons.append("курсив без збільшення")

    share = stats.count / analysis.total_lines if analysis.total_lines else 1.0
    if share > settings.max_style_frequency:
        score -= 6.0
        reasons.append(f"надто поширений стиль ({share:.0%} рядків)")
    elif share > 0.15:
        score -= 2.0
        reasons.append(f"поширений стиль ({share:.0%} рядків)")

    frac_first = stats.fraction(analysis.is_first_on_page)
    if frac_first >= 0.5:
        score += 1.6
        reasons.append(f"починає сторінку ({frac_first:.0%})")
    elif frac_first >= 0.2:
        score += 0.6

    frac_alone = stats.fraction(lambda l: l.block_line_count <= 2)
    score += 1.2 * frac_alone
    if frac_alone >= 0.8:
        reasons.append("стоїть окремим блоком")

    consistency = stats.x_consistency()
    score += 1.4 * consistency - 0.8
    if consistency < 0.5:
        reasons.append("початок рядків «гуляє»")

    purity = stats.purity()
    score += 1.2 * purity - 0.9
    if purity < 0.7:
        reasons.append("стиль займає лише частину рядка")

    spaced = stats.frac_spaced(analysis.body_size)
    score += 1.0 * spaced
    if spaced >= 0.7:
        reasons.append("відступ згори")

    words = stats.median_words()
    if words <= 10:
        score += 0.8
    elif words <= 16:
        score += 0.2
    elif words > 22:
        score -= 1.8
        reasons.append("довгі рядки")

    frac_sentence = stats.fraction(lambda l: bool(SENTENCE_END.search(l.text)) and l.words > 6)
    if frac_sentence > 0.5:
        score -= 1.6
        reasons.append("рядки схожі на речення")

    frac_numbered = stats.fraction(lambda l: bool(NUMBERING.match(l.text)))
    frac_section = stats.fraction(lambda l: bool(SECTION_WORD.match(l.text)))
    if frac_numbered >= 0.5 or frac_section >= 0.5:
        score += 1.0
        reasons.append("нумерація або ключові слова розділів")

    if stats.median_letters() < 3:
        score -= 3.0
        reasons.append("рядки з однієї-двох літер")

    if stats.fraction(lambda l: bool(CAPTION.match(l.text))) > 0.4:
        score -= 2.5
        reasons.append("підписи до рисунків/таблиць")

    # Стиль, що трапився один раз посеред сторінки, — радше службовий напис.
    if stats.count == 1 and frac_first < 0.5 and ratio < 1.5:
        score -= 1.0
        reasons.append("єдиний рядок такого стилю")

    # Повтори однакового тексту (шаблонні написи на кшталт «Означення»).
    uniqueness = stats.uniqueness()
    if stats.count >= 3 and uniqueness <= 0.4:
        score -= 4.0 * (1.0 - uniqueness)
        reasons.append("текст повторюється")

    # Стиль, усі рядки якого лишилися на титульній сторінці й не більші за
    # основний текст, — це вихідні дані (автор, група, назва курсу).
    if stats.pages == 1 and ratio < 1.1 and min(l.page for l in stats.lines) == 0:
        score -= 2.5
        reasons.append("лише на титульній сторінці")

    if stats.fraction(lambda l: l.text.startswith("(") and l.text.endswith(")")) > 0.5:
        score -= 2.0
        reasons.append("текст у дужках — примітка")

    # Ключове правило ярусності: щоб стиль став рівнем змісту, він має
    # відрізнятися від основного тексту хоч чимось, окрім десятої частки пункта.
    distinct = (
        ratio >= 1.15
        or stats.bold != bool(body and body.bold)
        or (body is not None and stats.family != body.family)
    )
    if not distinct:
        score -= 4.0
        reasons.append("майже не відрізняється від основного тексту")

    return StyleVerdict(stats, score, reasons)


def select_heading_styles(
    analysis: Analysis, settings: DetectionSettings
) -> list[StyleVerdict]:
    """Обирає стилі-рівні та впорядковує їх від найвищого рівня до найнижчого."""
    if not analysis.body:
        return []

    verdicts = []
    for stats in analysis.styles.values():
        if stats.style.key() == analysis.body.key():
            continue
        # Стилі, що трапляються тільки на титульній сторінці, — це вихідні
        # дані документа. Вони не мають забирати місце в рівнях змісту.
        if analysis.cover_pages and all(l.page in analysis.cover_pages for l in stats.lines):
            continue
        verdict = score_style(stats, analysis, settings)
        if verdict.score >= settings.score_threshold:
            verdicts.append(verdict)

    # З найпереконливіших лишаємо ті, що вкладаються в дозволену глибину.
    # Рахуємо саме яруси кегля: одне накреслення напівжирним і звичайним — це
    # один рівень, і воно не має з'їдати два з дозволених.
    verdicts.sort(key=lambda v: -v.score)
    verdicts = verdicts[:STYLE_LIMIT]

    # Візуальний ранг: більший кегль — вищий рівень; за рівного кегля жирний вище.
    verdicts.sort(key=lambda v: (-v.stats.size, not v.stats.bold, v.stats.italic))

    kept: list = []
    tiers = 0
    previous_size = None
    for verdict in verdicts:
        if previous_size is None or verdict.stats.size < previous_size - 0.05:
            tiers += 1
            previous_size = verdict.stats.size
        if tiers > settings.max_depth:
            break
        kept.append(verdict)
    return kept


def _levels_by_size(verdicts: list) -> dict:
    """Рівень змісту — це ярус кегля, а не окремий стиль.

    Якщо документ набирає один і той самий ярус двома накресленнями (звичайним
    і напівжирним), це все одно один рівень: інакше вони з'їдали б два рівні
    з дозволеної глибини, а зміст ставав би глибшим, ніж документ насправді.
    """
    level_of: dict = {}
    level = 0
    previous_size = None
    for verdict in verdicts:                      # вже відсортовані за кеглем
        if previous_size is None or verdict.stats.size < previous_size - 0.05:
            level += 1
            previous_size = verdict.stats.size
        level_of[verdict.stats.style.key()] = level
    return level_of


# --------------------------------------------------------------------------
# Відсів окремих рядків
# --------------------------------------------------------------------------

def _is_running_head(line: Line, analysis: Analysis) -> bool:
    repeated = analysis.running_heads
    if not repeated:
        return False
    rel = line.y0 / line.page_height if line.page_height else 0.5
    if 0.12 < rel < 0.86:
        return False
    return re.sub(r"\d+", "#", line.text.casefold()) in repeated


def reject_reason(
    line: Line,
    analysis: Analysis,
    settings: DetectionSettings,
    weak_styles: set = frozenset(),
) -> str | None:
    """Чому цей рядок не може бути заголовком (None — може).

    :param weak_styles: стилі, які не більші за основний текст. Такий стиль
        зазвичай слугує і для виділення всередині абзацу («Приклад 1.»,
        «Теорема 5.»), тому від його рядків вимагаємо номер розділу.
    """
    text = line.text
    if weak_styles and _style_key(line, analysis) in weak_styles:
        if not NUMBERED_TITLE.match(text):
            return "виділення в тексті без номера розділу"
    if len(text) > settings.max_title_chars:
        return "задовгий"
    if line.words > settings.max_title_words:
        return "забагато слів"
    if line.page in analysis.toc_pages and not analysis.is_first_on_page(line):
        return "сторінка надрукованого змісту"
    if DOT_LEADER.search(line.raw_text):
        return "рядок зі змісту (крапковий хвіст)"
    if CAPTION.match(text):
        return "підпис до рисунка/таблиці"
    if _is_running_head(line, analysis):
        return "колонтитул"
    if line.letters < 2 and not BARE_ORDINAL.match(text):
        return "замало тексту"
    if SENTENCE_END.search(text) and line.words > 12 and not NUMBERING.match(text):
        return "схоже на речення"
    if math_ratio(text) > 0.3 and any(ch in MATH_CHARS for ch in text):
        return "формула, а не назва"
    if MATH_OPENING.match(text):
        return "рядок починається з формули"
    return None


#: Крапка, знак оклику чи питання в кінці рядка означають, що назва скінчилася.
_TITLE_END = re.compile(r"[.!?]$")


def _mergeable(a: Line, b: Line) -> bool:
    """Чи є другий рядок продовженням назви, перенесеної з першого."""
    if a.page != b.page:
        return False
    if abs(a.canonical_size - b.canonical_size) > 0.05:
        return False
    if a.style.bold != b.style.bold or a.style.family != b.style.family:
        return False
    if b.y0 - a.y1 > 0.9 * max(a.canonical_size, 1.0):
        return False
    if len(a.text) + len(b.text) > 220:
        return False
    # «2.» на окремому рядку — це номер розділу, назва йде далі.
    if BARE_ORDINAL.match(a.text):
        return True
    return not _TITLE_END.search(a.text)


def _merge_wrapped(lines: list) -> list:
    """Склеює назву, перенесену на кілька рядків.

    Блоки тут навмисно не враховуються: старі PDF (Distiller, doPDF) часто
    кладуть кожен рядок заголовка в окремий блок, тому орієнтуємося на стиль
    і відстань між рядками.
    """
    merged: list = []
    for line in lines:
        if merged and _mergeable(merged[-1], line):
            previous = merged[-1]
            # Назву, розірвану дефісом («Жордана-» + «Гаусса»), склеюємо без
            # пробілу, решту — з пробілом.
            separator = "" if previous.text.endswith(("-", "‑", "–")) else " "
            joined = Line(
                page=previous.page,
                text=f"{previous.text}{separator}{line.text}".strip(),
                raw_text=f"{previous.raw_text}{separator}{line.raw_text}".strip(),
                bbox=(
                    min(previous.x0, line.x0),
                    previous.y0,
                    max(previous.x1, line.x1),
                    line.y1,
                ),
                spans=previous.spans + line.spans,
                block_index=previous.block_index,
                line_index=previous.line_index,
                page_width=previous.page_width,
                page_height=previous.page_height,
                space_above=previous.space_above,
                space_below=line.space_below,
                block_line_count=previous.block_line_count,
                canonical_size=previous.canonical_size,
                canonical_style=previous.canonical_style,
                merged_from=previous.merged_from + 1,
            )
            merged[-1] = joined
        else:
            merged.append(line)
    return merged


def _cover_filter(candidates: list, analysis: Analysis) -> list:
    """Прибирає все, що стоїть на титульній сторінці.

    Назва курсу, група, автор і рік набрані там великим кеглем, але жодним
    розділом не є, тож у змісті лише заважають.
    """
    if not analysis.cover_pages:
        return candidates
    return [c for c in candidates if c.line.page not in analysis.cover_pages]


def _drop_repeated_neighbours(candidates: list) -> list:
    """Beamer та PowerPoint повторюють назву слайда на продовженнях — лишаємо першу."""
    kept: list = []
    for candidate in candidates:
        if kept:
            previous = kept[-1]
            same_title = previous.title.casefold() == candidate.title.casefold()
            if same_title and candidate.line.page - previous.line.page <= 1:
                continue
        kept.append(candidate)
    return kept


def _run_in_candidates(analysis: Analysis, level: int) -> list:
    """Запасний режим: мітки на кшталт «Приклад 3.» на початку абзацу.

    Використовується лише тоді, коли звичайних заголовків майже немає —
    інакше такі мітки засмічують зміст (у конспектах вони трапляються всюди).
    """
    found: list = []
    for line in analysis.lines:
        if line.line_index != 0 or line.block_line_count > 12:
            continue
        leading = next((s for s in line.spans if s.text.strip()), None)
        if leading is None or not leading.bold:
            continue
        match = RUN_IN_LABEL.match(line.text)
        if not match:
            continue
        title = line.text[: match.end()].strip()
        if len(title) < 4:
            continue
        candidate = Candidate(line=line, score=2.0, level=level, title=title,
                              reasons=["мітка на початку абзацу"])
        found.append(candidate)
    return found


def _labels_look_systematic(extra: list, page_count: int) -> bool:
    """Чи справді мітки розмічають документ, а не є звичайними словами в тексті.

    Ознаки справжньої розмітки: міток небагато на сторінку і вони одноманітні
    («Приклад 1.», «Приклад 2.», …). Якщо ж мітки різношерсті й трапляються
    по кілька на сторінку — це проза, а не структура.
    """
    if len(extra) < 3:
        return False
    if page_count and len(extra) > page_count * 1.5:
        return False
    words = Counter(c.title.split()[0].casefold().strip(".:)") for c in extra if c.title.split())
    if not words:
        return False
    return words.most_common(1)[0][1] / len(extra) >= 0.5


def detect(analysis: Analysis, settings: DetectionSettings) -> list:
    """Повертає впорядкований список заголовків із проставленими рівнями."""
    verdicts = select_heading_styles(analysis, settings)
    if not verdicts:
        return _finish(_run_in_candidates(analysis, 1), settings)

    level_of = _levels_by_size(verdicts)
    accepted_keys = set(level_of)
    weak_styles = {
        v.stats.style.key()
        for v in verdicts
        if analysis.body_size and v.stats.size < analysis.body_size * 1.15
    }

    wanted = [l for l in analysis.lines if _style_key(l, analysis) in accepted_keys]
    wanted = _merge_wrapped(wanted)

    candidates: list = []
    for line in wanted:
        key = _style_key(line, analysis)
        if key not in accepted_keys:
            continue
        reason = reject_reason(line, analysis, settings, weak_styles)
        if reason:
            continue
        candidates.append(Candidate(line=line, level=level_of[key], title=line.text))

    candidates = _cover_filter(candidates, analysis)
    candidates.sort(key=lambda c: (c.line.page, c.line.y0))
    candidates = _drop_repeated_neighbours(candidates)

    # Якщо структури на сторінку майже немає — пробуємо мітки на початку абзаців
    # («Приклад 3.», «Теорема (…)»). Там, де звичайні заголовки вже знайшлися,
    # ці мітки лише засмічували б зміст, тому поріг навмисно високий.
    thin = len(candidates) < max(3, analysis.page_count * 0.35)
    if thin:
        extra = _run_in_candidates(analysis, min(len(verdicts) + 1, settings.max_depth))
        extra = [c for c in extra if c.line.page not in analysis.cover_pages]
        if _labels_look_systematic(extra, analysis.page_count):
            candidates = sorted(candidates + extra, key=lambda c: (c.line.page, c.line.y0))
            candidates = _drop_repeated_neighbours(candidates)

    return _finish(candidates, settings)


def _style_key(line: Line, analysis: Analysis) -> tuple:
    if line.canonical_style is not None:
        return line.canonical_style.key()
    style = line.style
    return Style(size=line.canonical_size or style.size, bold=style.bold,
                 italic=style.italic, family=style.family, color=0).key()


def _finish(candidates: list, settings: DetectionSettings) -> list:
    """Прибирає дублікати, нормалізує рівні й обрізає зміст до потрібної глибини."""
    unique: list = []
    seen: set = set()
    for candidate in candidates:
        key = (candidate.title.casefold(), candidate.line.page)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)

    levels = sorted({c.level for c in unique if c.level})
    remap = {level: i + 1 for i, level in enumerate(levels)}
    for candidate in unique:
        candidate.level = remap.get(candidate.level, 1)
    # Обмеження глибини накладає tree.build_tree — там відома реальна
    # вкладеність, а не лише порядковий номер стилю.
    return unique
