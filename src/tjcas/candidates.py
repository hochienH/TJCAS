"""Stage 2: label every distinct citation key with its candidate count.

A citation only pins down ``年 / 字別 / 案號``; the court is unknown. Looking that
triple up in the filename index gives three outcomes, which is what this stage
tallies:

* ``none`` — no such judgment in the open data (pre-1996, or a prosecution file)
* ``unique`` — exactly one file, so the annotation is already settled
* ``ambiguous`` — two or more files, which stage 3 disambiguates by similarity

Keys are deduplicated first: the corpus cites the same judgment over and over,
so resolving distinct keys instead of occurrences cuts the index queries by an
order of magnitude.
"""

from __future__ import annotations

import csv
import gzip
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from tjcas.config import CANDIDATES_DIR, CITATIONS_DIR, CORPUS_DB
from tjcas.corpus import CorpusIndex

CANDIDATES_DB = CANDIDATES_DIR / "candidates.sqlite"
KEYS_CSV = CANDIDATES_DIR / "citation_keys.csv.gz"
SUMMARY_CSV = CANDIDATES_DIR / "candidate_summary.csv"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    year          TEXT NOT NULL,
    code          TEXT NOT NULL,
    number        TEXT NOT NULL,
    occurrences   INTEGER NOT NULL DEFAULT 0,
    n_candidates  INTEGER,
    PRIMARY KEY (year, code, number)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS key_candidates (
    year    TEXT NOT NULL,
    code    TEXT NOT NULL,
    number  TEXT NOT NULL,
    archive TEXT NOT NULL,
    entry   TEXT NOT NULL
);
"""
_CANDIDATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS key_candidates_key "
    "ON key_candidates (year, code, number)"
)


def bucket_of(n_candidates: int) -> str:
    if n_candidates == 0:
        return "none"
    return "unique" if n_candidates == 1 else "ambiguous"


def _count_file(path: Path) -> list[tuple[str, str, str, int]]:
    counter: Counter[tuple[str, str, str]] = Counter()
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            counter[(row["year"], row["code"], row["number"])] += 1
    return [(year, code, number, count) for (year, code, number), count in counter.items()]


def collect_keys(
    citations_dir: Path = CITATIONS_DIR,
    db_path: Path = CANDIDATES_DB,
    workers: int = 8,
    verbose: bool = True,
) -> int:
    """Aggregate stage-1 output into distinct keys with occurrence counts."""
    files = sorted(citations_dir.glob("*.csv.gz"))
    if not files:
        raise RuntimeError(f"no stage-1 citation files in {citations_dir}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.executescript(_SCHEMA)
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for index, rows in enumerate(pool.map(_count_file, files), start=1):
                connection.executemany(
                    "INSERT INTO keys (year, code, number, occurrences) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (year, code, number) "
                    "DO UPDATE SET occurrences = occurrences + excluded.occurrences",
                    rows,
                )
                if verbose:
                    print(
                        f"[{index}/{len(files)}] {files[index - 1].name}: "
                        f"{len(rows):>7,} distinct keys",
                        flush=True,
                    )
        return connection.execute("SELECT COUNT(*) FROM keys").fetchone()[0]


def _resolve_chunk(
    chunk: list[tuple[str, str, str]], db_path: Path
) -> list[tuple[tuple[str, str, str], list[tuple[str, str]]]]:
    with CorpusIndex(db_path) as index:
        return [
            (key, [(ref.archive, ref.entry) for ref in index.lookup(*key)])
            for key in chunk
        ]


def _chunks(items: Iterable[tuple[str, str, str]], size: int) -> Iterator[list]:
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def resolve_keys(
    db_path: Path = CANDIDATES_DB,
    corpus_db: Path = CORPUS_DB,
    workers: int = 8,
    chunk_size: int = 20_000,
    verbose: bool = True,
) -> Counter[str]:
    """Fill in ``n_candidates`` and the candidate file list for every key."""
    buckets: Counter[str] = Counter()
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("DELETE FROM key_candidates")
        pending = connection.execute(
            "SELECT year, code, number FROM keys WHERE n_candidates IS NULL"
        ).fetchall()
        done = 0
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_resolve_chunk, chunk, corpus_db)
                for chunk in _chunks(pending, chunk_size)
            ]
            for future in futures:
                results = future.result()
                connection.executemany(
                    "UPDATE keys SET n_candidates = ? WHERE year = ? AND code = ? "
                    "AND number = ?",
                    [(len(refs), *key) for key, refs in results],
                )
                connection.executemany(
                    "INSERT INTO key_candidates VALUES (?, ?, ?, ?, ?)",
                    [
                        (*key, archive, entry)
                        for key, refs in results
                        for archive, entry in refs
                    ],
                )
                for key, refs in results:
                    buckets[bucket_of(len(refs))] += 1
                done += len(results)
                if verbose:
                    print(f"resolved {done:,}/{len(pending):,} keys", flush=True)
        connection.execute(_CANDIDATE_INDEX)
    return buckets


def export(
    db_path: Path = CANDIDATES_DB,
    keys_csv: Path = KEYS_CSV,
    summary_csv: Path = SUMMARY_CSV,
) -> dict[str, dict[str, int]]:
    """Write the labelled keys and a keys/occurrences breakdown per bucket."""
    keys_csv.parent.mkdir(parents=True, exist_ok=True)
    summary: dict[str, dict[str, int]] = {}
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        with gzip.open(keys_csv, "wt", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["year", "code", "number", "occurrences", "n_candidates", "bucket"]
            )
            cursor = connection.execute(
                "SELECT year, code, number, occurrences, n_candidates FROM keys "
                "ORDER BY occurrences DESC"
            )
            for year, code, number, occurrences, n_candidates in cursor:
                label = bucket_of(n_candidates or 0)
                writer.writerow(
                    [year, code, number, occurrences, n_candidates, label]
                )
                stats = summary.setdefault(label, {"keys": 0, "occurrences": 0})
                stats["keys"] += 1
                stats["occurrences"] += occurrences
    with summary_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["bucket", "keys", "occurrences"])
        for label in ("none", "unique", "ambiguous"):
            stats = summary.get(label, {"keys": 0, "occurrences": 0})
            writer.writerow([label, stats["keys"], stats["occurrences"]])
    return summary


def iter_ambiguous_keys(
    db_path: Path = CANDIDATES_DB, limit: int | None = None
) -> Iterator[tuple[tuple[str, str, str], list[tuple[str, str]]]]:
    """Yield ``((year, code, number), [(archive, entry), ...])`` for ambiguous keys."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        query = (
            "SELECT year, code, number FROM keys WHERE n_candidates >= 2 "
            "ORDER BY occurrences DESC"
        )
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        for key in connection.execute(query).fetchall():
            refs = connection.execute(
                "SELECT archive, entry FROM key_candidates "
                "WHERE year = ? AND code = ? AND number = ?",
                key,
            ).fetchall()
            yield key, refs
