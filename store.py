"""Per-station CSV storage for the RIVERNET.LK archive.

Layout:
    data/{region}/{unit_id}_{device_type}.csv        current month, plain CSV
    archive/{YYYY-MM}/{unit_id}_{device_type}.csv.gz older months, gzipped
"""
from __future__ import annotations

import csv
import gzip
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DATA_DIR = Path("data")
ARCHIVE_DIR = Path("archive")

CSV_HEADER = ["datetime_utc", "datetime_local_530", "value", "received_at_utc", "source"]


def _safe_name(unit_id: str, device_type: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", str(unit_id)).strip("_")
    return f"{stem or 'station'}_{device_type}.csv"


def station_path(region: str, unit_id: str, device_type: str) -> Path:
    return DATA_DIR / _safe_region(region) / _safe_name(unit_id, device_type)


def _safe_region(region: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(region or "unknown")).strip("_")
    return cleaned or "unknown"


def _metadata_lines(device: Dict[str, Any]) -> List[str]:
    rainy = device.get("additional") or {}
    coords = rainy.get("coordinates") or {}
    lat = coords.get("latitude")
    lon = coords.get("longitude")
    unit = "m" if device.get("type") == "river_level" else "mm"
    lines = [
        f"# station: {device.get('location') or ''}",
        f"# unit_id: {device.get('unitId') or ''}",
        f"# device_key: {device.get('deviceKey') or ''}",
        f"# type: {device.get('type') or ''}   region: {device.get('region') or ''}",
        f"# value unit: {unit}   max_level_{unit}: {rainy.get('maxLevel') or ''}",
        f"# lat: {lat}   lon: {lon}",
        "# source: rivernet.lk flood early-warning API (site-owner permission granted)",
    ]
    return lines


def ensure_station_file(path: Path, device: Dict[str, Any]) -> None:
    """Create a station CSV with metadata header if it does not exist."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        for line in _metadata_lines(device):
            handle.write(line + "\n")
        handle.write(",".join(CSV_HEADER) + "\n")


def _read_rows(path: Path) -> List[Dict[str, str]]:
    with path.open(encoding="utf-8-sig") as handle:
        reader = csv.DictReader(
            (row for row in handle if not row.startswith("#") and not row.startswith("datetime_utc,")),
            fieldnames=CSV_HEADER,
        )
        return [row for row in reader if row.get("datetime_utc")]


def _comment_lines(path: Path) -> List[str]:
    with path.open(encoding="utf-8-sig") as handle:
        return [line.rstrip("\n") for line in handle if line.startswith("#")]


def _atomic_replace(src: str, dst: str) -> None:
    """os.replace with retries for transient locks (AV scans on Windows)."""
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(1 + attempt)


def _write_rows(path: Path, header: List[str], rows: Iterable[Dict[str, str]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        for line in _comment_lines(path):
            handle.write(line + "\n")
        writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    _atomic_replace(str(tmp), str(path))


def _minute_key(datetime_utc: str) -> str:
    """Dedup key: datetime floored to the minute.

    Snapshot rows land on a 2-minute grid while chart rows are exact minutes,
    so comparing whole timestamps would stack near-duplicates for the same
    minute. Rows are stored at full precision; only the key is floored.
    """
    return datetime_utc[:16]


def append_rows(path: Path, rows: Iterable[Dict[str, Any]], device: Dict[str, Any]) -> int:
    """Append new rows, deduping on the minute of datetime_utc. Returns count appended."""
    ensure_station_file(path, device)
    existing = _read_rows(path)
    seen = {_minute_key(row["datetime_utc"]) for row in existing}
    added = 0
    for row in rows:
        key = row.get("datetime_utc")
        if not key or _minute_key(key) in seen:
            continue
        existing.append({k: "" if row.get(k) is None else str(row.get(k)) for k in CSV_HEADER})
        seen.add(_minute_key(key))
        added += 1
    if added:
        existing.sort(key=lambda r: r["datetime_utc"])
        _write_rows(path, CSV_HEADER, existing)
    return added


def last_datetime(path: Path) -> Optional[str]:
    """Efficient peek at the last row's datetime (for gap detection)."""
    if not path.exists():
        return None
    last: Optional[str] = None
    with path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            if line.startswith("#") or not line.strip():
                continue
            if line.startswith("datetime_utc,"):
                continue
            last = line.split(",", 1)[0]
    if last and last[0] == "\ufeff":
        last = last[1:]
    return last


def gzip_csv(path: Path) -> Path:
    """Compress a plain CSV into archive/{YYYY-MM}/...csv.gz with dedup+sort.

    The last-row date decides the target month folder.
    """
    rows = _read_rows(path)
    rows.sort(key=lambda r: r["datetime_utc"])
    header = _read_header(path)
    month = _month_of(rows[-1]["datetime_utc"]) if rows else datetime.now().strftime("%Y-%m")
    target_dir = ARCHIVE_DIR / month
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (path.stem + ".csv.gz")
    _write_gz(target, header, rows)
    return target


def _write_gz(target: Path, comments: List[str], rows: List[Dict[str, str]]) -> None:
    tmp = target.with_suffix(target.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as handle:
        for line in comments:
            handle.write(line + "\n")
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    _atomic_replace(str(tmp), str(target))


def read_gz_rows(path: Path) -> List[Dict[str, str]]:
    """Read data rows from an existing archive .csv.gz (skips comments/header)."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        reader = csv.DictReader(
            (row for row in handle
             if not row.startswith("#") and not row.startswith("datetime_utc,") and row.strip()),
            fieldnames=CSV_HEADER,
        )
        return [row for row in reader if row.get("datetime_utc")]


def merge_gz(target: Path, comments: List[str], rows: List[Dict[str, str]]) -> int:
    """Merge rows into an archive month file, deduping on the minute. Returns added."""
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = read_gz_rows(target) if target.exists() else []
    seen = {_minute_key(row["datetime_utc"]) for row in existing}
    merged = list(existing)
    added = 0
    for row in rows:
        key = _minute_key(row["datetime_utc"])
        if key in seen:
            continue
        merged.append({k: str(row.get(k) or "") for k in CSV_HEADER})
        seen.add(key)
        added += 1
    if added:
        merged.sort(key=lambda r: r["datetime_utc"])
        _write_gz(target, comments, merged)
    return added


def split_and_archive(path: Path) -> int:
    """Move non-current-month rows from a plain station CSV into archive gzips.

    Rows are grouped by month: the current month stays in data/ as plain CSV,
    every older month is merged (deduped) into archive/{YYYY-MM}/{stem}.csv.gz.
    Returns the number of months archived. Files with no current-month rows are
    removed from data/ after archiving; empty files are left untouched.
    """
    rows = _read_rows(path)
    if not rows:
        return 0
    comments = _comment_lines(path)
    current = _now_month()
    by_month: Dict[str, List[Dict[str, str]]] = {}
    for row in rows:
        by_month.setdefault(_month_of(row["datetime_utc"]), []).append(row)
    archived = 0
    for month, month_rows in by_month.items():
        if month == current:
            continue
        target = ARCHIVE_DIR / month / (path.stem + ".csv.gz")
        merge_gz(target, comments, month_rows)
        archived += 1
    current_rows = by_month.get(current, [])
    if current_rows:
        current_rows.sort(key=lambda r: r["datetime_utc"])
        _write_rows(path, CSV_HEADER, current_rows)
    else:
        path.unlink(missing_ok=True)
    return archived


def _read_header(path: Path) -> List[str]:
    with path.open(encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle if line.startswith("#")]


def _month_of(datetime_utc: str) -> str:
    try:
        return datetime.strptime(datetime_utc[:10], "%Y-%m-%d").strftime("%Y-%m")
    except ValueError:
        return datetime.now().strftime("%Y-%m")


def _now_month() -> str:
    return datetime.now().strftime("%Y-%m")
