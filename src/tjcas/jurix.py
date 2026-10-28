"""Compare TJCAS citations against the JURIX-2026 regex baseline.

There is no ground truth, so JURIX (court name + 年字號 regex, the traditional
extractor shared with another project) serves as a silver reference. Both sides are
reduced to the same edge: (source judgment file, cited key), where the key is
``(year, normalize_code(code), number)`` exactly as the corpus index builds it.
Court is deliberately not compared here; that needs stage-3 resolution.

Sets per source judgment, deduplicated, self-citation (key == own key) dropped:

  J         JURIX edges
  J_corpus  J restricted to keys that exist in the corpus index (post-1996 files)
  T_all     every TJCAS stage-1 edge
  T         T_all restricted to keys in the corpus index (bucket unique/ambiguous)

Silver metrics:

  recall_vs_jurix         |T ∩ J| / |J|
  recall_vs_jurix_corpus  |T ∩ J_corpus| / |J_corpus|
  precision_vs_jurix      |T ∩ J| / |T|
  extra                   |T \\ J|

Both sources partition by monthly archive (JURIX ``result_{YYYYMM}_*.json``,
TJCAS ``{YYYYMM}.csv.gz``), so each month is compared independently and only
the counters are merged. A source is identified by (month, file name).
"""

from __future__ import annotations

import csv
import glob
import gzip
import json
import random
import sqlite3
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from pathlib import Path

from tjcas.codes import normalize_code

Key = tuple[str, str, str]

# Filled in the parent before the pool forks, so workers share it copy-on-write.
_CORPUS_KEYS: frozenset[Key] = frozenset()


@lru_cache(maxsize=None)
def _code(code: str) -> str:
    return normalize_code(code)


def make_key(year: str, code: str, number: str) -> Key | None:
    if not (year.isdigit() and number.isdigit()):
        return None
    return str(int(year)), _code(code), str(int(number))


def own_key(file_name: str) -> Key | None:
    parts = file_name.removesuffix(".json").split(",")
    if len(parts) < 4:
        return None
    return make_key(parts[1], parts[2], parts[3])


def load_corpus_keys(corpus_db: Path) -> frozenset[Key]:
    with sqlite3.connect(f"file:{corpus_db}?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT DISTINCT year, code_norm, number FROM documents")
        return frozenset((year, code, number) for year, code, number in rows)


def _jurix_edges(files: list[str]) -> tuple[set[tuple[str, Key]], int]:
    edges: set[tuple[str, Key]] = set()
    bad = 0
    for path in files:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        for file_name, info in data.items():
            own = own_key(file_name)
            for target in info.get("citations") or []:
                parts = target.split(",")
                key = make_key(parts[1], parts[2], parts[3]) if len(parts) == 4 else None
                if key is None:
                    bad += 1
                elif key != own:
                    edges.add((file_name, key))
    return edges, bad


def _tjcas_edges(path: Path) -> tuple[set[tuple[str, Key]], int]:
    edges: set[tuple[str, Key]] = set()
    bad = 0
    if not path.exists():
        return edges, bad
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            file_name = row["entry"].rsplit("/", 1)[-1]
            key = make_key(row["year"], row["code"], row["number"])
            if key is None:
                bad += 1
            elif key != own_key(file_name):
                edges.add((file_name, key))
    return edges, bad


def compare_month(
    month: str, jurix_files: list[str], tjcas_csv: Path, n_samples: int, seed: int
) -> dict[str, object]:
    J, bad_j = _jurix_edges(jurix_files)
    T_all, bad_t = _tjcas_edges(tjcas_csv)
    J_corpus = {e for e in J if e[1] in _CORPUS_KEYS}
    T = {e for e in T_all if e[1] in _CORPUS_KEYS}
    both = T & J
    extra = T - J
    missed = J_corpus - T_all

    rng = random.Random(f"{seed}-{month}")
    counts = Counter(
        J=len(J),
        J_corpus=len(J_corpus),
        T_all=len(T_all),
        T=len(T),
        T_and_J=len(both),
        T_all_and_J=len(T_all & J),
        extra=len(extra),
        missed_corpus=len(missed),
        bad_jurix_ids=bad_j,
        bad_tjcas_rows=bad_t,
    )
    return {
        "month": month,
        "counts": counts,
        "sources_J": {s for s, _ in J},
        "sources_T": {s for s, _ in T},
        "keys_J": {k for _, k in J},
        "keys_J_corpus": {k for _, k in J_corpus},
        "keys_T": {k for _, k in T},
        "sample_extra": rng.sample(sorted(extra), min(n_samples, len(extra))),
        "sample_missed": rng.sample(sorted(missed), min(n_samples, len(missed))),
    }


def _ratio(a: int, b: int) -> float | None:
    return a / b if b else None


def compare(
    jurix_dir: Path,
    citations_dir: Path,
    corpus_db: Path,
    workers: int = 2,
    n_samples: int = 30,
    seed: int = 0,
    months: list[str] | None = None,
    log=print,
) -> dict[str, object]:
    global _CORPUS_KEYS
    log(f"loading corpus keys from {corpus_db}")
    _CORPUS_KEYS = load_corpus_keys(corpus_db)
    log(f"{len(_CORPUS_KEYS):,} distinct corpus keys")

    by_month: dict[str, list[str]] = defaultdict(list)
    for path in glob.glob(str(jurix_dir / "result_*.json")):
        by_month[Path(path).name.split("_", 2)[1]].append(path)
    tjcas_months = {p.name.removesuffix(".csv.gz") for p in citations_dir.glob("*.csv.gz")}
    all_months = sorted(set(by_month) | tjcas_months)
    if months:
        all_months = [m for m in all_months if m in set(months)]
    log(f"{len(all_months)} months; JURIX-only {sorted(set(by_month) - tjcas_months)}, "
        f"TJCAS-only {sorted(tjcas_months - set(by_month))}")

    total: Counter[str] = Counter()
    sources_J: set[str] = set()
    sources_T: set[str] = set()
    keys_J: set[Key] = set()
    keys_J_corpus: set[Key] = set()
    keys_T: set[Key] = set()
    pool_extra: list = []
    pool_missed: list = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(compare_month, m, by_month.get(m, []),
                        citations_dir / f"{m}.csv.gz", n_samples, seed)
            for m in all_months
        ]
        for index, future in enumerate(futures, start=1):
            part = future.result()
            month = part["month"]
            total.update(part["counts"])
            sources_J |= {(month, s) for s in part["sources_J"]}
            sources_T |= {(month, s) for s in part["sources_T"]}
            keys_J |= part["keys_J"]
            keys_J_corpus |= part["keys_J_corpus"]
            keys_T |= part["keys_T"]
            pool_extra += [(month, s, k) for s, k in part["sample_extra"]]
            pool_missed += [(month, s, k) for s, k in part["sample_missed"]]
            if index % 20 == 0 or index == len(futures):
                log(f"[{index}/{len(futures)}] {month}: J={total['J']:,} T={total['T']:,} "
                    f"T∩J={total['T_and_J']:,}")

    rng = random.Random(seed)
    fmt = lambda rows: [
        {"month": m, "source": s, "cited_key": ",".join(k)}
        for m, s, k in rng.sample(rows, min(n_samples, len(rows)))
    ]
    return {
        "counts": dict(total),
        "metrics": {
            "recall_vs_jurix": _ratio(total["T_and_J"], total["J"]),
            "recall_vs_jurix_corpus": _ratio(total["T_and_J"], total["J_corpus"]),
            "precision_vs_jurix": _ratio(total["T_and_J"], total["T"]),
            "extra_edges": total["extra"],
            "extra_share_of_T": _ratio(total["extra"], total["T"]),
            "T_over_J_corpus": _ratio(total["T"], total["J_corpus"]),
            "recall_vs_jurix_regex_only": _ratio(total["T_all_and_J"], total["J"]),
        },
        "distinct": {
            "sources_J": len(sources_J),
            "sources_T": len(sources_T),
            "sources_T_only": len(sources_T - sources_J),
            "cited_keys_J": len(keys_J),
            "cited_keys_J_corpus": len(keys_J_corpus),
            "cited_keys_T": len(keys_T),
            "cited_keys_T_only": len(keys_T - keys_J),
        },
        "samples": {"extra_T_not_J": fmt(pool_extra), "missed_J_corpus_not_T_all": fmt(pool_missed)},
    }
