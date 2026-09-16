"""Build the global filename index used to resolve 「X年Y字第Z號」 citations."""

from __future__ import annotations

import argparse
from pathlib import Path

from tjcas.config import CORPUS_DB
from tjcas.corpus import build_index
from tjcas.sampling import list_archives


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="only index the first N archives")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--db", default=str(CORPUS_DB))
    args = parser.parse_args()

    archives = list_archives()[: args.limit]
    total = build_index(archives, Path(args.db), workers=args.workers)
    print(f"indexed {total:,} documents from {len(archives)} archives -> {args.db}")


if __name__ == "__main__":
    main()
