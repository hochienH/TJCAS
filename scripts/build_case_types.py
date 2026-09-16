"""CLI: build the case-type (字別) dictionaries."""

from __future__ import annotations

import argparse

from tjcas.case_types import (
    COURT_CSV,
    OBSERVED_CSV,
    PROSECUTION_CSV,
    scan_observed,
    write_court_csv,
    write_observed_csv,
    write_prosecution_csv,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-official", action="store_true")
    parser.add_argument("--skip-observed", action="store_true")
    parser.add_argument(
        "--limit", type=int, default=None, help="only scan the first N archives"
    )
    args = parser.parse_args()

    if not args.skip_official:
        print(f"court: {write_court_csv()} rows -> {COURT_CSV}")
        print(f"prosecution: {write_prosecution_csv()} rows -> {PROSECUTION_CSV}")

    if not args.skip_observed:
        records = scan_observed(limit=args.limit)
        write_observed_csv(records)
        print(f"observed: {len(records)} codes -> {OBSERVED_CSV}")


if __name__ == "__main__":
    main()
