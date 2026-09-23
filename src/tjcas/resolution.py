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
import hashlib
import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from tjcas.candidates import CANDIDATES_DB
from tjcas.citations import judgment_text
from tjcas.config import CITATIONS_DIR, RESOLUTION_DIR, get_data_route
from tjcas.patterns import normalize
from tjcas.sampling import stream_entries
from tjcas.similarity import METHODS, agreement, majority, rank

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


_CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS done_keys (
    year     TEXT NOT NULL,
    code     TEXT NOT NULL,
    number   TEXT NOT NULL,
    resolved INTEGER NOT NULL,
    PRIMARY KEY (year, code, number)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS predictions (
    occurrence_id INTEGER NOT NULL,
    method        TEXT NOT NULL,
    archive       TEXT NOT NULL,
    entry         TEXT NOT NULL,
    score         REAL NOT NULL,
    margin        REAL NOT NULL
);
"""


def checkpoint_path(db_path: Path) -> Path:
    """Where ``resolve`` keeps the work it has finished, next to the work database."""
    return db_path.with_name(f"{db_path.stem}.checkpoint.sqlite")


def open_checkpoint(path: Path) -> sqlite3.Connection:
    """The checkpoint database, created on first use.

    Only the parent process writes here and workers never read it, so WAL is
    safe, and a batch's predictions commit together with the keys they finish:
    a process killed at any point loses the batches in flight and nothing else.
    """
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.executescript(_CHECKPOINT_SCHEMA)
    return connection


def key_fingerprint(keys: Sequence[tuple[str, str, str]]) -> str:
    """Identify a key set, so a checkpoint is never resumed against another one."""
    digest = hashlib.sha256()
    for key in sorted(keys):
        digest.update("\x1f".join(key).encode("utf-8"))
        digest.update(b"\x1e")
    return f"{len(keys)}:{digest.hexdigest()}"


def _resolve_batch(
    db_path: Path, keys: Sequence[tuple[str, str, str]]
) -> tuple[list[tuple], list[tuple[str, str, str, int]]]:
    """Score one batch of keys.

    Returns the prediction rows and, for every key of the batch, how many of its
    occurrences were scored: zero for a key left with fewer than two candidate
    texts, which is finished all the same.
    """
    rows: list[tuple] = []
    done: list[tuple[str, str, str, int]] = []
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for key in keys:
            texts = [
                (archive, entry, gzip.decompress(payload).decode("utf-8"))
                for archive, entry, payload in connection.execute(
                    "SELECT c.archive, c.entry, t.payload FROM candidates c "
                    "JOIN candidate_texts t "
                    "ON t.archive = c.archive AND t.entry = c.entry "
                    "WHERE c.year = ? AND c.code = ? AND c.number = ? "
                    "ORDER BY c.archive, c.entry",
                    key,
                )
            ]
            if len(texts) < 2:
                done.append((*key, 0))
                continue
            occurrences = connection.execute(
                "SELECT o.id, c.text FROM occurrences o JOIN contexts c "
                "ON c.occurrence_id = o.id "
                "WHERE o.year = ? AND o.code = ? AND o.number = ?",
                key,
            ).fetchall()
            for occurrence_id, context in occurrences:
                for choice in rank(context, [text for _, _, text in texts]).values():
                    if choice.index < 0:
                        continue
                    archive, entry, _ = texts[choice.index]
                    rows.append(
                        (occurrence_id, choice.method, archive, entry, choice.score, choice.margin)
                    )
            done.append((*key, len(occurrences)))
    finally:
        connection.close()
    return rows, done


def resolve(
    db_path: Path = RESOLUTION_DB,
    workers: int = 32,
    batch_size: int = 200,
    verbose: bool = True,
    resume: bool = True,
) -> int:
    """Run all three similarity methods on every ambiguous occurrence.

    Scoring dominates this stage — a decision with the median six candidates
    costs a quarter of a second — and keys are independent, so they are scored
    in parallel. Workers only ever read the work database. The parent records
    each finished batch in a separate checkpoint database (``checkpoint_path``)
    and copies the predictions over once every key is done, because the work
    database runs with journal_mode OFF, where a concurrent writer locks readers
    out instead of making them wait.

    The full corpus takes days on one machine, long enough for the parent to be
    killed partway; it has been, by SIGKILL, with nothing in the log. With
    ``resume`` a rerun skips every key the checkpoint already holds. The
    checkpoint remembers which key set it was written for and refuses to be
    resumed against another; ``resume=False`` discards it and starts over.

    Returns the number of occurrences scored, including those a resumed run
    found already done.
    """
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        keys = connection.execute(
            "SELECT DISTINCT year, code, number FROM occurrences"
        ).fetchall()
    total_keys = len(keys)
    fingerprint = key_fingerprint(keys)
    path = checkpoint_path(db_path)
    checkpoint = open_checkpoint(path)
    try:
        with checkpoint:
            if not resume:
                for table in ("meta", "done_keys", "predictions"):
                    checkpoint.execute(f"DELETE FROM {table}")
            stored = checkpoint.execute(
                "SELECT value FROM meta WHERE name = 'keys'"
            ).fetchone()
            if stored is None:
                checkpoint.execute("INSERT INTO meta VALUES ('keys', ?)", (fingerprint,))
            elif stored[0] != fingerprint:
                raise RuntimeError(
                    f"{path} was written for another set of keys; "
                    "rerun with resume=False to discard it"
                )
        done = set(checkpoint.execute("SELECT year, code, number FROM done_keys"))
        resolved = checkpoint.execute(
            "SELECT COALESCE(SUM(resolved), 0) FROM done_keys"
        ).fetchone()[0]
        todo = [key for key in keys if key not in done]
        # The parent idles for days while its workers score, which makes it the
        # kind of process an OOM killer picks; hold on to no more than it needs.
        del keys, done
        batches = [todo[index : index + batch_size] for index in range(0, len(todo), batch_size)]
        if verbose:
            print(
                f"{total_keys:,} keys, {total_keys - len(todo):,} already done; "
                f"{len(todo):,} left in {len(batches):,} batches, {workers} workers",
                flush=True,
            )
        del todo
        with ProcessPoolExecutor(max_workers=workers) as pool:
            pending = {pool.submit(_resolve_batch, db_path, batch) for batch in batches}
            for finished, future in enumerate(as_completed(pending), start=1):
                rows, finished_keys = future.result()
                with checkpoint:
                    checkpoint.executemany(
                        "INSERT INTO predictions VALUES (?, ?, ?, ?, ?, ?)", rows
                    )
                    checkpoint.executemany(
                        "INSERT INTO done_keys VALUES (?, ?, ?, ?)", finished_keys
                    )
                resolved += sum(count for *_, count in finished_keys)
                # A future holds on to its result, so keeping all 8,066 of them in
                # one container keeps every batch's rows alive until the run ends,
                # long after they have been checkpointed. Over fifteen million
                # occurrences that is more than ten gigabytes the parent never
                # gives back.
                pending.discard(future)
                del rows, finished_keys, future
                if verbose and finished % 100 == 0:
                    print(
                        f"[{finished}/{len(batches)}] batches, {resolved:,} occurrences",
                        flush=True,
                    )
        stored_keys = checkpoint.execute("SELECT COUNT(*) FROM done_keys").fetchone()[0]
    finally:
        checkpoint.close()
    if stored_keys != total_keys:
        raise RuntimeError(f"{path} holds {stored_keys:,} of {total_keys:,} keys")
    connection = _connect(db_path)
    try:
        connection.execute("ATTACH DATABASE ? AS checkpoint", (str(path),))
        with connection:
            connection.execute("DELETE FROM predictions")
            connection.execute(
                "INSERT INTO predictions SELECT occurrence_id, method, archive, entry, "
                "score, margin FROM checkpoint.predictions"
            )
        connection.execute("DETACH DATABASE checkpoint")
    finally:
        connection.close()
    return resolved


def _case_id(entry: str) -> str:
    """Identify the *case* behind a file: the ``court,year,code,number`` prefix.

    One case can be published as several files that differ only in the tail of
    the name (``...,20250318,7.json`` and ``...,20250318,6.json``), and those
    files need not sit in the same monthly archive, so neither the tail nor the
    archive belongs in this key. The court does: drop it and every candidate of
    an ambiguous key collapses onto one id, making agreement trivially 1.
    """
    name = entry.rsplit("/", 1)[-1].removesuffix(".json")
    return ",".join(name.split(",")[:4])


def report(
    db_path: Path = RESOLUTION_DB,
    predictions_csv: Path = PREDICTIONS_CSV,
    agreement_csv: Path = AGREEMENT_CSV,
) -> dict[str, dict[str, float]]:
    """Export per-occurrence predictions and the cross-method agreement rates.

    Agreement is reported under two readings of "the methods picked the same
    judgment": ``strict`` compares the files themselves, ``loose`` compares the
    cases behind them (see ``_case_id``). Coarsening can only merge picks, so
    every ``loose`` agreement rate is >= its ``strict`` counterpart and
    ``no_majority`` can only fall.

    Each row also carries what the methods settled on under either reading and
    how they got there — unanimously, by majority, or not at all. A reading can
    reach a majority the other misses, so the two get their own columns.
    """
    predictions_csv.parent.mkdir(parents=True, exist_ok=True)
    names = sorted(METHODS)
    decisions: list[dict[str, tuple[str, str]]] = []
    loose_decisions: list[dict[str, str]] = []
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
                + [
                    "majority_strict_archive",
                    "majority_strict_entry",
                    "decision_strict",
                    "majority_loose",
                    "decision_loose",
                ]
            )
            for identity, picks in _group_by_occurrence(cursor):
                if len(picks) != len(names):
                    continue
                strict = {name: picks[name][:2] for name in names}
                loose = {name: _case_id(entry) for name, (_, entry) in strict.items()}
                strict_pick, strict_mode = majority(strict)
                loose_pick, loose_mode = majority(loose)
                writer.writerow(
                    identity
                    + [value for name in names for value in picks[name]]
                    # no majority means no winner to name, which a CSV writes empty
                    + [*(strict_pick or ("", "")), strict_mode, loose_pick or "", loose_mode]
                )
                decisions.append(strict)
                loose_decisions.append(loose)
    views = {"strict": decisions, "loose": loose_decisions}
    rates = {view: agreement(rows) for view, rows in views.items()}
    with agreement_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["view", "comparison", "agreement_rate", "occurrences"])
        for view, view_rates in rates.items():
            for label, rate in view_rates.items():
                writer.writerow([view, label, f"{rate:.4f}", len(decisions)])
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
