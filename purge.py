"""purge.py - one-off: drop all source=snapshot rows from the archive.

The archive is chart-only now (collect.py was removed), so remaining snapshot
rows are stale duplicates. Runs idempotently: reruns find nothing to purge.
"""
from __future__ import annotations

import gzip
import sys
from pathlib import Path
from typing import Dict, List

import store

DATA_DIR = store.DATA_DIR
ARCHIVE_DIR = store.ARCHIVE_DIR
SKIPPED: List[Path] = []


def _locked(path: Path) -> bool:
    """True if another process holds an exclusive write handle (e.g. an editor)."""
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True


def purge_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return [row for row in rows if (row.get("source") or "").strip() != "snapshot"]


def purge_data_file(path: Path) -> int:
    if _locked(path):
        SKIPPED.append(path)
        return 0
    rows = store._read_rows(path)
    kept = purge_rows(rows)
    if len(kept) == len(rows):
        return 0
    store._write_rows(path, store.CSV_HEADER, kept)
    return len(rows) - len(kept)


def purge_gz_file(path: Path) -> int:
    if _locked(path):
        SKIPPED.append(path)
        return 0
    rows = store.read_gz_rows(path)
    kept = purge_rows(rows)
    if len(kept) == len(rows):
        return 0
    comments = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                comments.append(line.rstrip("\n"))
    store._write_gz(path, comments, kept)
    return len(rows) - len(kept)


def main() -> int:
    total = 0
    touched = 0
    for path in sorted(DATA_DIR.rglob("*.csv")):
        removed = purge_data_file(path)
        if removed:
            touched += 1
        total += removed
    for path in sorted(ARCHIVE_DIR.rglob("*.csv.gz")):
        removed = purge_gz_file(path)
        if removed:
            touched += 1
        total += removed
    print(f"purged rows={total} files_touched={touched}")
    for path in SKIPPED:
        print(f"SKIPPED (locked): {path}")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
