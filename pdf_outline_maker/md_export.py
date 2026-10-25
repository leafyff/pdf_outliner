"""Перетворювач md -> PDF зі змістом.

Верстку робить не ця програма, а сам Obsidian: його конвертер — єдиний, що
дає точно той вигляд, який видно на екрані (тема, callouts, KaTeX, вкладення).
Бракує йому лише однієї речі — дерева закладок.

Тому програма ставить у сховище маленький плагін (`obsidian_plugin\\
pdf-outline-export`), просить Obsidian зробити його ж звичайний експорт і
лише додає до нього опцію Chromium `generateDocumentOutline`, яка складає
зміст із заголовків `<h1>…<h6>`. Рендеринг від цього не змінюється взагалі.

Обмін із плагіном — через посилання `obsidian://pdf-outline-export?...`;
відповідь плагін лишає у файлі `<результат>.pdf.status`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from . import md_outline
from .config import (
    MARGIN_CHOICES,
    OUTLINE_CHOICES,
    OUTLINE_CHROMIUM,
    PAGE_SIZES,
    Settings,
)
from .report import setup_console
from .text import plural
from .writer import PdfWriteError, write_outline

#: Тека проєкту (там, де лежать export.bat і config.json).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_NAME = "config.json"
#: export.bat запам'ятовує теку, з якої його запустили: відносні шляхи в
#: аргументах треба рахувати від неї, а не від теки проєкту.
CALLER_DIR_ENV = "PDF_OUTLINE_MAKER_CWD"

PLUGIN_ID = "pdf-outline-export"
PLUGIN_SOURCE = PROJECT_ROOT / "obsidian_plugin" / PLUGIN_ID
PLUGIN_FILES = ("manifest.json", "main.js")
PROTOCOL_ACTION = "pdf-outline-export"

_LINE = "─" * 64


class ExportError(Exception):
    """Помилка, яку варто показати користувачеві без трасування стека."""


# ----------------------------------------------------------------------
#  Сховище Obsidian
# ----------------------------------------------------------------------

def obsidian_config_path() -> Path:
    """Файл, у якому Obsidian тримає список сховищ."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise ExportError("не знайдено теку APPDATA — це точно Windows?")
    return Path(appdata) / "obsidian" / "obsidian.json"


def known_vaults() -> list[dict]:
    """Усі сховища, про які знає Obsidian, від найсвіжішого."""
    path = obsidian_config_path()
    if not path.exists():
        raise ExportError(
            "Obsidian ще жодного разу не запускався на цьому комп'ютері "
            f"(немає {path})"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ExportError(f"не вдалося прочитати {path.name}: {exc}") from exc
    vaults = []
    for vault_id, entry in (raw.get("vaults") or {}).items():
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        vaults.append({
            "id": vault_id,
            "path": Path(entry["path"]),
            "open": bool(entry.get("open")),
            "ts": entry.get("ts") or 0,
        })
    vaults.sort(key=lambda item: (item["open"], item["ts"]), reverse=True)
    if not vaults:
        raise ExportError("Obsidian не знає жодного сховища")
    return vaults


def pick_vault(wanted: str | None) -> dict:
    """Вибирає сховище: вказане користувачем або відкрите зараз."""
    vaults = known_vaults()
    if not wanted:
        return vaults[0]
    target = Path(wanted)
    for vault in vaults:
        if vault["path"] == target or str(vault["path"]).casefold() == str(target).casefold():
            return vault
        if vault["path"].name.casefold() == wanted.casefold():
            return vault
    names = ", ".join(sorted(vault["path"].name for vault in vaults))
    raise ExportError(f"сховище «{wanted}» не знайдено. Відомі: {names}")


# ----------------------------------------------------------------------
#  Плагін
# ----------------------------------------------------------------------

@dataclass
class PluginState:
    """Що сталося з плагіном під час підготовки."""

    copied: bool = False      # файли щойно записані (ставили або оновлювали)
    first_install: bool = False
    just_enabled: bool = False

    @property
    def needs_reload(self) -> bool:
        """Obsidian підхопить зміни лише після перезавантаження."""
        return self.copied or self.just_enabled


def install_plugin(vault: Path) -> PluginState:
    """Кладе плагін у сховище й вмикає його в списку community-plugins."""
    for name in PLUGIN_FILES:
        if not (PLUGIN_SOURCE / name).exists():
            raise ExportError(f"у проєкті немає файлу плагіна: {PLUGIN_SOURCE / name}")

    target = vault / ".obsidian" / "plugins" / PLUGIN_ID
    state = PluginState(first_install=not target.exists())
    target.mkdir(parents=True, exist_ok=True)
    for name in PLUGIN_FILES:
        source_file = PLUGIN_SOURCE / name
        target_file = target / name
        same = (
            target_file.exists()
            and target_file.read_bytes() == source_file.read_bytes()
        )
        if not same:
            shutil.copy2(source_file, target_file)
            state.copied = True

    enabled_path = vault / ".obsidian" / "community-plugins.json"
    try:
        enabled = json.loads(enabled_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        enabled = []
    if not isinstance(enabled, list):
        enabled = []
    if PLUGIN_ID not in enabled:
        enabled.append(PLUGIN_ID)
        enabled_path.parent.mkdir(parents=True, exist_ok=True)
        enabled_path.write_text(json.dumps(enabled, indent=2), encoding="utf-8")
        state.just_enabled = True
    return state


# ----------------------------------------------------------------------
#  Виклик Obsidian
# ----------------------------------------------------------------------

def build_uri(vault: dict, source: Path, target: Path, settings) -> str:
    # Зміст від Chromium коштує тегованого PDF: на великій нотатці з
    # математикою друк розтягується на хвилини, а файл роздувається в рази.
    # Тому типово просимо звичайний друк, а зміст вписуємо самі.
    chromium = settings.outline == OUTLINE_CHROMIUM
    params = {
        "vault": vault["path"].name,
        "src": str(source),
        "out": str(target),
        "status": f"{target}.status",
        "pageSize": settings.page_size,
        "includeName": "1" if settings.include_name else "0",
        "downscale": str(settings.downscale),
        "landscape": "1" if settings.landscape else "0",
        "margin": settings.margin,
        "open": "1" if settings.open_result else "0",
        "outline": "1" if chromium else "0",
        "tagged": "1" if chromium else "0",
    }
    # quote_via=quote: пробіл має стати %20, а не «+» — назви нотаток у
    # користувача з пробілами, а розбирач посилань Obsidian «+» не розкодовує.
    return f"obsidian://{PROTOCOL_ACTION}?{urlencode(params, quote_via=quote)}"


def launch(uri: str) -> None:
    """Відкриває посилання obsidian:// — цим Obsidian і отримує завдання."""
    try:
        os.startfile(uri)  # type: ignore[attr-defined]  # лише Windows
    except AttributeError:
        subprocess.run(["cmd", "/c", "start", "", uri], check=False)
    except OSError as exc:
        raise ExportError(f"не вдалося відкрити посилання obsidian:// ({exc})") from exc


#: Скільки секунд після запуску Obsidian вважаємо «холодним» станом.
COLD_SECONDS = 120.0


def obsidian_uptime() -> float | None:
    """Скільки секунд працює Obsidian. None — якщо він не запущений."""
    command = (
        "$p = Get-Process Obsidian -ErrorAction SilentlyContinue | "
        "Sort-Object StartTime | Select-Object -First 1; "
        "if ($p) { $p.StartTime.ToString('o') }"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    stamp = done.stdout.strip()
    if not stamp:
        return None
    try:
        started = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    now = datetime.now(started.tzinfo) if started.tzinfo else datetime.now()
    return max((now - started).total_seconds(), 0.0)


def wait_for_status(status_path: Path, timeout: float) -> dict:
    """Чекає на відповідь плагіна. Повертає словник {ok, out|error}."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if status_path.exists():
            # Файл міг бути перехоплений на півдорозі — даємо запису дописатися.
            for _ in range(10):
                try:
                    return json.loads(status_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    time.sleep(0.05)
        time.sleep(0.2)
    raise ExportError(f"Obsidian не відповів за {timeout:.0f} с")


def export_one(vault: dict, source: Path, target: Path, settings) -> dict:
    """Повний цикл для одного файлу: завдання -> очікування -> перевірка."""
    status_path = Path(f"{target}.status")
    for leftover in (status_path,):
        leftover.unlink(missing_ok=True)
    if target.exists():
        try:
            target.unlink()
        except OSError as exc:
            raise ExportError(
                f"немає доступу до {target.name} — можливо, файл відкритий "
                f"у переглядачі ({exc})"
            ) from exc

    launch(build_uri(vault, source, target, settings))
    try:
        answer = wait_for_status(status_path, settings.timeout)
    finally:
        status_path.unlink(missing_ok=True)

    if not answer.get("ok"):
        raise ExportError(answer.get("error") or "Obsidian повернув помилку")
    if not target.exists():
        raise ExportError("Obsidian відзвітував про успіх, але файлу немає")
    return answer


# ----------------------------------------------------------------------
#  Перевірка результату
# ----------------------------------------------------------------------

def add_outline_from_markdown(source: Path, target: Path, settings) -> None:
    """Вписує у готовий PDF зміст, узятий із розмітки вхідного `.md`."""
    import pymupdf

    with pymupdf.open(target) as document:
        nodes = md_outline.build_for_file(
            document, source,
            title=source.stem if settings.include_name else None,
        )
    if not nodes:
        return
    try:
        write_outline(target, target, nodes)
    except PdfWriteError as exc:
        raise ExportError(str(exc)) from exc


def read_outline(pdf: Path) -> tuple[list, int]:
    """Повертає зміст готового PDF і кількість сторінок."""
    import pymupdf

    with pymupdf.open(pdf) as document:
        return document.get_toc(), document.page_count


def print_outline(toc: list, limit: int = 40) -> None:
    for level, title, page in toc[:limit]:
        print(f"   {'   ' * (level - 1)}{title}  ·  с. {page}")
    if len(toc) > limit:
        print(f"   … ще {len(toc) - limit}")


# ----------------------------------------------------------------------
#  Аргументи та діалог
# ----------------------------------------------------------------------

def caller_dir() -> Path:
    raw = os.environ.get(CALLER_DIR_ENV)
    if raw and Path(raw).is_dir():
        return Path(raw)
    return Path.cwd()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf_outline_maker.md_export",
        description="Перетворює .md на PDF зі змістом, руками самого Obsidian.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Приклади:\n"
            "  export.bat                           спитати параметри й обробити теку md\n"
            "  export.bat --yes                     те саме мовчки, зі значеннями за замовчуванням\n"
            "  export.bat --page-size A3 --downscale 80\n"
            "  export.bat \"C:\\Нотатки\\Лаб 5.md\" -o D:\\PDF\n"
        ),
    )
    parser.add_argument("paths", nargs="*", type=Path,
                        help="конкретні .md або теки; без них береться тека md")
    parser.add_argument("-i", "--input", type=Path, help="тека з вхідними .md")
    parser.add_argument("-o", "--output", type=Path, help="тека для готових PDF")
    parser.add_argument("--page-size", choices=PAGE_SIZES, help="формат аркуша (типово A4)")
    parser.add_argument("--include-name", dest="include_name", action="store_true",
                        help="додати назву файлу першим заголовком")
    parser.add_argument("--no-include-name", dest="include_name", action="store_false",
                        help="не додавати назву файлу")
    parser.add_argument("--downscale", type=int, metavar="N",
                        help="масштаб у відсотках, 10..100 (типово 100)")
    parser.add_argument("--landscape", dest="landscape", action="store_true",
                        help="альбомна орієнтація")
    parser.add_argument("--no-landscape", dest="landscape", action="store_false",
                        help="книжкова орієнтація")
    parser.add_argument("--margin", choices=MARGIN_CHOICES, help="поля сторінки")
    parser.add_argument("--outline", choices=OUTLINE_CHOICES,
                        help="хто будує зміст: md (ми, типово) чи chromium (друкар)")
    parser.add_argument("--vault", help="сховище Obsidian (назва або шлях)")
    parser.add_argument("--timeout", type=float, metavar="СЕК",
                        help="скільки чекати на Obsidian (типово 180)")
    parser.add_argument("--open", dest="open_result", action="store_true",
                        help="відкрити готовий PDF у переглядачі")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="нічого не питати, узяти значення за замовчуванням")
    parser.add_argument("--no-tree", dest="show_tree", action="store_false",
                        help="не друкувати зміст готового файлу")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="показати, скільки часу забрав кожен етап")
    parser.add_argument("-q", "--quiet", action="store_true", help="лише помилки")
    parser.add_argument("--config", type=Path, help=f"шлях до {CONFIG_NAME}")
    parser.set_defaults(include_name=None, landscape=None, show_tree=True)
    return parser


def ask(question: str, default: str) -> str:
    try:
        answer = input(f"{question} [{default}]: ").strip()
    except EOFError:
        return default
    return answer or default


def ask_yes_no(question: str, default: bool) -> bool:
    default_text = "так" if default else "ні"
    while True:
        answer = ask(question, default_text).casefold()
        if answer in {"так", "т", "y", "yes", "1", "+"}:
            return True
        if answer in {"ні", "н", "n", "no", "0", "-"}:
            return False
        print("   Відповідайте «так» або «ні».")


def ask_settings(export) -> None:
    """Запитує три головні параметри вікна експорту Obsidian."""
    print("Параметри експорту (Enter — лишити як є)")
    while True:
        answer = ask(f"  Формат аркуша {'/'.join(PAGE_SIZES)}", export.page_size)
        match = [size for size in PAGE_SIZES if size.casefold() == answer.casefold()]
        if match:
            export.page_size = match[0]
            break
        print("   Такого формату немає.")
    export.include_name = ask_yes_no("  Назва файлу як заголовок", export.include_name)
    while True:
        answer = ask("  Масштаб, %", str(export.downscale))
        if answer.isdigit() and 10 <= int(answer) <= 100:
            export.downscale = int(answer)
            break
        print("   Потрібне число від 10 до 100.")
    print()


def apply_args(export, args: argparse.Namespace) -> None:
    """Ключі командного рядка мають вищий пріоритет за config.json."""
    if args.input is not None:
        export.input_dir = args.input
    if args.output is not None:
        export.output_dir = args.output
    if args.page_size:
        export.page_size = args.page_size
    if args.include_name is not None:
        export.include_name = args.include_name
    if args.downscale is not None:
        export.downscale = args.downscale
    if args.landscape is not None:
        export.landscape = args.landscape
    if args.margin:
        export.margin = args.margin
    if args.outline:
        export.outline = args.outline
    if args.vault:
        export.vault = args.vault
    if args.timeout is not None:
        export.timeout = args.timeout
    if args.open_result:
        export.open_result = True


def collect_sources(paths: list[Path], export, base: Path) -> list[Path]:
    """Список .md для обробки: з аргументів або з теки `md`."""
    sources: list[Path] = []
    if paths:
        for raw in paths:
            path = raw if raw.is_absolute() else base / raw
            if path.is_dir():
                sources.extend(sorted(path.glob("*.md")))
            elif path.suffix.casefold() == ".md":
                sources.append(path)
            else:
                raise ExportError(f"це не .md і не тека: {path}")
    else:
        folder = export.input_dir
        if not folder.is_absolute():
            folder = PROJECT_ROOT / folder
        folder.mkdir(parents=True, exist_ok=True)
        sources = sorted(folder.glob("*.md"))
        if not sources:
            raise ExportError(f"у теці {folder} немає жодного .md — покладіть туди файл")
    missing = [path for path in sources if not path.exists()]
    if missing:
        raise ExportError(f"немає файлу: {missing[0]}")
    return [path.resolve() for path in sources]


def target_for(source: Path, export) -> Path:
    if export.output_dir is None:
        return source.with_suffix(".pdf")
    folder = export.output_dir
    if not folder.is_absolute():
        folder = PROJECT_ROOT / folder
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{source.stem}.pdf"


# ----------------------------------------------------------------------
#  Головний цикл
# ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = build_parser().parse_args(argv)

    config_path = args.config or (PROJECT_ROOT / CONFIG_NAME)
    try:
        settings = Settings.load(config_path if config_path.exists() else None)
    except ValueError as exc:
        print(f"[помилка] {exc}")
        return 2
    export = settings.export
    apply_args(export, args)

    quiet = args.quiet
    try:
        sources = collect_sources(args.paths, export, caller_dir())
        vault = pick_vault(export.vault)
        state = install_plugin(vault["path"])
    except ExportError as exc:
        print(f"[помилка] {exc}")
        return 1

    if not quiet:
        print(_LINE)
        print(f"Сховище Obsidian: {vault['path']}")
        if state.first_install:
            print(f"Плагін «{PLUGIN_ID}» щойно встановлено.")
        elif state.copied:
            print(f"Плагін «{PLUGIN_ID}» оновлено.")
        if state.needs_reload:
            print("Щоб Obsidian його підхопив, перезавантажте застосунок:")
            print("  Ctrl+R у вікні Obsidian (або закрийте й відкрийте його).")
        print(_LINE)

    interactive = not args.yes and sys.stdin is not None and sys.stdin.isatty()
    if interactive and not quiet:
        ask_settings(export)

    try:
        settings.validate()
    except ValueError as exc:
        print(f"[помилка] {exc}")
        return 2

    # Перший друк у щойно запущеному Obsidian виходить інакшим за всі
    # наступні: математика доверстується вже по дорозі (штатний експорт
    # Obsidian має ту саму ваду). Тому для першого файлу робимо повтор —
    # він і дає ту верстку, яку видно на екрані.
    uptime = obsidian_uptime()
    repeat_first = uptime is None or uptime < COLD_SECONDS
    if repeat_first and not quiet:
        print("Obsidian щойно запущено — для першого файлу зроблю контрольний повтор.")
        print(_LINE)

    failures = 0
    for index, source in enumerate(sources):
        target = target_for(source, export)
        if not quiet:
            print(f"{source.name}  ->  {target.name}")
        started = time.monotonic()
        try:
            if index == 0 and repeat_first:
                export_one(vault, source, target, export)
            answer = export_one(vault, source, target, export)
            if export.outline != OUTLINE_CHROMIUM:
                add_outline_from_markdown(source, target, export)
            toc, pages = read_outline(target)
        except ExportError as exc:
            failures += 1
            print(f"   [помилка] {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - PyMuPDF на пошкодженому файлі
            failures += 1
            print(f"   [помилка] не вдалося прочитати готовий PDF: {exc}")
            continue
        if quiet:
            continue
        for warning in answer.get("warnings") or []:
            print(f"   [увага] {warning}")
        if args.verbose and answer.get("stages"):
            stages = ", ".join(f"{name} {value / 1000:.1f} с"
                               for name, value in answer["stages"].items())
            print(f"   [етапи] {stages}")
        seconds = time.monotonic() - started
        if toc:
            depth = max(level for level, _, _ in toc)
            print(
                f"   {plural(len(toc), 'пункт', 'пункти', 'пунктів')} змісту, "
                f"{plural(depth, 'рівень', 'рівні', 'рівнів')}, "
                f"{plural(pages, 'сторінка', 'сторінки', 'сторінок')}, "
                f"{seconds:.1f} с"
            )
            if args.show_tree:
                print_outline(toc)
        else:
            print(f"   PDF готовий ({pages} с.), але заголовків у нотатці немає — зміст порожній")

    if failures and not quiet:
        print(_LINE)
        print("Якщо Obsidian не відповідає: він має бути запущений, а плагін")
        print(f"«{PLUGIN_ID}» — увімкнений у Settings -> Community plugins.")
    return 1 if failures else 0


if __name__ == "__main__":  # `python -m pdf_outline_maker.md_export`
    sys.exit(main())
