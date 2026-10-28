"""Regex patterns for 「X年Y字第Z號」 citations.

The design follows two layers:
- ``CITATION_PATTERN`` is the strict "ultimate" pattern used for extraction.
- ``PARTIAL_PATTERNS`` are deliberately loose probes. Anything they match that
  the ultimate pattern misses is a gap in the ultimate pattern, which is how
  the pattern gets refined (see ``scripts/audit_patterns.py``).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# 判決書以固定寬度排版，換行處會插入 \r\n 與縮排，切斷引用字串
_LINE_WRAP = re.compile(r"[\r\n]+[ \u3000]*")
# 附表的框線會插在引用中間（例：「89年促字第││    26070號」）
_BOX_DRAWING = re.compile(r"[\u2500-\u257f\uff5c]")

ARABIC = r"0-9\uff10-\uff19"
# 判決常以字母 O、全形 Ｏ 或圓圈代替零；normalize() 的 NFKC 會把全形 Ｏ 轉成 ASCII O
ZERO_ALIASES = r"Oo\u3007\u25cb\u25ef\uff2f\uff4f\u039f\u041e\u043e"
CHINESE_NUM = rf"\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u5eff\u5345\u767e\u5343\u96f6{ZERO_ALIASES}"
CJK = r"\u4e00-\u9fff"
# 判決書源自 Big5，舊式字別會用造字區的字元（例：`上更` 後面那個圈一）
PUA = r"\ue000-\uf8ff"

# 空白與數字的量詞一律用 possessive：號、等、之、頓號都不在數字與空白的字元集裡，
# 回溯救不回一個失敗的比對，只會在附表那種「數字夾長串空白」的排版上指數爆炸
_SP = r"[ \u3000]*+"
YEAR = rf"(?:[{ARABIC}]{{1,3}}|[{CHINESE_NUM}]{{1,6}})"
# 更審輪次寫在括號裡。NFKC 會把 ㈠、（一）、﹙一﹚ 一律收成 (一)，角括號與方頭括號不會
_ROUND = rf"(?:[(\u3008\u3014\u3010][{ARABIC}{CHINESE_NUM}]{{1,3}}[)\u3009\u3015\u3011])"
# 字別不會含「第號月日」，排除可避免向前吃掉上下文；「民」「年」只擋成對的「民國」
# 「年度」，整個字元擋掉會連 附民、國、年訴 這些真字別一起漏掉
_CODE_CHAR = (
    rf"(?:(?![\u7b2c\u865f\u6708\u65e5])(?!\u6c11\u570b)(?!\u5e74\u5ea6)[{CJK}{PUA}])"
)
CODE = rf"{_CODE_CHAR}{{1,10}}{_ROUND}?"
_NUMBER_ITEM = rf"(?:[{ARABIC}{CHINESE_NUM}]{_SP}){{1,15}}+"
_SUFFIX = rf"(?:{_SP}\u4e4b{_SP}(?:[{ARABIC}{CHINESE_NUM}]{_SP}){{1,5}}+)?+"
# 「第六六五六、六六五七、七○○九號」這種頓號連寫記法，「第」有時會每項重複
NUMBER = (
    rf"{_NUMBER_ITEM}{_SUFFIX}"
    rf"(?:{_SP}[\u3001,]{_SP}(?:\u7b2c{_SP})?{_NUMBER_ITEM}{_SUFFIX})*+"
)

CITATION_PATTERN = re.compile(
    rf"(?P<year>{YEAR}){_SP}\u5e74{_SP}(?:\u5ea6{_SP})?"
    rf"(?P<code>{CODE}){_SP}\u5b57{_SP}\u7b2c{{1,2}}{_SP}"
    rf"(?P<number>{NUMBER}){_SP}(?:\u7b49{_SP})?\u865f"
)

PARTIAL_PATTERNS: dict[str, re.Pattern[str]] = {
    # Z 只允許阿拉伯數字 / 只允許國字，分別檢查兩種寫法的覆蓋率
    "arabic_number": re.compile(rf"{YEAR}{_SP}\u5e74{_SP}(?:\u5ea6{_SP})?{CODE}\u5b57\u7b2c[{ARABIC}]+\u865f"),
    "chinese_number": re.compile(rf"{YEAR}{_SP}\u5e74{_SP}(?:\u5ea6{_SP})?{CODE}\u5b57\u7b2c[{CHINESE_NUM}]+\u865f"),
    # Z 完全放寬，用來發現數字部分還有哪些沒想到的字元
    "any_number": re.compile(rf"{YEAR}{_SP}\u5e74{_SP}(?:\u5ea6{_SP})?{CODE}{_SP}\u5b57{_SP}\u7b2c.{{1,25}}?\u865f"),
    # Y 完全放寬，用來發現字別部分的例外寫法
    "any_code": re.compile(rf"{YEAR}{_SP}\u5e74{_SP}(?:\u5ea6{_SP})?.{{1,12}}?\u5b57{_SP}\u7b2c.{{1,25}}?\u865f"),
    # X 完全放寬，用來發現年份部分的例外寫法
    "any_year": re.compile(rf".{{1,8}}?\u5e74{_SP}(?:\u5ea6{_SP})?{CODE}{_SP}\u5b57{_SP}\u7b2c.{{1,25}}?\u865f"),
    # 連「年」都不要求，涵蓋公文字號等非判決寫法
    "no_year": re.compile(rf"[{CJK}]{{1,10}}\u5b57{_SP}\u7b2c.{{1,25}}?\u865f"),
}


@dataclass(frozen=True)
class Citation:
    year: str
    code: str
    number: str
    start: int
    end: int
    text: str


def normalize(text: str) -> str:
    """Join citations broken by line wrapping or table borders."""
    return _BOX_DRAWING.sub(" ", unicodedata.normalize("NFKC", _LINE_WRAP.sub("", text)))


def find_citations(text: str) -> list[Citation]:
    """Extract citations; a 頓號-joined list yields one citation per number."""
    citations: list[Citation] = []
    for match in CITATION_PATTERN.finditer(text):
        year = _squeeze(match.group("year"))
        code = _squeeze(match.group("code"))
        for number in re.split(r"[\u3001,]", _squeeze(match.group("number"))):
            number = number.lstrip("\u7b2c")
            if not number:
                continue
            citations.append(
                Citation(
                    year=year,
                    code=code,
                    number=number,
                    start=match.start(),
                    end=match.end(),
                    text=match.group(0),
                )
            )
    return citations


def _squeeze(value: str) -> str:
    return re.sub(r"[ \u3000]+", "", value)
