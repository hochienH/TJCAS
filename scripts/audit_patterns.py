"""CLI: audit the ultimate citation pattern against the loose partial patterns.

Anything a partial pattern matches but the ultimate pattern does not is
reported as a gap, together with the surrounding text, so the pattern can be
refined round after round.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from tjcas.case_types import (
    COURT_CSV,
    OBSERVED_CSV,
    PROSECUTION_CSV,
    load_codes,
)
from tjcas.config import MANIFEST_DIR, SAMPLES_DIR
from tjcas.patterns import PARTIAL_PATTERNS, find_citations, normalize

GAP_REPORT = MANIFEST_DIR / "pattern_gaps.csv"
CODE_REPORT = MANIFEST_DIR / "pattern_codes.csv"


def classify(code: str, observed: set[str], prosecution: set[str], court: set[str]) -> str:
    if code in observed:
        return "court"
    if code in prosecution:
        return "prosecution"
    if code in court:
        return "court_pdf_only"
    return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=SAMPLES_DIR)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--context", type=int, default=15)
    args = parser.parse_args()

    observed = load_codes(OBSERVED_CSV) if OBSERVED_CSV.exists() else set()
    prosecution = load_codes(PROSECUTION_CSV) if PROSECUTION_CSV.exists() else set()
    court = load_codes(COURT_CSV) if COURT_CSV.exists() else set()

    files = sorted(args.samples.glob("*.json"))[: args.limit]
    total = 0
    kinds: Counter[str] = Counter()
    codes: Counter[tuple[str, str]] = Counter()
    gap_counts: Counter[str] = Counter()
    gaps: list[dict[str, object]] = []

    for path in files:
        text = normalize(json.loads(path.read_text(encoding="utf-8")).get("JFULL", ""))
        citations = find_citations(text)
        total += len(citations)
        covered = [(c.start, c.end) for c in citations]
        for citation in citations:
            kind = classify(citation.code, observed, prosecution, court)
            kinds[kind] += 1
            codes[(citation.code, kind)] += 1

        for name, pattern in PARTIAL_PATTERNS.items():
            for match in pattern.finditer(text):
                if any(start < match.end() and match.start() < end for start, end in covered):
                    continue
                gap_counts[name] += 1
                gaps.append(
                    {
                        "pattern": name,
                        "sample": path.name,
                        "match": match.group(0),
                        "context": text[
                            max(0, match.start() - args.context) : match.end() + args.context
                        ],
                    }
                )

    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    _write(GAP_REPORT, ["pattern", "sample", "match", "context"], gaps)
    _write(
        CODE_REPORT,
        ["code", "kind", "count"],
        [{"code": c, "kind": k, "count": n} for (c, k), n in codes.most_common()],
    )

    print(f"files={len(files)} citations={total}")
    print("classification:", dict(kinds))
    print("gaps per partial pattern:", dict(gap_counts))
    print(f"reports -> {GAP_REPORT}, {CODE_REPORT}")


def _write(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
