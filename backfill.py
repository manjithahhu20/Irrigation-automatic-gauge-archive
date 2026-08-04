"""backfill.py - resumable history backfill for the RIVERNET.LK archive.

Pulls station history from the public chart endpoint (no auth required):
    /api/reports/{river-level|rainfall}/chart/minute/{from_ms}/{to_ms}
    ?keys={device_key}&last24HoursData={0|1}&isPublic=1&isPublicHistory=1
and merges it into the same per-station CSVs used by collect.py.

Server retention rules (verified empirically):
  * last24HoursData=1 -> ~1-minute points, but only ~2.4 days are retained,
    so the final chunk re-pulls the last 3 days on every run.
  * last24HoursData=0 -> 5-minute points, full history, for river_level and
    river_rain only; the server retains NO rain history (rain stations get
    only the 1-minute recent chunk; the 5-minute snapshot collector in
    collect.py is what builds the rain record over time).

Use cases:
  * first run:     python backfill.py                         (auto-detect per
                     station from its created_at; clip with --from)
  * daily repair:  python backfill.py --from $(date -u -d '-3 days' +%F)
  * dry test:      python backfill.py --from 2026-08-01 --to 2026-08-03 --limit 2

Progress is tracked in state/backfill.json so interrupted runs resume. Ranges
are processed as sub-chunks (default 30 days; the 5-minute series tolerates
much larger windows). Requests are throttled to ~1/sec per the site's
robots.txt Crawl-delay.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import rivernet
import store

STATE_FILE = Path("state/backfill.json")
DEFAULT_CHUNK_DAYS = 30
RECENT_DAYS = 3  # the server retains only ~2.4 days of 1-minute data


def parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=rivernet.SRI_LANKA)


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {"stations": {}}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def chunk_range(from_dt: datetime, to_dt: datetime, days: int) -> List[Tuple[str, str]]:
    chunks: List[Tuple[str, str]] = []
    current = from_dt
    while current < to_dt:
        end = min(current + timedelta(days=days - 1), to_dt)
        chunks.append((current.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")))
        current = end + timedelta(days=1)
    return chunks


def build_chunks(from_dt: datetime, to_dt: datetime, days: int,
                 today: datetime, recent_only: bool = False) -> List[Tuple[str, str, bool]]:
    """Split a station's range into (from, to, last_24h) chunks.

    Historical chunks (last24HoursData=0, 5-minute points) are only useful for
    river_level / river_rain — the server retains no rain history. A final
    chunk spanning the last ~3 days uses last24HoursData=1 (1-minute points);
    the server keeps only ~2.4 days of 1-minute data, so this window must be
    re-pulled at least daily to preserve it. recent_only=True (rain stations)
    fetches just that window.
    """
    recent_start = rivernet.floor_minutes(today - timedelta(days=RECENT_DAYS))
    chunks: List[Tuple[str, str, bool]] = []
    if not recent_only and from_dt < recent_start:
        for from_day, to_day in chunk_range(from_dt, min(to_dt, recent_start), days):
            chunks.append((from_day, to_day, False))
    if to_dt > recent_start:
        chunks.append((
            recent_start.strftime("%Y-%m-%d"),
            min(to_dt, today).strftime("%Y-%m-%d"),
            True,
        ))
    return chunks


def station_start(device: dict, explicit_from: Optional[datetime]) -> datetime:
    """Per-station start: its created_at, clipped by an explicit --from."""
    created = rivernet.parse_iso(device.get("created_at"))
    if created is None:
        created = rivernet.now_local() - timedelta(days=7)
    if explicit_from is not None and explicit_from > created:
        created = explicit_from
    return rivernet.floor_minutes(created)


def collect_devices(client: rivernet.Rivernet) -> List[dict]:
    devices: List[dict] = []
    for device_type in rivernet.DEVICE_TYPES:
        devices.extend(client.all_latest(device_type))
    return devices


def process_station(client: rivernet.Rivernet, state: Dict[str, Any],
                    device: dict, chunks: List[Tuple[str, str, bool]]) -> Dict[str, int]:
    unit = f"{device.get('unitId')}_{device.get('type')}"
    device_key = device.get("deviceKey")
    device_type = device.get("type")
    region = device.get("region") or "unknown"
    state_key = f"{unit}:{device_key}"
    entry = state["stations"].setdefault(
        state_key, {"unit": unit, "device_key": device_key, "done": [],
                    "station": device.get("location") or ""}
    )

    stat = {"chunks": 0, "rows": 0, "failed": 0}

    for from_day, to_day, last_24h in chunks:
        if {"from": from_day, "to": to_day} in entry["done"]:
            continue
        from_ms = rivernet.to_millis(rivernet.floor_minutes(parse_date(from_day)))
        to_ms = rivernet.to_millis(parse_date(to_day) + timedelta(days=1))
        try:
            payload = client.chart(device_type, device_key, from_ms, to_ms,
                                   last_24h=last_24h)
            points = rivernet.chart_points(payload, device_key)
            path = store.station_path(region, store.station_label(device), device_type)
            added = store.append_rows(path, points, device)
            entry["done"].append({"from": from_day, "to": to_day})
            stat["chunks"] += 1
            stat["rows"] += added
            print(f"  {unit} {from_day}:{to_day} last24h={int(last_24h)} "
                  f"points={len(points)} added={added}")
        except rivernet.RivernetError as exc:
            stat["failed"] += 1
            print(f"  {unit} {from_day}:{to_day} FAILED: {exc}", file=sys.stderr)
            # keep going; the chunk stays unmarked and resumes next run
        time.sleep(rivernet.CRAWL_DELAY)
    return stat


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill RIVERNET.LK station history")
    parser.add_argument("--from", dest="from_day", default="",
                        help="start YYYY-MM-DD (default: each station's created_at)")
    parser.add_argument("--to", dest="to_day", default="",
                        help="end YYYY-MM-DD inclusive (default: today)")
    parser.add_argument("--chunk-days", dest="chunk_days", type=int, default=DEFAULT_CHUNK_DAYS)
    parser.add_argument("--type", choices=rivernet.DEVICE_TYPES, default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument("--limit", type=int, default=None,
                        help="process at most N stations (testing)")
    args = parser.parse_args()

    client = rivernet.Rivernet()

    devices = collect_devices(client)
    if args.type:
        devices = [d for d in devices if d.get("type") == args.type]
    if args.region:
        devices = [d for d in devices if (d.get("region") or "") == args.region]
    devices.sort(key=lambda d: (d.get("region") or "", d.get("unitId") or ""))
    if args.limit:
        devices = devices[: args.limit]

    today = rivernet.now_local()
    to_dt = parse_date(args.to_day) if args.to_day else today
    explicit_from = parse_date(args.from_day) if args.from_day else None

    state = load_state()
    totals = {"chunks": 0, "rows": 0, "failed": 0}
    for device in devices:
        device_type = device.get("type")
        from_dt = station_start(device, explicit_from)
        chunks = build_chunks(from_dt, to_dt, args.chunk_days, today,
                              recent_only=(device_type == "rain"))
        unit = f"{device.get('unitId')}_{device_type}"
        print(f"{unit} ({device.get('location') or ''}) start={from_dt.date()} "
              f"last={chunks[-1][1] if chunks else ''} chunks={len(chunks)}")
        stat = process_station(client, state, device, chunks)
        for key in totals:
            totals[key] += stat[key]
        save_state(state)

    print(f"TOTAL chunks={totals['chunks']} rows={totals['rows']} failed={totals['failed']}")
    return 0 if totals["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
