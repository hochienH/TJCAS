"""Corpus-scale gap audit: what the loose patterns catch and the strict one misses.

This has to run *before* stage 1: every gap found here changes
``CITATION_PATTERN``, and changing the pattern invalidates every citation
already extracted.

Gap examples are collapsed into a *shape* — every numeral becomes ``#`` — so the
report stays human-sized. `89年促字第26070號` and `90年促字第12345號` are one
problem to fix, not two.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from tjcas.citations import judgment_text
from tjcas.patterns import (
    ARABIC,
    CHINESE_NUM,
    PARTIAL_PATTERNS,
    find_citations,
    normalize,
)
from tjcas.sampling import list_archives, stream_entries

_NUMERALS = re.compile(rf"[{ARABIC}{CHINESE_NUM}]+")
SHAPES_PER_PATTERN = 5_000


def shape_of(text: str) -> str:
    """Collapse a match into its shape by masking every run of numerals."""
    return _NUMERALS.sub("#", text)


@dataclass
class GapReport:
    archive: str
    documents: int = 0
    citations: int = 0
    gaps: Counter[str] = field(default_factory=Counter)
    shapes: Counter[tuple[str, str]] = field(default_factory=Counter)
    examples: dict[tuple[str, str], str] = field(default_factory=dict)

    def merge(self, other: GapReport) -> None:
        self.documents += other.documents
        self.citations += other.citations
        self.gaps.update(other.gaps)
        self.shapes.update(other.shapes)
        for key, context in other.examples.items():
            self.examples.setdefault(key, context)

    def trim(self, per_pattern: int = SHAPES_PER_PATTERN) -> None:
        """Keep only the commonest shapes so the report can cross process boundaries."""
        by_pattern: dict[str, list[tuple[tuple[str, str], int]]] = {}
        for key, count in self.shapes.items():
            by_pattern.setdefault(key[0], []).append((key, count))
        kept: set[tuple[str, str]] = set()
        for items in by_pattern.values():
            items.sort(key=lambda item: item[1], reverse=True)
            kept.update(key for key, _ in items[:per_pattern])
        self.shapes = Counter({key: self.shapes[key] for key in kept})
        self.examples = {key: self.examples[key] for key in kept if key in self.examples}


def scan_text(text: str, report: GapReport, context: int) -> None:
    citations = find_citations(text)
    report.citations += len(citations)
    covered = [(c.start, c.end) for c in citations]
    for name, pattern in PARTIAL_PATTERNS.items():
        for match in pattern.finditer(text):
            if any(start < match.end() and match.start() < end for start, end in covered):
                continue
            key = (name, shape_of(match.group(0)))
            report.gaps[name] += 1
            report.shapes[key] += 1
            report.examples.setdefault(
                key, text[max(0, match.start() - context) : match.end() + context]
            )


def scan_archive(archive: Path, context: int) -> GapReport:
    report = GapReport(archive.stem)
    for _, payload in stream_entries(archive):
        full_text = judgment_text(payload)
        if full_text is None:
            continue
        report.documents += 1
        scan_text(normalize(full_text), report, context)
    report.trim()
    return report


def scan_corpus(
    archives: Iterable[Path] | None = None,
    context: int = 20,
    workers: int = 32,
    verbose: bool = True,
) -> GapReport:
    targets = list(archives) if archives is not None else list_archives()
    total = GapReport("corpus")
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(scan_archive, a, context): a for a in targets}
        for done, future in enumerate(as_completed(futures), start=1):
            report = future.result()
            total.merge(report)
            if verbose:
                print(
                    f"[{done}/{len(targets)}] {report.archive}: "
                    f"{report.documents:>7,} docs  {report.citations:>8,} citations  "
                    f"{sum(report.gaps.values()):>7,} gaps",
                    flush=True,
                )
    return total
