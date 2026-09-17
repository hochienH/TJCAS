"""Global filename index of the corpus.

Every judgment file is named ``court,year,code,number,date,seq.json``, so the
`X年Y字第Z號` of a citation can be looked up directly against filenames without
ever decompressing anything. The court name is deliberately *not* part of the
key: extracting it from the citing text is unreliable (see README limitations),
which is exactly why ambiguous keys get resolved by full-text similarity later.

The index lives in SQLite because the corpus has tens of millions of entries —
too many to keep in a Python dict, and read-only SQLite connections are safe to
share across worker processes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from tjcas.codes import normalize_code
from tjcas.config import CORPUS_DB
from tjcas.sampling import iter_entries, list_archives

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    archive   TEXT NOT NULL,
    entry     TEXT NOT NULL,
    court     TEXT NOT NULL,
    year      TEXT NOT NULL,
    code      TEXT NOT NULL,
    code_norm TEXT NOT NULL,
    number    TEXT NOT NULL,
    jdate     TEXT NOT NULL,
    seq       TEXT NOT NULL
);
"""
_KEY_INDEX = (
    "CREATE INDEX IF NOT EXISTS documents_key ON documents (year, code_norm, number)"
)


@dataclass(frozen=True)
class DocumentRef:
    archive: str
    entry: str
    court: str
    year: str
    code: str
    code_norm: str
    number: str
    jdate: str
    seq: str

    @property
    def jid(self) -> str:
        parts = [self.court, self.year, self.code, self.number, self.jdate]
        return ",".join(parts + [self.seq] if self.seq else parts)


def parse_entry_name(archive: str, entry: str) -> DocumentRef | None:
    """Parse ``.../TPSM,114,台上,123,20250101,1.json`` into a DocumentRef."""
    name = entry.rsplit("/", 1)[-1]
    if not name.endswith(".json"):
        return None
    parts = name[: -len(".json")].split(",")
    if len(parts) < 5:
        return None
    court, year, code, number, jdate = parts[:5]
    seq = parts[5] if len(parts) > 5 else ""
    if not year.isdigit() or not number.isdigit():
        return None
    return DocumentRef(
        archive=archive,
        entry=entry,
        court=court,
        year=str(int(year)),
        code=code,
        code_norm=normalize_code(code),
        number=str(int(number)),
        jdate=jdate,
        seq=seq,
    )


def _archive_rows(archive: Path) -> list[tuple[str, ...]]:
    stem = archive.stem
    rows = []
    for entry in iter_entries(archive):
        ref = parse_entry_name(stem, entry.name)
        if ref is not None:
            rows.append(
                (ref.archive, ref.entry, ref.court, ref.year, ref.code, ref.code_norm,
                 ref.number, ref.jdate, ref.seq)
            )
    return rows


def build_index(
    archives: Iterable[Path] | None = None,
    db_path: Path = CORPUS_DB,
    workers: int = 32,
    verbose: bool = True,
) -> int:
    """Rebuild the filename index from scratch. Returns the number of documents."""
    targets = list(archives) if archives is not None else list_archives()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    total = 0
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.executescript(_SCHEMA)
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for index, rows in enumerate(pool.map(_archive_rows, targets), start=1):
                connection.executemany(
                    "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
                )
                total += len(rows)
                if verbose:
                    print(
                        f"[{index}/{len(targets)}] {targets[index - 1].name}: "
                        f"{len(rows):>7,} docs (total {total:,})",
                        flush=True,
                    )
        if verbose:
            print("building key index ...", flush=True)
        connection.execute(_KEY_INDEX)
    return total


class CorpusIndex:
    """Read-only lookup of (year, code_norm, number) -> documents."""

    def __init__(self, db_path: Path = CORPUS_DB) -> None:
        if not db_path.exists():
            raise RuntimeError(
                f"corpus index missing at {db_path}; run scripts/build_corpus_index.py"
            )
        self._connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    def __enter__(self) -> CorpusIndex:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def lookup(self, year: str, code: str, number: str) -> list[DocumentRef]:
        cursor = self._connection.execute(
            "SELECT archive, entry, court, year, code, code_norm, number, jdate, seq "
            "FROM documents WHERE year = ? AND code_norm = ? AND number = ?",
            (year, normalize_code(code), number),
        )
        return [DocumentRef(*row) for row in cursor]

    def iter_documents(self, archive: str) -> Iterator[DocumentRef]:
        cursor = self._connection.execute(
            "SELECT archive, entry, court, year, code, code_norm, number, jdate, seq "
            "FROM documents WHERE archive = ?",
            (archive,),
        )
        for row in cursor:
            yield DocumentRef(*row)
