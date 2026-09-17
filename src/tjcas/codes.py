"""Collapse every spelling of a 字別 onto one join key.

更審輪次 is written in brackets whose punctuation drifts release to release —
half-width ``(一)``, full-width ``（一）``, angle ``〈一〉``, tortoise ``〔一〕``,
lenticular ``【一】``, the single character ``㈠`` — and before Unicode, Big5 造字
in the private use area (no bracket at all). Filenames from 107 年 onward drop
the brackets entirely (``上更一``). None of this is a citation authoring choice;
it is which decade and which font table wrote the file, so it has to be undone
before two spellings can be compared.
"""

from __future__ import annotations

import re
import unicodedata

# U+F674..U+F67E, confirmed against each judgment's own citation of its case
# number (see /tmp/pua_extrapolate.py): codepoint 0xF67F - n encodes round n.
# 0xE686 also showed up in one filename but resolved to garbage on the only
# occurrence found, so it is deliberately left unmapped.
_PUA_ROUND = re.compile(r"[\uf674-\uf67e]")
_ROUND_WORDS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一"]
_BRACKETS = str.maketrans("", "", "()\u3008\u3009\u3014\u3015\u3010\u3011")


def _round_word(match: re.Match[str]) -> str:
    return _ROUND_WORDS[0xF67F - ord(match.group()) - 1]


def normalize_code(code: str) -> str:
    """Fold bracket/PUA spelling variants of a 字別 onto the same string.

    NFKC folds ㈠ and full-width brackets into ``(一)``; the remaining Big5 造字
    are mapped explicitly. Every bracket character is then stripped, so
    ``上更(一)``, ``上更㈠``, ``上更〈一〉`` and the post-2018 ``上更一`` all become
    ``上更一``.
    """
    text = _PUA_ROUND.sub(_round_word, unicodedata.normalize("NFKC", code))
    return text.translate(_BRACKETS).strip()
