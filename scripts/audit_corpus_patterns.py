"""CLI: run the partial-pattern gap audit across the whole corpus.

Must be run before stage 1 — any regex change it triggers invalidates the
citations already extracted.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from tjcas.config import MANIFEST_DIR
from tjcas.gaps import scan_corpus
from tjcas.sampling import list_archives

SHAPE_REPORT = MANIFEST_DIR / "corpus_gap_shapes.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="only scan the first N archives")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--context", type=int, default=20)
    parser.add_argument("--top", type=int, default=300, help="shapes reported per pattern")
    parser.add_argument("--out", type=Path, default=SHAPE_REPORT)
    args = parser.parse_args()

    report = scan_corpus(list_archives()[: args.limit], args.context, args.workers)

    by_pattern: dict[str, list[tuple[str, int]]] = {}
    for (pattern, shape), count in report.shapes.items():
        by_pattern.setdefault(pattern, []).append((shape, count))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["pattern", "shape", "count", "example"])
        for pattern, shapes in sorted(by_pattern.items()):
            shapes.sort(key=lambda item: item[1], reverse=True)
            for shape, count in shapes[: args.top]:
                writer.writerow(
                    [pattern, shape, count, report.examples.get((pattern, shape), "")]
                )

    print(f"\ndocuments={report.documents:,} citations={report.citations:,}")
    print("gaps per partial pattern:")
    for pattern, count in report.gaps.most_common():
        print(f"  {pattern:<16}{count:>10,}  ({len(by_pattern.get(pattern, [])):,} shapes)")
    print(f"report -> {args.out}")


if __name__ == "__main__":
    main()
