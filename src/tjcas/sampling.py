"""Read judgment JSON out of the monthly RAR archives.

Archives are never written to disk: entries are listed with ``rar l`` and their
contents are streamed straight out of ``rar p``.
"""

from __future__ import annotations

import csv
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from tjcas.config import MANIFEST_PATH, SAMPLES_DIR, get_data_route

RAR_BIN = "rar"
_TABLE_SEPARATOR = "-----------"


@dataclass(frozen=True)
class ArchiveEntry:
    archive: Path
    name: str
    size: int

    @property
    def sample_filename(self) -> str:
        return f"{self.archive.stem}__{Path(self.name).name}"


def list_archives(data_route: Path | None = None) -> list[Path]:
    route = data_route or get_data_route()
    return sorted(route.glob("*.rar"))


def _run_rar(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        [RAR_BIN, *args], capture_output=True, check=False
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"rar {' '.join(args)} failed: {stderr}")
    return result


def iter_entries(archive: Path) -> Iterator[ArchiveEntry]:
    """Yield the file entries of an archive without extracting anything."""
    output = _run_rar(["l", str(archive)]).stdout.decode("utf-8", errors="replace")
    in_table = False
    for line in output.splitlines():
        if line.startswith(_TABLE_SEPARATOR):
            if in_table:
                break
            in_table = True
            continue
        if not in_table:
            continue
        parts = line.split(maxsplit=4)
        if len(parts) < 5:
            continue
        attributes, size, _date, _time, name = parts
        if "D" in attributes:
            continue
        if not size.isdigit():
            continue
        yield ArchiveEntry(archive=archive, name=name, size=int(size))


def largest_entry(archive: Path, suffix: str = ".json") -> ArchiveEntry | None:
    candidates = (e for e in iter_entries(archive) if e.name.endswith(suffix))
    return max(candidates, key=lambda e: e.size, default=None)


def stream_entries(archive: Path) -> Iterator[tuple[ArchiveEntry, bytes]]:
    """Yield ``(entry, raw bytes)`` for every file in ``archive``, touching no disk.

    ``rar p`` writes the entries back to back in listing order, so the stream is
    cut up using the sizes ``rar l`` already reported.
    """
    entries = list(iter_entries(archive))
    process = subprocess.Popen(
        [RAR_BIN, "p", "-inul", str(archive)], stdout=subprocess.PIPE
    )
    stdout = process.stdout
    assert stdout is not None
    try:
        for entry in entries:
            payload = stdout.read(entry.size)
            if len(payload) != entry.size:
                raise RuntimeError(
                    f"{archive.name}: stream ended inside {entry.name}"
                )
            yield entry, payload
    finally:
        stdout.close()
        if process.poll() is None:
            process.terminate()
        process.wait()


def extract_entry(entry: ArchiveEntry, dest_dir: Path) -> Path:
    """Stream a single entry out of its archive into ``dest_dir``."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    payload = _run_rar(["p", "-inul", str(entry.archive), entry.name]).stdout
    destination = dest_dir / entry.sample_filename
    destination.write_bytes(payload)
    return destination


def write_manifest(entries: list[ArchiveEntry], manifest_path: Path) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["archive", "entry", "size_bytes", "sample_filename"])
        for entry in entries:
            writer.writerow(
                [entry.archive.name, entry.name, entry.size, entry.sample_filename]
            )


def sample_cases(
    data_route: Path | None = None,
    dest_dir: Path = SAMPLES_DIR,
    manifest_path: Path = MANIFEST_PATH,
    limit: int | None = None,
    verbose: bool = True,
) -> list[ArchiveEntry]:
    """Pick the largest JSON judgment of every archive and extract it."""
    archives = list_archives(data_route)[:limit]
    sampled: list[ArchiveEntry] = []
    for index, archive in enumerate(archives, start=1):
        entry = largest_entry(archive)
        if entry is None:
            if verbose:
                print(f"[{index}/{len(archives)}] {archive.name}: no json entry")
            continue
        extract_entry(entry, dest_dir)
        sampled.append(entry)
        if verbose:
            print(
                f"[{index}/{len(archives)}] {archive.name}: "
                f"{entry.size:>9,} bytes  {Path(entry.name).name}"
            )
    write_manifest(sampled, manifest_path)
    return sampled
