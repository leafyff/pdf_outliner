"""Точка входу: `python -m pdf_outline_maker`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
