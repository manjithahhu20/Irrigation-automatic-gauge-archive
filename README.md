# RIVERNET.LK flood-data archive

Automated, educational archive of real-time river levels, rainfall and
river-rain gauge readings from Sri Lanka's **RIVERNET.LK** flood early-warning
system (<https://rivernet.lk>). GitHub Actions polls the **public** API and
commits the data as per-station CSV files: live 5-minute snapshots plus a
full-history backfill (1-minute resolution for recent days, 5-minute for
history).

> **Permission:** the RIVERNET.LK site owner has granted permission for this
> archive. `robots.txt` explicitly allows bots and LLM agents
> (`Crawl-delay: 1`, which this project honours by throttling to ~1 req/sec).

## Repository layout

```
data/{region}/{Station Name}_{type}.csv        current month, plain CSV (education-friendly)
archive/{YYYY-MM}/{Station Name}_{type}.csv.gz prior months, gzipped (keeps repo small)
state/backfill.json                            resume state for backfill work
state/last_collect.json                        last snapshot run metadata
rivernet.py                                    API client (snapshot + public chart history)
collect.py                                     live 5-minute snapshot collector
backfill.py                                    resumable history backfill + daily repair
housekeep.py                                   monthly archiving / integrity pass
verify.py                                      anonymous endpoint schema check
```

**CSV columns:** `datetime_utc, datetime_local_530, value, received_at_utc, source`

- `datetime_utc` is the true UTC timestamp of the reading.
- `datetime_local_530` is the same instant in Sri Lanka local time (UTC+05:30).
- `value` units: river_level = metres, rain/river_rain = millimetres.

Station metadata (coordinates, max level, alert thresholds) is stored in `#`
header lines of each file. Rows from both sources (snapshot + chart) are
merged and deduplicated on the minute.

> **Timestamp caveat (fixed):** the chart API stamps telemetry in Sri Lanka
> *local* wall-clock time but labels it as UTC (`+00:00`/`Z`, epoch `x`
> matching the local-shifted stamp). All rows written before 2026-08-04 were
> shifted by −05:30 in a one-off migration (`migrate.py`) so `datetime_utc`
> now holds true UTC; files carry a `# migrated:` marker in their header.
> Snapshot rows were always correct.

## Setup

1. Fork / create a repo from this directory and enable Actions.
2. Push. The `collect` workflow starts automatically.
3. **No credentials or Secrets are required** — every endpoint used by this
   archive is public.

## Workflows

| Workflow    | Trigger                        | Action |
|-------------|--------------------------------|--------|
| `collect`   | `*/5` min cron + manual        | live snapshot → rain-only value rows (long-term rainfall record), metadata refresh for all stations |
| `backfill`  | `workflow_dispatch` + daily 01:15 & 13:15 UTC | one-time full history (river stations only, 5-min), then 3-day gap repair incl. 1-minute recent data |
| `housekeep` | daily 02:30 UTC                | zips prior months to `archive/`, dedups, prunes empties |

## Data retention rules (verified empirically)

The chart API serves three flavours of data, with different retention:

| Series | How to request | Retention |
|--------|----------------|-----------|
| 1-minute | `last24HoursData=1` | **~2.4 days only** — re-pulled every run by the backfill's 3-day "recent" chunk |
| 5-minute | `last24HoursData=0` | **full history** (verified to 120+ days) for river_level and river_rain |
| rain | either | **no history at all** — only ~2.4 days of 1-minute data exists server-side |

Consequences for the archive:
- `backfill.py` pulls 5-minute history in 30-day chunks for river stations
  only, plus the rolling 3-day 1-minute window for every station.
- `collect.py` writes snapshot value rows **only for rain stations** — they are
  the only long-term rainfall record. river_level / river_rain snapshot values
  are skipped (identical to the chart's 5-minute series).
- The daily backfill runs twice (01:15 / 13:15 UTC) so a delayed Actions run
  cannot lose the ephemeral 1-minute data.

## Caveats

- GitHub Actions schedules are UTC, **min interval 5 minutes**, and may be
  delayed 5–30 min under load; schedules auto-disable after 60 days without
  repo activity. The twice-daily backfill job repairs any gaps this causes.
- Rain series contain outlier spikes (e.g. `2000.00 mm` on some gauges) that
  are almost certainly telemetry artifacts, not real rainfall. They are kept
  raw in the archive; filter with care in analyses.

## API reference (reverse-engineered)

Base: `https://api.rivernet.lk/api`

- `GET /overview/latest-status-paginated?deviceType={river_level|rain|river_rain}&page=N&isTableView=1` (public)
- `GET /overview/homepage-basin-list-with-multiple-levels` (public basin list)
- `GET /reports/{river-level|rainfall}/chart/minute/{from_ms}/{to_ms}?keys={device_key}&last24HoursData={0|1}&isPublic=1&isPublicHistory=1` (public)
  → `{"results":{"series":[{"name":…,"data":[{"x":_ms_,"y":_value_,"t":"ISO"}]}]}}`

Chart path timestamps are epoch **milliseconds**. `keys` is the snapshot API's
`deviceKey` field per station. Device inventory: 39 river-level, 17 rain,
22 river-rain gauges (~78 stations).