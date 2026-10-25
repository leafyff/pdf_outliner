"""Розбір аргументів командного рядка та головний цикл обробки."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import EXISTING_CHOICES, FALLBACK_CHOICES, Settings
from .discovery import Job, collect_jobs, is_output_name, is_pdf, output_path_for
from .pipeline import STATUS_FAILED, Result, process
from .report import Reporter, setup_console

#: Тека проєкту (там, де лежать run.bat і config.json).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_NAME = "config.json"
#: run.bat запам'ятовує в цій змінній теку, з якої його запустили: сам він
#: переходить до теки проєкту, тому відносні шляхи інакше вказували б не туди.
CALLER_DIR_ENV = "PDF_OUTLINE_MAKER_CWD"


def caller_dir() -> Path:
    """Тека, відносно якої користувач писав шляхи в командному рядку."""
    raw = os.environ.get(CALLER_DIR_ENV)
    if raw:
        candidate = Path(raw)
        if candidate.is_dir():
            return candidate
    return Path.cwd()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf_outline_maker",
        description="Додає структурований зміст (закладки) до PDF-файлів з теки data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Приклади:\n"
            "  run.bat                        обробити всі PDF у теці data\n"
            "  run.bat --dry-run              показати знайдені заголовки, нічого не писати\n"
            "  run.bat --input D:\\Конспекти   взяти файли з іншої теки\n"
            "  run.bat --max-depth 2          лишити у змісті лише два рівні\n"
        ),
    )
    parser.add_argument("paths", nargs="*", type=Path,
                        help="конкретні PDF або теки; без них береться тека data")
    parser.add_argument("-i", "--input", type=Path, help="тека з вхідними PDF")
    parser.add_argument("-o", "--output", type=Path, help="тека для результатів")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="шукати PDF і в підтеках")
    parser.add_argument("--existing", choices=EXISTING_CHOICES,
                        help="що робити з PDF, у якому зміст уже є")
    parser.add_argument("--fallback", choices=FALLBACK_CHOICES,
                        help="запасний варіант, коли заголовків не знайдено")
    parser.add_argument("--max-depth", type=int, metavar="N",
                        help="максимальна глибина змісту (типово 4)")
    parser.add_argument("--expand-levels", type=int, metavar="N",
                        help="до якого рівня розгортати панель закладок (типово всі)")
    parser.add_argument("--title-node", dest="add_title_node", action="store_true",
                        help="загорнути зміст у пункт з назвою документа")
    parser.add_argument("--no-open-bookmarks", dest="open_bookmarks", action="store_false",
                        help="не розгортати панель закладок при відкритті")
    parser.add_argument("--rewrite", dest="incremental", action="store_false",
                        help="перезаписати PDF повністю замість дозапису")
    parser.add_argument("--skip-existing", action="store_true",
                        help="не чіпати файли, для яких результат уже створено")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="показати результат, нічого не записуючи")
    parser.add_argument("--no-tree", dest="show_tree", action="store_false",
                        help="не друкувати дерево змісту")
    parser.add_argument("-v", "--verbose", action="store_true", help="докладний вивід")
    parser.add_argument("-q", "--quiet", action="store_true", help="лише помилки")
    parser.add_argument("--config", type=Path, help=f"шлях до {CONFIG_NAME}")
    parser.set_defaults(open_bookmarks=None, incremental=None, show_tree=None,
                        add_title_node=None)
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    """Порядок пріоритетів: значення за замовчуванням < config.json < ключі CLI."""
    if args.config is not None:
        config_path = args.config
        if not config_path.is_absolute():
            config_path = caller_dir() / config_path
        if not config_path.exists():
            raise ValueError(f"файл конфігурації «{args.config}» не знайдено")
    else:
        config_path = PROJECT_ROOT / CONFIG_NAME
    settings = Settings.load(config_path)

    if args.input is not None:
        settings.input_dir = args.input
    if args.output is not None:
        settings.output_dir = args.output
    if args.recursive:
        settings.recursive = True
    if args.existing:
        settings.existing = args.existing
    if args.fallback:
        settings.fallback = args.fallback
    if args.max_depth is not None:
        settings.detection.max_depth = args.max_depth
    if args.expand_levels is not None:
        settings.expand_levels = args.expand_levels
    if args.add_title_node is not None:
        settings.add_title_node = args.add_title_node
    if args.open_bookmarks is not None:
        settings.open_bookmarks = args.open_bookmarks
    if args.incremental is not None:
        settings.incremental = args.incremental
    if args.show_tree is not None:
        settings.show_tree = args.show_tree
    if args.skip_existing:
        settings.skip_existing = True
    if args.dry_run:
        settings.dry_run = True
    if args.verbose:
        settings.verbose = True
    if args.quiet:
        settings.quiet = True

    # Теку з config.json шукаємо поруч із програмою, а вказану ключем -i —
    # відносно того місця, звідки користувач запустив програму.
    if not settings.input_dir.is_absolute():
        base = caller_dir() if args.input is not None else PROJECT_ROOT
        settings.input_dir = (base / settings.input_dir).resolve()
    if settings.output_dir is not None and not settings.output_dir.is_absolute():
        base = caller_dir() if args.output is not None else PROJECT_ROOT
        settings.output_dir = (base / settings.output_dir).resolve()

    settings.validate()
    return settings


def _gather(paths: list, settings: Settings) -> tuple[list, list]:
    """Збирає завдання з явно переданих шляхів або з теки за замовчуванням."""
    if not paths:
        return collect_jobs(settings.input_dir, settings.output_dir, settings.recursive)

    jobs: list = []
    skipped: list = []
    base = caller_dir()
    for path in paths:
        path = path if path.is_absolute() else (base / path)
        if path.is_dir():
            more_jobs, more_skipped = collect_jobs(path, settings.output_dir, settings.recursive)
            jobs.extend(more_jobs)
            skipped.extend(more_skipped)
        elif path.is_file() and is_pdf(path):
            if is_output_name(path):
                skipped.append((path, "вже є результатом (_outline)"))
            else:
                jobs.append(Job(source=path, target=output_path_for(path, settings.output_dir)))
        else:
            skipped.append((path, "не знайдено або не PDF"))
    return jobs, skipped


def main(argv: list | None = None) -> int:
    setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = settings_from_args(args)
    except ValueError as exc:
        print(f"Помилка конфігурації: {exc}", file=sys.stderr)
        return 2

    reporter = Reporter(settings)

    if not settings.input_dir.exists() and not args.paths:
        print(f"Тека «{settings.input_dir}» не існує.", file=sys.stderr)
        print("Створіть її та покладіть туди PDF-файли.", file=sys.stderr)
        return 2

    jobs, skipped = _gather(list(args.paths), settings)
    reporter.header(settings.input_dir, len(jobs))
    reporter.skipped_inputs(skipped)

    missing = [p for p, reason in skipped if reason == "не знайдено або не PDF"]
    if missing and not jobs:
        for path in missing:
            print(f"Не знайдено PDF: {path}", file=sys.stderr)
        return 2

    if not jobs:
        reporter.nothing_to_do(settings.input_dir)
        return 0

    results = []
    for index, job in enumerate(jobs, start=1):
        try:
            result = process(job, settings)
        except KeyboardInterrupt:
            print("\nПерервано користувачем.", file=sys.stderr)
            return 130
        except Exception as exc:  # noqa: BLE001 - один зламаний файл не спиняє решту
            result = Result(job=job, status=STATUS_FAILED, message=f"несподівана помилка: {exc}")
        results.append(result)
        reporter.file_result(index, len(jobs), result)

    reporter.summary(results)
    return 1 if any(r.status == STATUS_FAILED for r in results) else 0
