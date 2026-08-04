"""migrate.py - one-off data migration (2026-08-04).

1. Chart-row timestamps: the RIVERNET.LK chart API stamps telemetry in Sri
   Lanka local wall-clock time but labels it as UTC ("+00:00"/"Z", with a
   matching epoch x). The stored datetime_utc therefore actually held local
   time and datetime_local_530 was +05:30 too far. Every chart row is
   rewritten to:

       datetime_utc       = true UTC   (old value reinterpreted as +05:30)
       datetime_local_530 = true local (old datetime_utc wall clock)

   Snapshot rows (source="snapshot") were already correct and stay untouched.

2. File rename: <unit>_<type>.csv -> <Station Name>_<type>.csv across
   data/ and archive/, using each file's "# station:" comment.

Migrated files get a "# migrated:" comment line, so re-runs are no-ops.

Usage:
    python migrate.py
"""
from __future__ import annotations

import csv
import gzip
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import rivernet
import store

MARKER = "# migrated: 2026-08-04 chart local-stamp -> true UTC (migrate.py)"


def _comment_lines(path: Path) -> List[str]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle if line.startswith("#")]


def _read_plain(path: Path) -> List[Dict[str, str]]:
    with path.open(encoding="utf-8-sig") as handle:
        reader = csv.DictReader(
            (row for row in handle
             if not row.startswith("#") and not row.startswith("datetime_utc,")),
            fieldnames=store.CSV_HEADER,
        )
        return [row for row in reader if row.get("datetime_utc")]


def _read_gz(path: Path) -> List[Dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        reader = csv.DictReader(
            (row for row in handle
             if not row.startswith("#") and not row.startswith("datetime_utc,") and row.strip()),
            fieldnames=store.CSV_HEADER,
        )
        return [row for row in reader if row.get("datetime_utc")]


def _migrate_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for row in rows:
        if row.get("source") == "chart":
            old = row.get("datetime_utc", "")
            try:
                wall = datetime.fromisoformat(old.replace("Z", "+00:00")).replace(tzinfo=None)
                local = wall.replace(tzinfo=rivernet.SRI_LANKA)
                row = dict(row)
                row["datetime_utc"] = local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                row["datetime_local_530"] = local.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                print(f"    WARN: unparseable datetime_utc {old!r} left as-is", file=sys.stderr)
        out.append(row)
    seen: set = set()
    deduped: List[Dict[str, str]] = []
    for row in sorted(out, key=lambda r: (r.get("source") != "snapshot", r["datetime_utc"])):
        key = row["datetime_utc"][:16]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    deduped.sort(key=lambda r: r["datetime_utc"])
    return deduped


def _write_plain(path: Path, comments: List[str], rows: List[Dict[str, str]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        for line in comments:
            handle.write(line + "\n")
        writer = csv.DictWriter(handle, fieldnames=store.CSV_HEADER, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    store._atomic_replace(str(tmp), str(path))


def _write_gz(path: Path, comments: List[str], rows: List[Dict[str, str]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as handle:
        for line in comments:
            handle.write(line + "\n")
        writer = csv.DictWriter(handle, fieldnames=store.CSV_HEADER, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    store._atomic_replace(str(tmp), str(path))


def migrate_file(path: Path) -> int:
    """Rewrite one station file. Returns rows written, or -1 if already done."""
    comments = _comment_lines(path)
    if any(line.startswith("# migrated:") for line in comments):
        return -1
    rows = _read_gz(path) if path.suffix == ".gz" else _read_plain(path)
    if not rows:
        return 0
    migrated = _migrate_rows(rows)
    new_comments = [MARKER] + comments
    if path.suffix == ".gz":
        _write_gz(path, new_comments, migrated)
    else:
        _write_plain(path, new_comments, migrated)
    return len(migrated)


def _station_from_comments(comments: List[str]) -> str:
    for line in comments:
        if line.startswith("# station:"):
            return line.split(":", 1)[1].strip()
    return ""


def _comment_field(comments: List[str], field: str) -> str:
    for line in comments:
        if line.startswith(f"# {field}:"):
            return line.split(":", 1)[1].strip()
    return ""


def _type_from_comments(comments: List[str]) -> str:
    raw = _comment_field(comments, "type")
    return raw.split()[0] if raw else ""


def rename_file(path: Path, suffix: str) -> Path:
    """Rename to <Station Name>_<type><suffix> using the file's metadata
    comments; the type is read from the "# type:" comment (two-part types
    like river_level / river_rain were mangled by the first pass)."""
    comments = _comment_lines(path)
    station = _station_from_comments(comments) or _comment_field(comments, "unit_id")
    device_type = _type_from_comments(comments)
    if not device_type:
        stem = path.name[: -len(suffix)]
        for known in ("river_level", "river_rain"):
            if stem.endswith("_" + known):
                device_type = known
                break
        else:
            device_type = stem.rsplit("_", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", station or "station").strip("_. ") or "station"
    new_path = path.with_name(f"{base}_{device_type}{suffix}")
    if new_path.exists() and new_path.resolve() != path.resolve():
        unit = _comment_field(comments, "unit_id") or "x"
        new_path = path.with_name(f"{base}_{unit}_{device_type}{suffix}")
    if new_path != path:
        store._atomic_replace(str(path), str(new_path))
    return new_path


def main() -> int:
    files = sorted(store.DATA_DIR.rglob("*.csv")) + sorted(store.ARCHIVE_DIR.rglob("*.gz"))
    migrated_files = 0
    total_rows = 0
    for path in files:
        n = migrate_file(path)
        if n < 0:
            continue
        migrated_files += 1
        total_rows += n
        print(f"migrated {path} rows={n}")
    renamed = 0
    for path in files:
        suffix = ".csv.gz" if path.suffix == ".gz" else ".csv"
        new_path = rename_file(path, suffix)
        if new_path != path:
            renamed += 1
            print(f"renamed {path} -> {new_path}")
    print(f"TOTAL migrated_files={migrated_files} rows={total_rows} renamed={renamed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
