"""Stage 3: disambiguate multi-candidate citations and compare the three methods."""

from __future__ import annotations

import argparse
from pathlib import Path

from tjcas.candidates import CANDIDATES_DB
from tjcas.config import CITATIONS_DIR
from tjcas.resolution import (
    DEFAULT_WINDOW,
    RESOLUTION_DB,
    build_occurrences,
    fetch_candidate_texts,
    fetch_contexts,
    report,
    resolve,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--citations", default=str(CITATIONS_DIR))
    parser.add_argument("--candidates-db", default=str(CANDIDATES_DB))
    parser.add_argument("--db", default=str(RESOLUTION_DB))
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--limit-keys", type=int, help="only the N most cited ambiguous keys")
    parser.add_argument("--max-per-key", type=int, help="cap occurrences kept per key")
    parser.add_argument(
        "--skip-fetch", action="store_true", help="reuse the texts already in the work database"
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not args.skip_fetch:
        occurrences = build_occurrences(
            Path(args.citations),
            Path(args.candidates_db),
            db_path,
            limit_keys=args.limit_keys,
            max_per_key=args.max_per_key,
        )
        print(f"\nambiguous occurrences: {occurrences:,}")
        contexts = fetch_contexts(db_path, args.window, args.workers)
        print(f"contexts fetched: {contexts:,}")
        texts = fetch_candidate_texts(db_path, args.workers)
        print(f"candidate texts fetched: {texts:,}")

    resolved = resolve(db_path)
    print(f"occurrences resolved: {resolved:,}")
    for label, rate in report(db_path).items():
        print(f"{label:<16}{rate:.4f}")


if __name__ == "__main__":
    main()
