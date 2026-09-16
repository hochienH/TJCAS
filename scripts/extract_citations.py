"""Stage 1: extract every judgment's citation list across the whole corpus."""

from __future__ import annotations

import argparse
from pathlib import Path

from tjcas.citations import run
from tjcas.config import CITATIONS_DIR
from tjcas.sampling import list_archives


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="only process the first N archives")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--out", default=str(CITATIONS_DIR))
    parser.add_argument("--work-dir", help="scratch space for extraction (default WORK_ROUTE)")
    parser.add_argument(
        "--no-resume", action="store_true", help="reprocess archives that already have output"
    )
    args = parser.parse_args()

    archives = list_archives()[: args.limit]
    reports = run(
        archives,
        out_dir=Path(args.out),
        work_dir=Path(args.work_dir) if args.work_dir else None,
        workers=args.workers,
        resume=not args.no_resume,
    )
    documents = sum(r.documents for r in reports)
    citations = sum(r.citations for r in reports)
    unparsable = sum(r.unparsable for r in reports)
    self_refs = sum(r.self_refs for r in reports)
    print(
        f"\n{len(reports)} archives  {documents:,} documents  "
        f"{citations:,} citations  {self_refs:,} self references  "
        f"{unparsable:,} unparsable numbers"
    )


if __name__ == "__main__":
    main()
