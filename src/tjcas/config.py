"""Project paths and environment configuration."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
SAMPLES_DIR = DATA_DIR / "samples"
REFERENCE_DIR = DATA_DIR / "reference"
MANIFEST_DIR = DATA_DIR / "manifest"
MANIFEST_PATH = MANIFEST_DIR / "sample_manifest.csv"

INDEX_DIR = DATA_DIR / "index"
CORPUS_DB = INDEX_DIR / "corpus.sqlite"
CITATIONS_DIR = DATA_DIR / "citations"
CANDIDATES_DIR = DATA_DIR / "candidates"
RESOLUTION_DIR = DATA_DIR / "resolution"

load_dotenv(PROJECT_ROOT / ".env")


def get_data_route() -> Path:
    """Return the archive directory declared as DATA_ROUTE in .env."""
    raw = os.getenv("DATA_ROUTE")
    if not raw:
        raise RuntimeError("DATA_ROUTE is not set; define it in .env")
    route = Path(raw.strip().strip("\"'")).expanduser()
    if not route.is_dir():
        raise RuntimeError(f"DATA_ROUTE does not point to a directory: {route}")
    return route
