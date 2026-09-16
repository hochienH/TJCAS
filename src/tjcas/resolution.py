"""Stage 3 orchestration: disambiguate the citations that map to several files.

Full text is never kept for the whole corpus, so the texts needed here are
fetched on demand: occurrences are grouped by the archive they live in, each
archive is streamed once out of ``rar p``, and only the rows that matter are
kept — the citing contexts (±window characters) and the full text of candidate
files, both inside a single SQLite work database.
"""

from __future__ import annotations

import csv
import gzip
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from tjcas.candidates import CANDIDATES_DB
from tjcas.citations import judgment_text
from tjcas.config import CITATIONS_DIR, RESOLUTION_DIR, get_data_route
from tjcas.patterns import normalize
from tjcas.sampling import stream_entries
from tjcas.similarity import METHODS, agreement, rank

RESOLUTION_DB = RESOLUTION_DIR / "resolution.sqlite"
PREDICTIONS_CSV = RESOLUTION_DIR / "predictions.csv.gz"
AGREEMENT_CSV = RESOLUTION_DIR / "method_agreement.csv"
DEFAULT_WINDOW = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS occurrences (
    id          INTEGER PRIMARY KEY,
    src_archive TEXT NOT NULL,
    src_entry   TEXT NOT NULL,
    year        TEXT NOT NULL,
    code        TEXT NOT NULL,
    number      TEXT NOT NULL,
    start       INTEGER NOT NULL,
    end         INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS contexts (
    occurrence_id INTEGER PRIMARY KEY,
    text          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_texts (
    archive TEXT NOT NULL,
    entry   TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (archive, entry)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS candidates (
    year    TEXT NOT NULL,
    code    TEXT NOT NULL,
    number  TEXT NOT NULL,
    archive TEXT NOT NULL,
    entry   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS predictions (
    occurrence_id INTEGER NOT NULL,
    method        TEXT NOT NULL,
    archive       TEXT NOT NULL,
    entry         TEXT NOT NULL,
    score         REAL NOT NULL,
    margin        REAL NOT NULL
);
"""
_INDEXES = [
    "CREATE INDEX IF NOT EXISTS occurrences_src ON occurrences (src_archive)",
    "CREATE INDEX IF NOT EXISTS occurrences_key ON occurrences (year, code, number)",
    "CREATE INDEX IF NOT EXISTS candidates_key ON candidates (year, code, number)",
]


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode = OFF")
    connection.execute("PRAGMA synchronous = OFF")
    return connection


def _ambiguous_keys(
    candidates_db: Path, limit_keys: int | None
) -> dict[tuple[str, str, str], list[tuple[str, str]]]:
    with sqlite3.connect(f"file:{candidates_db}?mode=ro", uri=True) as connection:
        query = (
            "SELECT year, code, number FROM keys WHERE n_candidates >= 2 "
            "ORDER BY occurrences DESC"
        )
        if limit_keys is not None:
            query += f" LIMIT {int(limit_keys)}"
        keys = connection.execute(query).fetchall()
        selected = set(keys)
        mapping: dict[tuple[str, str, str], list[tuple[str, str]]] = defaultdict(list)
        cursor = connection.execute(
            "SELECT year, code, number, archive, entry FROM key_candidates"
        )
        for year, code, number, archive, entry in cursor:
            key = (year, code, number)
            if key in selected:
                mapping[key].append((archive, entry))
    return mapping


def build_occurrences(
    citations_dir: Path = CITATIONS_DIR,
    candidates_db: Path = CANDIDATES_DB,
    db_path: Path = RESOLUTION_DB,
    limit_keys: int | None = None,
    max_per_key: int | None = None,
    verbose: bool = True,
) -> int:
    """Collect the stage-1 citation occurrences whose key is ambiguous."""
    mapping = _ambiguous_keys(candidates_db, limit_keys)
    if verbose:
        print(f"ambiguous keys selected: {len(mapping):,}", flush=True)
    db_path.unlink(missing_ok=True)
    files = sorted(citations_dir.glob("*.csv.gz"))
    with _connect(db_path) as connection:
        connection.executescript(_SCHEMA)
        connection.executemany(
            "INSERT INTO candidates VALUES (?, ?, ?, ?, ?)",
            [(*key, archive, entry) for key, refs in mapping.items() for archive, entry in refs],
        )
        for index, path in enumerate(files, start=1):
            archive = path.name[: -len(".csv.gz")]
            rows = []
            with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    key = (row["year"], row["code"], row["number"])
                    if key in mapping:
                        rows.append(
                            (archive, row["entry"], *key, int(row["start"]), int(row["end"]))
                        )
            connection.executemany(
                "INSERT INTO occurrences "
                "(src_archive, src_entry, year, code, number, start, end) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            if verbose:
                print(f"[{index}/{len(files)}] {archive}: {len(rows):,} occurrences", flush=True)
        if max_per_key is not None:
            connection.execute(
                "DELETE FROM occurrences WHERE id IN ("
                "  SELECT id FROM ("
                "    SELECT id, ROW_NUMBER() OVER ("
                "      PARTITION BY year, code, number ORDER BY id) AS rank"
                "    FROM occurrences"
                "  ) WHERE rank > ?)",
                (int(max_per_key),),
            )
        for statement in _INDEXES:
            connection.execute(statement)
        return connection.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]


def _read_judgments(archive: Path, entries: set[str]) -> Iterator[tuple[str, str]]:
    """Stream ``archive`` once and yield the wanted entries as ``(name, JFULL)``."""
    remaining = set(entries)
    for entry, payload in stream_entries(archive):
        if entry.name not in remaining:
            continue
        remaining.discard(entry.name)
        full_text = judgment_text(payload)
        if full_text is not None:
            yield entry.name, full_text
        if not remaining:
            return


def _context_worker(
    archive: Path,
    spans: dict[str, list[tuple[int, int, int]]],
    window: int,
) -> list[tuple[int, str]]:
    results: list[tuple[int, str]] = []
    for entry, full_text in _read_judgments(archive, set(spans)):
        text = normalize(full_text)
        for occurrence_id, start, end in spans[entry]:
            results.append((occurrence_id, text[max(0, start - window) : end + window]))
    return results


def _candidate_worker(archive: Path, entries: set[str]) -> list[tuple[str, str, bytes]]:
    return [
        (archive.stem, entry, gzip.compress(normalize(full_text).encode("utf-8"), 1))
        for entry, full_text in _read_judgments(archive, entries)
    ]


def _run_archive_tasks(worker, tasks, workers, verbose, label):
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, *task): task[0] for task in tasks}
        for done, future in enumerate(as_completed(futures), start=1):
            archive = futures[future]
            payload = future.result()
            if verbose:
                print(
                    f"[{done}/{len(tasks)}] {label} {archive.name}: {len(payload):,} rows",
                    flush=True,
                )
            yield payload


def fetch_contexts(
    db_path: Path = RESOLUTION_DB,
    window: int = DEFAULT_WINDOW,
    workers: int = 32,
    verbose: bool = True,
) -> int:
    """Re-stream the citing judgments and store ±``window`` characters around each span."""
    route = get_data_route()
    with _connect(db_path) as connection:
        connection.execute("DELETE FROM contexts")
        grouped: dict[str, dict[str, list[tuple[int, int, int]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        cursor = connection.execute(
            "SELECT id, src_archive, src_entry, start, end FROM occurrences"
        )
        for occurrence_id, archive, entry, start, end in cursor:
            grouped[archive][entry].append((occurrence_id, start, end))
        tasks = [
            (route / f"{archive}.rar", dict(spans), window)
            for archive, spans in sorted(grouped.items())
        ]
        total = 0
        for payload in _run_archive_tasks(
            _context_worker, tasks, workers, verbose, "contexts"
        ):
            connection.executemany("INSERT OR REPLACE INTO contexts VALUES (?, ?)", payload)
            total += len(payload)
        return total


def fetch_candidate_texts(
    db_path: Path = RESOLUTION_DB,
    workers: int = 32,
    verbose: bool = True,
) -> int:
    """Re-stream every candidate judgment and cache its normalized full text."""
    route = get_data_route()
    with _connect(db_path) as connection:
        grouped: dict[str, set[str]] = defaultdict(set)
        cursor = connection.execute(
            "SELECT DISTINCT c.archive, c.entry FROM candidates c "
            "JOIN occurrences o ON o.year = c.year AND o.code = c.code "
            "AND o.number = c.number"
        )
        for archive, entry in cursor:
            grouped[archive].add(entry)
        tasks = [
            (route / f"{archive}.rar", entries)
            for archive, entries in sorted(grouped.items())
        ]
        total = 0
        for payload in _run_archive_tasks(
            _candidate_worker, tasks, workers, verbose, "candidates"
        ):
            connection.executemany(
                "INSERT OR REPLACE INTO candidate_texts VALUES (?, ?, ?)", payload
            )
            total += len(payload)
        return total


def resolve(db_path: Path = RESOLUTION_DB, verbose: bool = True) -> int:
    """Run all three similarity methods on every ambiguous occurrence."""
    with _connect(db_path) as connection:
        connection.execute("DELETE FROM predictions")
        keys = connection.execute(
            "SELECT DISTINCT year, code, number FROM occurrences"
        ).fetchall()
        resolved = 0
        for index, key in enumerate(keys, start=1):
            refs = connection.execute(
                "SELECT archive, entry FROM candidates "
                "WHERE year = ? AND code = ? AND number = ?",
                key,
            ).fetchall()
            texts: list[tuple[str, str, str]] = []
            for archive, entry in refs:
                row = connection.execute(
                    "SELECT payload FROM candidate_texts WHERE archive = ? AND entry = ?",
                    (archive, entry),
                ).fetchone()
                if row is not None:
                    texts.append((archive, entry, gzip.decompress(row[0]).decode("utf-8")))
            if len(texts) < 2:
                continue
            occurrences = connection.execute(
                "SELECT o.id, c.text FROM occurrences o JOIN contexts c "
                "ON c.occurrence_id = o.id "
                "WHERE o.year = ? AND o.code = ? AND o.number = ?",
                key,
            ).fetchall()
            rows = []
            for occurrence_id, context in occurrences:
                for choice in rank(context, [text for _, _, text in texts]).values():
                    if choice.index < 0:
                        continue
                    archive, entry, _ = texts[choice.index]
                    rows.append(
                        (occurrence_id, choice.method, archive, entry, choice.score, choice.margin)
                    )
            connection.executemany(
                "INSERT INTO predictions VALUES (?, ?, ?, ?, ?, ?)", rows
            )
            resolved += len(occurrences)
            if verbose and index % 200 == 0:
                print(f"[{index}/{len(keys)}] keys resolved, {resolved:,} occurrences", flush=True)
        return resolved


def report(
    db_path: Path = RESOLUTION_DB,
    predictions_csv: Path = PREDICTIONS_CSV,
    agreement_csv: Path = AGREEMENT_CSV,
) -> dict[str, float]:
    """Export per-occurrence predictions and the cross-method agreement rates."""
    predictions_csv.parent.mkdir(parents=True, exist_ok=True)
    names = sorted(METHODS)
    decisions: list[dict[str, str]] = []
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        cursor = connection.execute(
            "SELECT o.id, o.src_archive, o.src_entry, o.year, o.code, o.number, "
            "p.method, p.archive, p.entry, p.score, p.margin "
            "FROM occurrences o JOIN predictions p ON p.occurrence_id = o.id "
            "ORDER BY o.id, p.method"
        )
        with gzip.open(predictions_csv, "wt", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["occurrence_id", "src_archive", "src_entry", "year", "code", "number"]
                + [
                    f"{name}_{field}"
                    for name in names
                    for field in ("archive", "entry", "score", "margin")
                ]
            )
            for identity, picks in _group_by_occurrence(cursor):
                if len(picks) != len(names):
                    continue
                writer.writerow(
                    identity
                    + [value for name in names for value in picks[name]]
                )
                decisions.append({name: picks[name][:2] for name in names})
    rates = agreement(decisions)
    with agreement_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["comparison", "agreement_rate", "occurrences"])
        for label, rate in rates.items():
            writer.writerow([label, f"{rate:.4f}", len(decisions)])
    return rates


def _group_by_occurrence(
    cursor: sqlite3.Cursor,
) -> Iterator[tuple[list, dict[str, tuple[str, str, float, float]]]]:
    identity: list = []
    picks: dict[str, tuple[str, str, float, float]] = {}
    for row in cursor:
        current = list(row[:6])
        if current != identity:
            if identity:
                yield identity, picks
            identity, picks = current, {}
        picks[row[6]] = (row[7], row[8], row[9], row[10])
    if identity:
        yield identity, picks
