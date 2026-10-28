"""Convert the 國字 numerals used in citations into the Arabic form used by filenames.

Judgments write numbers in two incompatible styles and only context tells them
apart: positional (``八十五`` = 85, ``一百零四`` = 104) and digit-by-digit
(``七八八八`` = 7888, ``一二四六二`` = 12462). A number containing 十/百/千/萬 is
read positionally, otherwise digit-by-digit.
"""

from __future__ import annotations

import re

_DIGITS = {
    "零": 0, "〇": 0, "○": 0, "◯": 0, "O": 0, "o": 0, "Ο": 0, "О": 0, "о": 0,
    "一": 1, "壹": 1,
    "二": 2, "貳": 2, "兩": 2,
    "三": 3, "參": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陸": 6,
    "七": 7, "柒": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}
_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000, "萬": 10000}
_POSITIONAL = re.compile(r"[十拾百佰千仟萬]")
_SUFFIX = re.compile(r"[之-]")


def _positional_value(text: str) -> int | None:
    total = section = digit = 0
    seen = False
    for char in text:
        if char in _DIGITS:
            digit = _DIGITS[char]
            seen = True
        elif char in _UNITS:
            unit = _UNITS[char]
            if unit == 10000:
                total = (total + section + digit) * unit
                section = digit = 0
            else:
                section += (digit or 1) * unit
                digit = 0
            seen = True
        else:
            return None
    return total + section + digit if seen else None


def _digitwise_value(text: str) -> str | None:
    digits = []
    for char in text:
        if char.isdigit():
            digits.append(char)
        elif char in _DIGITS:
            digits.append(str(_DIGITS[char]))
        else:
            return None
    return "".join(digits) or None


def to_arabic(text: str) -> str | None:
    """Return ``text`` as a plain Arabic numeral string, or None if unparsable."""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return str(int(text))
    if _POSITIONAL.search(text):
        value = _positional_value(text)
        return None if value is None else str(value)
    digits = _digitwise_value(text)
    return None if digits is None else str(int(digits))


def split_suffix(number: str) -> tuple[str, str]:
    """Split ``123之1`` into its base number and the 之-suffix (empty if none)."""
    parts = _SUFFIX.split(number, maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else ""
