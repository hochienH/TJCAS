"""Case-type (字別) dictionaries.

Three sources are combined:
- ``observed``: codes actually used in the open-data filenames. Every judgment
  file is named ``court,year,code,number,date,seq.json``, so this is the
  authoritative court-code list for the corpus; it also covers 最高法院 and
  行政法院, which the court PDF does not.
- ``court``: 臺灣高等法院以下各級法院民刑事案件案號字別及案件種類對照表 (PDF),
  used for the semantics of each code.
- ``prosecution``: 檢察案件編號計數分案報結實施要點 (PDF); those numbers are
  prosecutor case files, not court decisions.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from tjcas.config import MANIFEST_DIR, REFERENCE_DIR
from tjcas.sampling import iter_entries, list_archives

COURT_PDF = REFERENCE_DIR / "民刑事件編號計數分案報結實施要點附件.pdf"
PROSECUTION_PDF = REFERENCE_DIR / "檢察案件編號計數分案報結實施要點.pdf"
COURT_CSV = MANIFEST_DIR / "case_types_court.csv"
PROSECUTION_CSV = MANIFEST_DIR / "case_types_prosecution.csv"
OBSERVED_CSV = MANIFEST_DIR / "case_types_observed.csv"

_CJK = r"\u4e00-\u9fff"
_MAX_CODE_LEN = 8
# characters that never occur inside a 字別 but do occur in quoted phrases
_NOT_A_CODE = re.compile(r"[案法院署部條項款年月日之的及或]")


@dataclass
class ObservedCode:
    code: str
    count: int = 0
    courts: set[str] = field(default_factory=set)
    years: set[str] = field(default_factory=set)


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def _is_code(token: str) -> bool:
    return (
        1 <= len(token) <= _MAX_CODE_LEN
        and re.fullmatch(rf"[{_CJK}]+", token) is not None
    )


def parse_court_pdf(path: Path = COURT_PDF) -> list[dict[str, str]]:
    """Read the tabular court PDF: 案件種類 / 案號字別名稱 / 案件種類說明."""
    rows: list[dict[str, str]] = []
    section = ""
    category = ""
    for raw in _pdf_text(path).splitlines():
        line = raw.strip()
        if not line or "案號字別" in line:
            continue
        if "法院部分" in line or re.match(r"^[（(][一二三四五六七八九十]+[）)]", line):
            section = re.sub(r"^[一二三四五六七八九十、（）()]+", "", line).rstrip("：: ")
            continue
        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) >= 3 and _is_code(parts[1]):
            category, code, description = parts[0], parts[1], " ".join(parts[2:])
        elif len(parts) >= 2:
            code, description = parts[0], " ".join(parts[1:])
        else:
            code, description = parts[0], ""
        if not _is_code(code) or not description:
            if rows:  # a wrapped description line
                rows[-1]["description"] += line
            continue
        rows.append(
            {
                "code": code,
                "section": section,
                "category": category,
                "description": description,
            }
        )
    return rows


def parse_prosecution_pdf(path: Path = PROSECUTION_PDF) -> list[dict[str, str]]:
    """Read the prose-style prosecution PDF, where codes appear as 「X」."""
    text = re.sub(r"[\s\u3000]+", "", _pdf_text(path))
    stage = ""
    rows: dict[str, str] = {}
    for chunk in re.split(r"(?=[一二三四五六七八九十]+、)", text):
        header = re.match(r"^[一二三四五六七八九十]+、(.{0,12}?)[案卷業]", chunk)
        if header:
            stage = header.group(1)
        for quoted in re.findall(rf"「([{_CJK}（）()一二三四五六七八九十]{{1,10}})」", chunk):
            code = re.sub(r"[（(][一二三四五六七八九十]+[）)]$", "", quoted)
            if not _is_code(code) or _NOT_A_CODE.search(code):
                continue
            rows.setdefault(code, stage)
    return [{"code": code, "section": stage} for code, stage in rows.items()]


def scan_observed(
    data_route: Path | None = None, limit: int | None = None, verbose: bool = True
) -> list[ObservedCode]:
    """Collect the case-type codes used in archive filenames (no extraction)."""
    observed: dict[str, ObservedCode] = defaultdict(lambda: ObservedCode(""))
    archives = list_archives(data_route)[:limit]
    for index, archive in enumerate(archives, start=1):
        for entry in iter_entries(archive):
            parts = Path(entry.name).stem.split(",")
            if len(parts) < 4:
                continue
            court, year, code = parts[0], parts[1], parts[2]
            record = observed[code]
            record.code = code
            record.count += 1
            record.courts.add(court)
            record.years.add(year)
        if verbose:
            print(f"[{index}/{len(archives)}] {archive.name}: {len(observed)} codes", flush=True)
    return sorted(observed.values(), key=lambda r: -r.count)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def write_court_csv(path: Path = COURT_CSV) -> int:
    rows = parse_court_pdf()
    _write_csv(path, ["code", "section", "category", "description"], rows)
    return len(rows)


def write_prosecution_csv(path: Path = PROSECUTION_CSV) -> int:
    rows = parse_prosecution_pdf()
    _write_csv(path, ["code", "section"], rows)
    return len(rows)


def write_observed_csv(records: list[ObservedCode], path: Path = OBSERVED_CSV) -> None:
    rows: list[dict[str, object]] = []
    for record in records:
        years = sorted(record.years, key=lambda y: int(y) if y.isdigit() else -1)
        rows.append(
            {
                "code": record.code,
                "count": record.count,
                "n_courts": len(record.courts),
                "first_year": years[0],
                "last_year": years[-1],
                "top_courts": ";".join(sorted(record.courts)[:5]),
            }
        )
    _write_csv(
        path,
        ["code", "count", "n_courts", "first_year", "last_year", "top_courts"],
        rows,
    )


def load_codes(path: Path) -> set[str]:
    with path.open(encoding="utf-8") as handle:
        return {row["code"] for row in csv.DictReader(handle)}
