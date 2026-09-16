"""Stage 2: count how many corpus files each citation key could refer to."""

from __future__ import annotations

import argparse
from pathlib import Path

from tjcas.candidates import CANDIDATES_DB, collect_keys, export, resolve_keys
from tjcas.config import CITATIONS_DIR, CORPUS_DB


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--citations", default=str(CITATIONS_DIR))
    parser.add_argument("--corpus-db", default=str(CORPUS_DB))
    parser.add_argument("--db", default=str(CANDIDATES_DB))
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()

    db_path = Path(args.db)
    keys = collect_keys(Path(args.citations), db_path, workers=args.workers)
    print(f"\n{keys:,} distinct citation keys")
    resolve_keys(db_path, Path(args.corpus_db), workers=args.workers)
    summary = export(db_path)
    print("\nbucket      keys        occurrences")
    for label in ("none", "unique", "ambiguous"):
        stats = summary.get(label, {"keys": 0, "occurrences": 0})
        print(f"{label:<10}{stats['keys']:>12,}{stats['occurrences']:>19,}")


if __name__ == "__main__":
    main()
