"""Silver comparison of TJCAS citations against the JURIX-2026 regex baseline."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from datetime import datetime
from pathlib import Path

from tjcas import jurix
from tjcas.config import CANDIDATES_DIR, CITATIONS_DIR, CORPUS_DB, DATA_DIR

JURIX_EDGES = Path("/data/user/JURIX-2026/result/edge_extraction")


def log(message: str) -> None:
    print(f"[{datetime.now():%m-%d %H:%M:%S}] {message}", flush=True)


def check_buckets(keys_csv: Path) -> dict[str, int]:
    """Corpus-key membership must reproduce stage 2's none / not-none split."""
    agree = disagree = 0
    with gzip.open(keys_csv, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            key = jurix.make_key(row["year"], row["code"], row["number"])
            in_corpus = key is not None and key in jurix._CORPUS_KEYS
            if in_corpus == (row["bucket"] != "none"):
                agree += 1
            else:
                disagree += 1
    return {"agree": agree, "disagree": disagree}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jurix", default=str(JURIX_EDGES))
    parser.add_argument("--citations", default=str(CITATIONS_DIR))
    parser.add_argument("--corpus-db", default=str(CORPUS_DB))
    parser.add_argument("--keys-csv", default=str(CANDIDATES_DIR / "citation_keys.csv.gz"))
    parser.add_argument("--out", default=str(DATA_DIR / "comparison" / "jurix_comparison.json"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--n-samples", type=int, default=30)
    parser.add_argument("--months", nargs="*", help="Only these YYYYMM (smoke test).")
    args = parser.parse_args()

    result = jurix.compare(
        Path(args.jurix), Path(args.citations), Path(args.corpus_db),
        workers=args.workers, n_samples=args.n_samples, months=args.months, log=log,
    )
    result["bucket_check"] = check_buckets(Path(args.keys_csv))
    log(f"bucket check vs stage 2: {result['bucket_check']}")
    result["generated_at"] = datetime.now().isoformat(timespec="seconds")
    result["inputs"] = {k: getattr(args, k) for k in ("jurix", "citations", "corpus_db", "months")}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, value in {**result["counts"], **result["metrics"], **result["distinct"]}.items():
        log(f"{name:<28}{value}")
    log(f"wrote {out}")


if __name__ == "__main__":
    main()
