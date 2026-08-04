"""collect.py - 5-minute snapshot collector for the RIVERNET.LK archive.

Fetches the live snapshot of every gauge from
/api/overview/latest-status-paginated. Value rows are written to per-station
CSVs in data/{region}/{unit_id}_{type}.csv **only for rain stations**: the
server retains no rain history (the chart API only keeps ~2.4 days of 1-minute
rain points), so these snapshots are the only long-term rainfall record.

river_level / river_rain rows are NOT written — the public chart endpoint
already serves their full 5-minute history (see backfill.py), so snapshot
values would only duplicate it. The other device types are still fetched to
refresh station metadata (coordinates, max level, device keys).

Works without credentials (snapshot endpoint is public).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import List, Tuple

import rivernet
import store

STATE_FILE = Path("state/last_collect.json")
STALE_CUTOFF = timedelta(days=2)  # older snapshots are already in the chart backfill
VALUE_TYPES = ("rain",)  # only rain needs the snapshot as its long-term record


def main() -> int:
    client = rivernet.Rivernet()

    total_stations = 0
    total_rows = 0
    per_type: List[Tuple[str, int, int]] = []

    for device_type in rivernet.DEVICE_TYPES:
        started = time.time()
        try:
            devices = client.all_latest(device_type)
        except rivernet.RivernetError as exc:
            print(f"[{device_type}] ERROR: {exc}", file=sys.stderr)
            continue
        rows_added = 0
        cutoff = rivernet.iso_utc(rivernet.now_local() - STALE_CUTOFF)
        for device in devices:
            path = store.station_path(
                device.get("region") or "unknown",
                device.get("unitId") or device.get("deviceKey") or "?",
                device.get("type") or device_type,
            )
            if device_type not in VALUE_TYPES:
                # metadata-only pass for non-rain types (header refresh)
                store.ensure_station_file(path, device)
                continue
            row = rivernet.snapshot_row(device)
            if row is None:
                # station offline / not reporting yet — still register its file
                store.ensure_station_file(path, device)
                continue
            if row["datetime_utc"] < cutoff:
                # stale reading, already captured by the chart backfill
                continue
            added = store.append_rows(path, [row], device)
            rows_added += added
            total_rows += added
        total_stations += len(devices)
        per_type.append((device_type, len(devices), rows_added))
        print(
            f"[{device_type}] stations={len(devices)} rows_added={rows_added} "
            f"({time.time() - started:.1f}s)"
        )
        time.sleep(rivernet.CRAWL_DELAY)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "run_at_utc": rivernet.iso_utc(rivernet.now_local()),
        "per_type": [{"type": t, "stations": s, "rows_added": r} for t, s, r in per_type],
    }))

    print(f"TOTAL stations={total_stations} rows_added={total_rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
