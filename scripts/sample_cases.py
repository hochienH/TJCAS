"""CLI: sample the largest judgment JSON of every monthly archive."""

from __future__ import annotations

import argparse
from pathlib import Path

from tjcas.config import MANIFEST_PATH, SAMPLES_DIR
from tjcas.sampling import sample_cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=SAMPLES_DIR)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument(
        "--limit", type=int, default=None, help="only process the first N archives"
    )
    args = parser.parse_args()

    sampled = sample_cases(
        dest_dir=args.dest, manifest_path=args.manifest, limit=args.limit
    )
    total = sum(entry.size for entry in sampled)
    print(f"\nsampled {len(sampled)} cases, {total:,} bytes -> {args.dest}")


if __name__ == "__main__":
    main()
