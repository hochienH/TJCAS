"""Court-level agreement between TJCAS and the JURIX regex baseline on the edges both found.

``jurix.compare`` deliberately ignores the court: its edge is (source judgment file, (year, code,
number)). This module puts the court back for the edges in T ∩ J — the citations both sides read
the same 年字號 from — and asks whether they also name the same court.

  JURIX court  the code the regex read from the citation text (``TCDM,97,中簡,379`` -> ``TCDM``).
  TJCAS court  when every corpus document carrying the key sits in one court, that court
               (``single_court``: nothing to disambiguate, this only checks the key); otherwise the
               court of the stage-3 majority pick (``multi_court``), aggregated over the occurrences
               of the edge. ``strict`` takes the file-level majority, ``loose`` the case-level one
               (see ``resolution.report``). Several occurrences of one edge may pick different courts;
               the commonest wins, a tie is left undecided.

JURIX is silver, so every rate here is an agreement, not an accuracy. Two bounds frame the
multi-court rate: the share of edges whose JURIX court is not among the candidate courts at all
(the ceiling — those can never agree), and the agreement a uniformly random pick among the
candidate courts would reach (the floor).

Both sources partition by monthly archive, so months are compared independently and only the
counters are merged; a source is (month, file name), exactly as in ``jurix``.
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
from pathlib import Path

from tjcas.jurix import Key, _tjcas_edges, load_corpus_keys, make_key, own_key

Edge = tuple[str, str, Key]  # (month, source file name, cited key)
VIEWS = ("strict", "loose")

# Filled in the parent before the pools fork, so workers share them copy-on-write.
_CORPUS_KEYS: frozenset[Key] = frozenset()
_JURIX: dict[Edge, frozenset[str]] = {}
_PICKS: dict[Edge, tuple[Counter, Counter, list[int]]] = {}
_CORPUS_DB: Path = Path()


def court_of(entry: str) -> str:
    """``200201/臺灣高等法院臺南分院民事/TNHV,84,訴,15,20020108,1.json`` -> ``TNHV``."""
    return entry.rsplit("/", 1)[-1].split(",", 1)[0]


# --- phase 1: JURIX edges with their court -------------------------------------------------------

def _jurix_month(files: list[str]) -> list[tuple[str, Key, str]]:
    rows: list[tuple[str, Key, str]] = []
    for path in files:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        for file_name, info in data.items():
            own = own_key(file_name)
            for target in info.get("citations") or []:
                parts = target.split(",")
                if len(parts) != 4:
                    continue
                key = make_key(parts[1], parts[2], parts[3])
                if key is None or key == own or key not in _CORPUS_KEYS:
                    continue
                rows.append((file_name, key, parts[0]))
    return rows


def load_jurix(
    jurix_dir: Path, workers: int, months: list[str] | None, log
) -> dict[Edge, frozenset[str]]:
    by_month: dict[str, list[str]] = defaultdict(list)
    for path in glob.glob(str(jurix_dir / "result_*.json")):
        by_month[Path(path).name.split("_", 2)[1]].append(path)
    wanted = sorted(by_month) if not months else [m for m in sorted(by_month) if m in set(months)]
    edges: dict[Edge, set[str]] = defaultdict(set)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [(m, pool.submit(_jurix_month, by_month[m])) for m in wanted]
        for index, (month, future) in enumerate(futures, start=1):
            for source, key, court in future.result():
                edges[(month, source, key)].add(court)
            if index % 60 == 0 or index == len(futures):
                log(f"jurix [{index}/{len(futures)}] {month}: {len(edges):,} corpus edges")
    return {edge: frozenset(courts) for edge, courts in edges.items()}


# --- phase 2: the stage-3 picks for those edges -------------------------------------------------

def collect_picks(
    predictions_csv: Path, jurix: dict[Edge, frozenset[str]], log, limit: int | None = None
) -> dict[Edge, tuple[Counter, Counter, list[int]]]:
    """Per edge in J: Counter of strict pick courts, Counter of loose pick courts, [occurrences]."""
    picks: dict[Edge, tuple[Counter, Counter, list[int]]] = {}
    with gzip.open(predictions_csv, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        col = {name: index for index, name in enumerate(header)}
        i_archive, i_entry = col["src_archive"], col["src_entry"]
        i_year, i_code, i_number = col["year"], col["code"], col["number"]
        i_strict_entry, i_strict = col["majority_strict_entry"], col["decision_strict"]
        i_loose_pick, i_loose = col["majority_loose"], col["decision_loose"]
        n = 0
        for n, row in enumerate(reader, start=1):
            if limit and n > limit:
                break
            if n % 1_000_000 == 0:
                log(f"predictions {n:,} rows, {len(picks):,} edges in J so far")
            key = make_key(row[i_year], row[i_code], row[i_number])
            if key is None:
                continue
            edge = (row[i_archive], row[i_entry].rsplit("/", 1)[-1], key)
            if edge not in jurix:
                continue
            entry = picks.get(edge)
            if entry is None:
                entry = picks[edge] = (Counter(), Counter(), [0])
            entry[2][0] += 1
            if row[i_strict] != "none":
                entry[0][court_of(row[i_strict_entry])] += 1
            if row[i_loose] != "none":
                entry[1][row[i_loose_pick].split(",", 1)[0]] += 1
    log(f"predictions: {n:,} rows read")
    return picks


# --- phase 3: per month, the edges both found ---------------------------------------------------

def _key_courts(connection: sqlite3.Connection, cache: dict[Key, frozenset[str]], key: Key) -> frozenset[str]:
    courts = cache.get(key)
    if courts is None:
        rows = connection.execute(
            "SELECT DISTINCT court FROM documents WHERE year=? AND code_norm=? AND number=?", key
        )
        courts = cache[key] = frozenset(court for (court,) in rows)
    return courts


def _bucket(n_courts: int) -> str:
    return str(n_courts) if n_courts <= 3 else ("4-5" if n_courts <= 5 else "6+")


def compare_month(month: str, citations_csv: Path, n_samples: int) -> tuple[Counter, dict[str, list]]:
    T, _ = _tjcas_edges(citations_csv)
    counts: Counter[str] = Counter()
    samples: dict[str, list] = defaultdict(list)
    connection = sqlite3.connect(f"file:{_CORPUS_DB}?mode=ro", uri=True)
    cache: dict[Key, frozenset[str]] = {}

    def sample(label: str, **fields) -> None:
        if len(samples[label]) < n_samples:
            samples[label].append({"month": month, **fields})

    for source, key in sorted(T):
        edge = (month, source, key)
        jurix_courts = _JURIX.get(edge)
        if jurix_courts is None:
            continue
        counts["edges"] += 1
        if len(jurix_courts) > 1:
            counts["jurix_multi_court"] += 1
        candidates = _key_courts(connection, cache, key)
        cited = ",".join(key)
        if len(candidates) == 1:
            (court,) = candidates
            counts["single_court"] += 1
            if court in jurix_courts:
                counts["single_court_agree"] += 1
            else:
                sample("single_court_disagree", source=source, cited_key=cited,
                       jurix=sorted(jurix_courts), corpus=court)
            continue

        bucket = _bucket(len(candidates))
        counts["multi_court"] += 1
        counts[f"multi_court_n{bucket}"] += 1
        overlap = jurix_courts & candidates
        if not overlap:
            counts["multi_court_jurix_not_candidate"] += 1
            sample("jurix_not_candidate", source=source, cited_key=cited,
                   jurix=sorted(jurix_courts), candidates=sorted(candidates))
        counts["multi_court_random_x1e6"] += round(1e6 * len(overlap) / len(candidates))
        picks = _PICKS.get(edge)
        if picks is None:
            counts["multi_court_no_prediction"] += 1
            continue
        if picks[2][0] > 1:
            counts["multi_court_several_occurrences"] += 1
        for view, counter in zip(VIEWS, picks[:2]):
            if not counter:
                counts[f"{view}_no_majority"] += 1
                continue
            top = counter.most_common(2)
            if len(top) > 1 and top[0][1] == top[1][1]:
                counts[f"{view}_tie"] += 1
                continue
            if len(counter) > 1:
                counts[f"{view}_occurrences_disagree"] += 1
            court = top[0][0]
            counts[f"{view}_decided"] += 1
            counts[f"{view}_decided_n{bucket}"] += 1
            if court in jurix_courts:
                counts[f"{view}_agree"] += 1
                counts[f"{view}_agree_n{bucket}"] += 1
            else:
                sample(f"{view}_disagree", source=source, cited_key=cited, jurix=sorted(jurix_courts),
                       pick=court, picks=dict(counter), candidates=sorted(candidates))
    connection.close()
    return counts, samples


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def compare(
    jurix_dir: Path,
    citations_dir: Path,
    corpus_db: Path,
    predictions_csv: Path,
    workers: int = 4,
    n_samples: int = 30,
    seed: int = 0,
    months: list[str] | None = None,
    predictions_limit: int | None = None,
    log=print,
) -> dict[str, object]:
    global _CORPUS_KEYS, _JURIX, _PICKS, _CORPUS_DB
    _CORPUS_DB = corpus_db
    log(f"loading corpus keys from {corpus_db}")
    _CORPUS_KEYS = load_corpus_keys(corpus_db)
    log(f"{len(_CORPUS_KEYS):,} distinct corpus keys")

    _JURIX = load_jurix(jurix_dir, workers, months, log)
    log(f"JURIX: {len(_JURIX):,} corpus edges")
    _PICKS = collect_picks(predictions_csv, _JURIX, log, predictions_limit)
    log(f"predictions: {len(_PICKS):,} edges in J have stage-3 picks")

    all_months = sorted(p.name.removesuffix(".csv.gz") for p in citations_dir.glob("*.csv.gz"))
    if months:
        all_months = [m for m in all_months if m in set(months)]
    total: Counter[str] = Counter()
    pooled: dict[str, list] = defaultdict(list)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [
            (m, pool.submit(compare_month, m, citations_dir / f"{m}.csv.gz", n_samples))
            for m in all_months
        ]
        for index, (month, future) in enumerate(futures, start=1):
            counts, samples = future.result()
            total.update(counts)
            for label, rows in samples.items():
                pooled[label] += rows
            if index % 20 == 0 or index == len(futures):
                log(f"[{index}/{len(futures)}] {month}: edges={total['edges']:,} "
                    f"single={total['single_court']:,} multi={total['multi_court']:,} "
                    f"strict agree={total['strict_agree']:,}/{total['strict_decided']:,}")

    multi = total["multi_court"]
    metrics: dict[str, object] = {
        "edges_T_and_J": total["edges"],
        "single_court_share": _ratio(total["single_court"], total["edges"]),
        "single_court_agreement": _ratio(total["single_court_agree"], total["single_court"]),
        "multi_court_share": _ratio(multi, total["edges"]),
        "multi_court_ceiling": _ratio(multi - total["multi_court_jurix_not_candidate"], multi),
        "multi_court_random_floor": _ratio(total["multi_court_random_x1e6"] / 1e6, multi),
        "jurix_multi_court_share": _ratio(total["jurix_multi_court"], total["edges"]),
    }
    for view in VIEWS:
        decided = total[f"{view}_decided"]
        metrics[f"{view}_decided_share"] = _ratio(decided, multi)
        metrics[f"{view}_agreement_decided"] = _ratio(total[f"{view}_agree"], decided)
        metrics[f"{view}_agreement_all_multi"] = _ratio(total[f"{view}_agree"], multi)
        metrics[f"{view}_agreement_overall"] = _ratio(
            total["single_court_agree"] + total[f"{view}_agree"], total["edges"]
        )
        metrics[f"{view}_by_candidate_courts"] = {
            b: {
                "edges": total[f"multi_court_n{b}"],
                "decided": total[f"{view}_decided_n{b}"],
                "agreement_decided": _ratio(total[f"{view}_agree_n{b}"], total[f"{view}_decided_n{b}"]),
            }
            for b in ("2", "3", "4-5", "6+")
        }
    rng = random.Random(seed)
    return {
        "counts": dict(sorted(total.items())),
        "metrics": metrics,
        "samples": {
            label: rng.sample(rows, min(n_samples, len(rows))) for label, rows in sorted(pooled.items())
        },
    }
