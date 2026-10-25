"""Дрібні мовні помічники для повідомлень користувачеві."""

from __future__ import annotations


def plural(count: int, one: str, few: str, many: str) -> str:
    """Українська форма числівника: 1 закладка, 2 закладки, 5 закладок."""
    tail_100 = abs(count) % 100
    tail_10 = abs(count) % 10
    if 11 <= tail_100 <= 14:
        word = many
    elif tail_10 == 1:
        word = one
    elif 2 <= tail_10 <= 4:
        word = few
    else:
        word = many
    return f"{count} {word}"
