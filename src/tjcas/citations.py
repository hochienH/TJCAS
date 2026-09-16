"""Stage 1: extract the citation list of every judgment in the corpus.

Archives are expanded one at a time into scratch space, scanned, then deleted —
the full text is never kept, only ``(citing entry, year, code, number, span)``.
The span points into ``patterns.normalize(JFULL)``, so stage 3 can recover the
surrounding context by re-extracting just the judgments it needs.
"""

from __future__ import annotations

import csv
import gzip
import json
import shutil
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from tjcas.config import CITATIONS_DIR, get_work_route
from tjcas.corpus import parse_entry_name
from tjcas.numbers import split_suffix, to_arabic
from tjcas.patterns import find_citations, normalize
from tjcas.sampling import extract_archive, list_archives

FIELDNAMES = ["entry", "year", "code", "number", "suffix", "start", "end"]


@dataclass(frozen=True)
class ArchiveReport:
    archive: str
    documents: int
    citations: int
    unparsable: int
    self_refs: int


def iter_judgments(root: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(entry path relative to root, JFULL)`` for every judgment JSON."""
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        full = payload.get("JFULL")
        if isinstance(full, str) and full:
            yield path.relative_to(root).as_posix(), full


def citation_rows(
    entry: str, full_text: str, own_key: tuple[str, str, str] | None = None
) -> tuple[list[dict[str, object]], int, int]:
    """Turn one judgment into index-ready citation rows.

    Returns the rows plus the number of unparsable numerals and of matches on the
    judgment's own case number, which the header always repeats and which is not
    a citation of anything.
    """
    rows: list[dict[str, object]] = []
    unparsable = self_refs = 0
    for citation in find_citations(normalize(full_text)):
        year = to_arabic(citation.year)
        base, suffix = split_suffix(citation.number)
        number = to_arabic(base)
        if year is None or number is None:
            unparsable += 1
            continue
        if own_key is not None and (year, citation.code, number) == own_key:
            self_refs += 1
            continue
        rows.append(
            {
                "entry": entry,
                "year": year,
                "code": citation.code,
                "number": number,
                "suffix": to_arabic(suffix) or "" if suffix else "",
                "start": citation.start,
                "end": citation.end,
            }
        )
    return rows, unparsable, self_refs


def _write_rows(path: Path, rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(staging, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    staging.replace(path)


def process_archive(archive: Path, out_dir: Path, work_dir: Path) -> ArchiveReport:
    root = extract_archive(archive, work_dir)
    documents = citations = unparsable = self_refs = 0
    rows: list[dict[str, object]] = []
    try:
        for entry, full_text in iter_judgments(root):
            documents += 1
            ref = parse_entry_name(archive.stem, entry)
            own_key = (ref.year, ref.code, ref.number) if ref else None
            found, skipped, own = citation_rows(entry, full_text, own_key)
            rows.extend(found)
            citations += len(found)
            unparsable += skipped
            self_refs += own
        _write_rows(out_dir / f"{archive.stem}.csv.gz", rows)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return ArchiveReport(archive.stem, documents, citations, unparsable, self_refs)


def run(
    archives: Iterable[Path] | None = None,
    out_dir: Path = CITATIONS_DIR,
    work_dir: Path | None = None,
    workers: int = 8,
    resume: bool = True,
    verbose: bool = True,
) -> list[ArchiveReport]:
    targets = list(archives) if archives is not None else list_archives()
    if resume:
        targets = [a for a in targets if not (out_dir / f"{a.stem}.csv.gz").exists()]
    if not targets:
        return []
    scratch = work_dir or get_work_route()
    reports: list[ArchiveReport] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(process_archive, archive, out_dir, scratch): archive
            for archive in targets
        }
        for done, future in enumerate(as_completed(futures), start=1):
            report = future.result()
            reports.append(report)
            if verbose:
                print(
                    f"[{done}/{len(targets)}] {report.archive}: "
                    f"{report.documents:>7,} docs  {report.citations:>8,} citations  "
                    f"{report.self_refs:>7,} self  {report.unparsable:>6,} unparsable",
                    flush=True,
                )
    return sorted(reports, key=lambda r: r.archive)


def iter_citation_rows(out_dir: Path = CITATIONS_DIR) -> Iterator[tuple[str, dict[str, str]]]:
    """Replay stage-1 output as ``(archive stem, row)`` pairs."""
    for path in sorted(out_dir.glob("*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                yield path.name[: -len(".csv.gz")], row
