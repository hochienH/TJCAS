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
_PUA_ROUND = re.compile(r"[\uf674-\uf67e]")
_ROUND_WORDS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一"]
_BRACKETS = str.maketrans("", "", "()\u3008\u3009\u3014\u3015\u3010\u3011")

# U+E686 is unrelated to the Big5 block above — it appears in exactly one
# filename (KMHM,89,\uE686,1) and JFULL renders it as the real character 鴮
# ("上更鴮字第一號"). That is an OCR/transcription error, not this judgment's
# actual 字別: the case is externally confirmed as 上更(一) — round 1.
_LITERAL_OVERRIDES = {"\ue686": "一"}

# 臺 and 台 are the same 字別 spelled two ways (臺上 / 台上, 臺非 / 台非). The fold
# runs towards 台 because that is what the filenames use: 402,969 documents have
# a 台 in their 字別 against 12 with a 臺. Judgment text uses both, so without
# this every 臺上 citation misses a corpus that files them all under 台上 —
# 1,116,771 occurrences landing in the `none` bucket.
_VARIANTS = str.maketrans({"臺": "台"})


def _round_word(match: re.Match[str]) -> str:
    return _ROUND_WORDS[0xF67F - ord(match.group()) - 1]


def normalize_code(code: str) -> str:
    """Fold bracket/PUA spelling variants of a 字別 onto the same string.

    NFKC folds ㈠ and full-width brackets into ``(一)``; the remaining Big5 造字
    are mapped explicitly. Every bracket character is then stripped, so
    ``上更(一)``, ``上更㈠``, ``上更〈一〉`` and the post-2018 ``上更一`` all become
    ``上更一``. 臺 folds onto 台 for the same reason: same 字別, two spellings.
    """
    text = _PUA_ROUND.sub(_round_word, unicodedata.normalize("NFKC", code))
    for literal, replacement in _LITERAL_OVERRIDES.items():
        text = text.replace(literal, replacement)
    return text.translate(_VARIANTS).translate(_BRACKETS).strip()
