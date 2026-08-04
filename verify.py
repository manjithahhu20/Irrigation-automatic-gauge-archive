"""verify.py - anonymous end-to-end check of the RIVERNET.LK endpoints.

Run with no credentials; all endpoints probed here are public:
  * station inventory per device type (and earliest created_at)
  * the public chart history endpoint returns data
  * deviceKey == chart keys token holds per type
  * raw payload schema samples are dumped to state/schema/ for inspection

Usage:
    python verify.py
    python verify.py --range-days 14
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import rivernet
import store

SCHEMA_DIR = Path("state/schema")


def _dump(name: str, payload) -> None:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    path = SCHEMA_DIR / name
    path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    print(f"  raw schema dumped -> {path}")


def probe_type(client: rivernet.Rivernet, device_type: str,
               range_days: int, now: datetime) -> bool:
    print(f"--- {device_type} ---")
    devices = client.all_latest(device_type)
    print(f"  stations={len(devices)}")
    if not devices:
        print("  ERROR: no devices returned")
        return False
    earliest = None
    for device in devices:
        dt = rivernet.parse_iso(device.get("created_at"))
        if dt and (earliest is None or dt < earliest):
            earliest = dt
    print(f"  earliest created_at={earliest.date() if earliest else '?'}")

    sample = devices[0]
    unit = sample.get("unitId")
    key = sample.get("deviceKey")
    print(f"  sample: {unit} ({sample.get('location')}) device_key={key}")

    from_ms = rivernet.to_millis(rivernet.floor_minutes(now - timedelta(days=range_days)))
    # The server stores stamps in local wall-clock labeled as UTC, so its
    # range filter compares against x values shifted +05:30; extend to_ms to
    # include the newest ~5.5h of data.
    to_ms = rivernet.to_millis(now + timedelta(hours=6))
    payload = client.chart(device_type, key, from_ms, to_ms)
    points = rivernet.chart_points(payload, key)
    print(f"  chart {range_days}d: points_extracted={len(points)}")
    if points:
        print(f"    first point: {points[0]}")
        print(f"    last  point: {points[-1]}")
        last_dt = rivernet.parse_iso(points[-1]["datetime_utc"])
        if last_dt:
            age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
            print(f"    last point age: {age_min:.0f} min (fresh if < ~15)")
            if age_min > 30:
                print("  WARN: chart data appears stale")
    names = set()
    results = payload.get("results") if isinstance(payload, dict) else None
    for series in (results or {}).get("series") or []:
        if isinstance(series, dict) and series.get("name"):
            names.add(series["name"])
    if names:
        print(f"    series name(s): {', '.join(sorted(names))}")
    _dump(f"{unit}_chart_{range_days}d.json", payload)
    return bool(points)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--range-days", type=int, default=3,
                        help="history range to probe per station")
    args = parser.parse_args()

    client = rivernet.Rivernet()
    now = rivernet.floor_minutes(rivernet.now_local())

    ok = True
    for device_type in rivernet.DEVICE_TYPES:
        if not probe_type(client, device_type, args.range_days, now):
            ok = False
        time.sleep(rivernet.CRAWL_DELAY)

    print("VERIFY SUMMARY:", "all endpoint probes OK (public, no auth)" if ok
          else "see schema dump for failures")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
