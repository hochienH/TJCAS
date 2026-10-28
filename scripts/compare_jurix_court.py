"""Court-level agreement of TJCAS stage-3 picks with the JURIX regex baseline on T ∩ J."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from tjcas import jurix_court
from tjcas.config import CITATIONS_DIR, CORPUS_DB, DATA_DIR

JURIX_EDGES = Path("/data/user/JURIX-2026/result/edge_extraction")
PREDICTIONS = Path("/data/user/tjcas-work/out/predictions.csv.gz")


def log(message: str) -> None:
    print(f"[{datetime.now():%m-%d %H:%M:%S}] {message}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jurix", default=str(JURIX_EDGES))
    parser.add_argument("--citations", default=str(CITATIONS_DIR))
    parser.add_argument("--corpus-db", default=str(CORPUS_DB))
    parser.add_argument("--predictions", default=str(PREDICTIONS))
    parser.add_argument("--out", default=str(DATA_DIR / "comparison" / "jurix_court_agreement.json"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--n-samples", type=int, default=30)
    parser.add_argument("--months", nargs="*", help="Only these YYYYMM (smoke test).")
    parser.add_argument("--predictions-limit", type=int, help="Stop reading predictions after N rows (smoke test).")
    args = parser.parse_args()

    result = jurix_court.compare(
        Path(args.jurix), Path(args.citations), Path(args.corpus_db), Path(args.predictions),
        workers=args.workers, n_samples=args.n_samples, months=args.months,
        predictions_limit=args.predictions_limit, log=log,
    )
    result["generated_at"] = datetime.now().isoformat(timespec="seconds")
    result["inputs"] = {k: getattr(args, k) for k in ("jurix", "citations", "corpus_db", "predictions", "months")}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, value in result["metrics"].items():
        if not isinstance(value, dict):
            log(f"{name:<30}{value}")
    log(f"wrote {out}")


if __name__ == "__main__":
    main()
