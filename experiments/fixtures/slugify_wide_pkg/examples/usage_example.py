"""Usage example for utils.text_utils (unrelated to the slugify package)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import text_utils  # noqa: E402


def main() -> int:
    sample = "one two three"
    print("words:", text_utils.count_words(sample))
    print("blank:", text_utils.is_blank(sample))
    print("first:", text_utils.first_line(sample))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
