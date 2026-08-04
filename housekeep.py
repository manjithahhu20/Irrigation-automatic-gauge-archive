"""housekeep.py - monthly archiving and integrity pass.

1. Splits every station CSV in data/: current-month rows stay plain in data/,
   all older months are merged (deduped) into archive/{YYYY-MM}/...csv.gz.
2. Compresses any stray plain .csv left in archive/ from earlier runs.
3. Prunes empty region directories under data/.

Run daily in a workflow. Designed to keep the git repo small: only the current
month lives as plain (education-friendly) CSVs; everything older is gzipped.
"""
from __future__ import annotations

import gzip
import os
import shutil
import sys
from pathlib import Path

import store


def split_finished_files() -> int:
    """Move non-current-month rows from data/ CSVs into archive/ gzips."""
    archived = 0
    if not store.DATA_DIR.exists():
        return archived
    for path in sorted(store.DATA_DIR.rglob("*.csv")):
        archived += store.split_and_archive(path)
    return archived


def compress_loose_archives() -> int:
    compressed = 0
    for path in sorted(store.ARCHIVE_DIR.rglob("*.csv")) if store.ARCHIVE_DIR.exists() else []:
        store.gzip_csv(path)
        path.unlink(missing_ok=True)
        compressed += 1
        print(f"compressed {path}")
    return compressed


def prune_empty_dirs(root: Path) -> None:
    for dir_path in sorted((d for d in root.rglob("*") if d.is_dir()), reverse=True):
        try:
            dir_path.rmdir()
        except OSError:
            pass


def main() -> int:
    archived = split_finished_files()
    compressed = compress_loose_archives()
    if store.DATA_DIR.exists():
        prune_empty_dirs(store.DATA_DIR)
    print(f"TOTAL archived_months={archived} compressed={compressed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())