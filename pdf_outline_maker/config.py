"""Налаштування програми: значення за замовчуванням, config.json, ключі CLI."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

#: Як чинити з PDF, у якому зміст уже є.
EXISTING_KEEP = "keep"            # перенести наявний зміст як є
EXISTING_REGENERATE = "regenerate"  # побудувати новий з нуля
EXISTING_SKIP = "skip"            # не чіпати такий файл узагалі
EXISTING_CHOICES = (EXISTING_KEEP, EXISTING_REGENERATE, EXISTING_SKIP)

#: Що робити, коли заголовків не знайдено.
FALLBACK_NONE = "none"
FALLBACK_PAGES = "pages"
FALLBACK_CHOICES = (FALLBACK_NONE, FALLBACK_PAGES)

#: Формати аркуша, які розуміє експорт Obsidian (ті самі, що в його вікні).
PAGE_SIZES = ("A3", "A4", "A5", "Legal", "Letter", "Tabloid")

#: Поля сторінки у вікні експорту Obsidian.
MARGIN_CHOICES = ("default", "minimal", "none")

#: Хто будує зміст у перетвореному з .md файлі.
OUTLINE_MD = "md"              # ми самі: рівні й назви з розмітки, сторінки — пошуком
OUTLINE_CHROMIUM = "chromium"  # Chromium під час друку (потребує tagged PDF)
OUTLINE_CHOICES = (OUTLINE_MD, OUTLINE_CHROMIUM)


def _coerce(key: str, value: Any, current: Any) -> Any:
    """Приводить значення з config.json до типу параметра.

    JSON легко написати з помилкою («"max_depth": "3"»), і без перевірки така
    дрібниця вилазить незрозумілим збоєм уже посеред обробки файлу.
    """
    if current is None or value is None:
        return value
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        raise ValueError(f"{key}: очікується true або false, а не {value!r}")
    if isinstance(current, int) and not isinstance(value, bool):
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ValueError(f"{key}: очікується ціле число, а не {value!r}")
    if isinstance(current, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise ValueError(f"{key}: очікується число, а не {value!r}")
    if isinstance(current, str) and not isinstance(value, str):
        raise ValueError(f"{key}: очікується рядок, а не {value!r}")
    return value


@dataclass
class DetectionSettings:
    """Пороги детектора заголовків.

    Значення підібрані на реальних документах (конспекти з Obsidian, статті
    LaTeX, слайди Beamer/PowerPoint, звіти Word). Змінювати варто лише тоді,
    коли на конкретній добірці видно систематичну помилку.
    """

    #: Мінімальне перевищення кегля над основним текстом, щоб рядок вважався
    #: кандидатом лише за розміром.
    size_ratio: float = 1.08
    #: Кегль, більший за основний у стільки разів, — майже напевно артефакт
    #: (велика дужка формули, декоративний елемент). Титульні написи на кшталт
    #: 72pt «Звіт» при 16pt тексту сюди ще вміщаються.
    max_size_ratio: float = 6.0
    #: Скільки балів потрібно набрати рядку, щоб стати заголовком.
    score_threshold: float = 3.5
    #: Довші рядки заголовками не вважаємо.
    max_title_chars: int = 200
    max_title_words: int = 25
    #: Стиль, яким набрано більше цієї частки рядків, — це основний текст.
    max_style_frequency: float = 0.30
    #: Максимальна кількість рівнів у змісті.
    max_depth: int = 4
    #: Захист від «вибуху»: не більше стількох заголовків на сторінку.
    max_headings_per_page: float = 6.0
    #: Мінімальна кількість заголовків, щоб результат вважався осмисленим.
    min_headings: int = 2
    #: Об'єднувати сусідні рядки одного стилю в один заголовок (перенос назви).
    merge_wrapped: bool = True


@dataclass
class ExportSettings:
    """Перетворювач md -> PDF (export.bat): що передати експортові Obsidian."""

    #: Сховище Obsidian. None — взяти те, яке відкрите (чи останнє відкрите).
    vault: str | None = None
    #: Тека, куди користувач кладе .md.
    input_dir: Path = Path("md")
    #: Куди складати PDF. None — поруч із вхідним файлом.
    output_dir: Path | None = None
    page_size: str = "A4"
    #: Додати назву файлу першим заголовком (пункт «Include name as title»).
    include_name: bool = True
    #: Масштаб у відсотках (повзунок «Downscale percent»).
    downscale: int = 100
    landscape: bool = False
    margin: str = "default"
    #: Хто будує зміст. "md" — ми (швидко й без роздування файлу);
    #: "chromium" — сам друкар, але тоді PDF виходить тегований: на великих
    #: нотатках із математикою це хвилини роботи й файл у рази більший.
    outline: str = OUTLINE_MD
    #: Скільки секунд чекати на відповідь Obsidian.
    timeout: float = 180.0
    #: Відкрити готовий PDF у переглядачі.
    open_result: bool = False


@dataclass
class Settings:
    """Повна конфігурація запуску."""

    input_dir: Path = Path("data")
    output_dir: Path | None = None
    recursive: bool = False
    existing: str = EXISTING_KEEP
    fallback: str = FALLBACK_NONE
    #: Загорнути знайдені розділи в один кореневий вузол з назвою документа.
    #: Типово вимкнено: програма не вигадує структуру, якої немає у файлі.
    add_title_node: bool = False
    #: Розгортати панель закладок при відкритті файлу.
    open_bookmarks: bool = True
    #: До якого рівня розгортати дерево (None — усе).
    expand_levels: int | None = None
    #: Дозапис у кінець файлу замість повного перезапису.
    incremental: bool = True
    #: Не переписувати вже створені `*_outline.pdf`.
    skip_existing: bool = False
    #: Показати, що буде зроблено, без запису файлів.
    dry_run: bool = False
    #: Друкувати дерево змісту для кожного файлу.
    show_tree: bool = True
    verbose: bool = False
    quiet: bool = False
    detection: DetectionSettings = field(default_factory=DetectionSettings)
    export: ExportSettings = field(default_factory=ExportSettings)

    # --- завантаження / збереження --------------------------------------
    @classmethod
    def load(cls, path: Path | None) -> "Settings":
        """Читає config.json, якщо він існує; інакше повертає типові значення."""
        settings = cls()
        if path is None or not path.exists():
            return settings
        try:
            # utf-8-sig: редактори Windows (і PowerShell) люблять додавати BOM,
            # а json.loads на ньому падає.
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"не вдалося прочитати {path.name}: {exc}") from exc
        settings.apply(raw)
        return settings

    def apply(self, raw: dict[str, Any]) -> None:
        """Накладає словник значень (з config.json) на поточні налаштування."""
        known = {f.name for f in fields(self)}
        for key, value in raw.items():
            if key.startswith("_") or key in {"comment", "$schema"}:
                continue
            if key == "detection":
                if isinstance(value, dict):
                    self._apply_detection(value)
                continue
            if key == "export":
                if isinstance(value, dict):
                    self._apply_export(value)
                continue
            if key not in known:
                raise ValueError(f"невідомий параметр конфігурації: {key}")
            if key in {"input_dir", "output_dir"} and value is not None:
                value = Path(str(value))
            else:
                value = _coerce(key, value, getattr(self, key))
            setattr(self, key, value)
        self.validate()

    def _apply_detection(self, raw: dict[str, Any]) -> None:
        known = {f.name for f in fields(DetectionSettings)}
        for key, value in raw.items():
            if key.startswith("_") or key == "comment":
                continue
            if key not in known:
                raise ValueError(f"невідомий параметр detection: {key}")
            setattr(self.detection, key, _coerce(f"detection.{key}", value,
                                                 getattr(self.detection, key)))

    def _apply_export(self, raw: dict[str, Any]) -> None:
        known = {f.name for f in fields(ExportSettings)}
        for key, value in raw.items():
            if key.startswith("_") or key == "comment":
                continue
            if key not in known:
                raise ValueError(f"невідомий параметр export: {key}")
            if key in {"input_dir", "output_dir", "vault"} and value is not None:
                value = Path(str(value)) if key != "vault" else str(value)
            else:
                value = _coerce(f"export.{key}", value, getattr(self.export, key))
            setattr(self.export, key, value)

    def validate(self) -> None:
        if self.export.page_size not in PAGE_SIZES:
            raise ValueError(f"export.page_size має бути одним з {PAGE_SIZES}")
        if self.export.margin not in MARGIN_CHOICES:
            raise ValueError(f"export.margin має бути одним з {MARGIN_CHOICES}")
        if not 10 <= self.export.downscale <= 100:
            raise ValueError("export.downscale має бути в межах 10..100")
        if self.export.outline not in OUTLINE_CHOICES:
            raise ValueError(f"export.outline має бути одним з {OUTLINE_CHOICES}")
        if self.existing not in EXISTING_CHOICES:
            raise ValueError(f"existing має бути одним з {EXISTING_CHOICES}")
        if self.fallback not in FALLBACK_CHOICES:
            raise ValueError(f"fallback має бути одним з {FALLBACK_CHOICES}")
        if self.detection.max_depth < 1:
            raise ValueError("detection.max_depth має бути >= 1")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["input_dir"] = str(self.input_dir)
        data["output_dir"] = str(self.output_dir) if self.output_dir else None
        return data
