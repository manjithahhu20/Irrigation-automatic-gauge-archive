"""RIVERNET.LK archive client.

Public + authenticated access to the RIVERNET.LK flood early-warning API
(https://rivernet.lk, https://api.rivernet.lk). Endpoint details were
reverse-engineered from the Flutter web app (main.dart.js) and verified
against the live API.

Key facts discovered during engineering:
  * Live snapshot:    GET  /api/overview/latest-status-paginated?deviceType=..&page=..&isTableView=1
                      (no auth; device types: river_level, rain, river_rain)
  * Station history:  GET  /api/reports/{river-level|rainfall}/chart/minute/{from_ms}/{to_ms}
                      ?keys={device_key}&last24HoursData={0|1}&isPublic=1&isPublicHistory=1
                      (no auth; keys = the snapshot API's deviceKey field)
  * Chart response:   {"results":{"series":[{"name":..,"data":[{"x":_ms_,"y":_val_,"t":"ISO"}]}]}}
                      last24HoursData=1 -> 1-minute points (current day);
                      last24HoursData=0 -> 5-minute points (historical ranges)
  * Chart path timestamps are epoch MILLISECONDS (not microseconds, not dates).
  * The web app routes requests through https://api.rivernet.lk/cache-api.php?path=..
    (a browser-side cache proxy); the real API answers directly with the same
    payloads, so this client talks to the API directly.
  * Login exists (POST /api/login, form-encoded) but no endpoint used by this
    archive requires it.

An authorised scrape is in place: the site owner granted permission for this
educational archive. robots.txt explicitly allows bots and LLM agents; the
CRAWL_DELAY below honours the Crawl-delay: 1 directive.
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

API_BASE = os.environ.get("RIVERNET_API", "https://api.rivernet.lk/api")
USER_AGENT = os.environ.get(
    "RIVERNET_USER_AGENT",
    "rivernet-lk-archive/1.0 (educational flood-data archive; runs with site owner permission)",
)
SRI_LANKA = timezone(timedelta(hours=5, minutes=30))
ROUND_MINUTES = 2  # app rounds query times to a 2-minute grid
CRAWL_DELAY = float(os.environ.get("RIVERNET_CRAWL_DELAY", "1.2"))  # seconds between requests

DEVICE_TYPES = ("river_level", "rain", "river_rain")
REPORT_TYPES = {"river_level": "river-level", "rain": "rainfall", "river_rain": "river-level"}

SSL_VERIFY = os.environ.get("RIVERNET_SSL_VERIFY", "1").lower() in ("1", "true", "yes")


class RivernetError(Exception):
    """Raised for unrecoverable API failures."""


def _ssl_context() -> ssl.SSLContext:
    if SSL_VERIFY:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def now_local() -> datetime:
    """Current time in Sri Lanka timezone (+05:30)."""
    return datetime.now(SRI_LANKA)


def to_micros(dt: datetime) -> int:
    """Convert a (naive or aware) datetime to epoch microseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SRI_LANKA)
    return int(dt.timestamp() * 1_000_000)


def to_millis(dt: datetime) -> int:
    """Convert a (naive or aware) datetime to epoch milliseconds (chart paths)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SRI_LANKA)
    return int(dt.timestamp() * 1000)


def floor_minutes(dt: datetime, minutes: int = ROUND_MINUTES) -> datetime:
    """Floor a datetime to the given minute grid (site rounds to 2 min)."""
    dt = dt.replace(second=0, microsecond=0)
    return dt - timedelta(minutes=dt.minute % minutes)


def parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string into an aware datetime (UTC)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SRI_LANKA)
    return dt


def iso_utc(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def iso_local(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.astimezone(SRI_LANKA).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Rivernet:
    """Minimal authenticated client for the RIVERNET.LK API."""

    username: Optional[str] = None
    password: Optional[str] = None
    api_base: str = API_BASE
    timeout: int = 90
    token: Optional[str] = None
    _ctx: ssl.SSLContext = field(default_factory=_ssl_context)

    def __post_init__(self) -> None:
        self.username = self.username or os.environ.get("RIVERNET_USERNAME")
        self.password = self.password or os.environ.get("RIVERNET_PASSWORD")

    # -- low-level ---------------------------------------------------------

    def _open(self, url: str, method: str, data: Optional[bytes],
              headers: Dict[str, str], attempt: int = 0) -> Tuple[int, bytes, Dict[str, str]]:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "application/json, text/plain, */*")
        for key, val in headers.items():
            req.add_header(key, val)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if attempt < 4:
                time.sleep(min(2 ** attempt, 20))
                return self._open(url, method, data, headers, attempt + 1)
            raise RivernetError(f"network error for {url}: {exc}") from exc

    def request(self, path: str, method: str = "GET", params: Optional[Dict[str, Any]] = None,
                body: Optional[Dict[str, Any]] = None, form: Optional[Dict[str, Any]] = None,
                retries: int = 5, auth_retry: bool = True) -> Tuple[int, Any, Dict[str, str]]:
        url = self.api_base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        payload = None
        headers: Dict[str, str] = {}
        if form is not None:
            payload = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            status, raw, resp_headers = self._open(url, method, payload, headers)
            if status == 401 and auth_retry and self.token and attempt < retries - 1:
                self.login()
                time.sleep(CRAWL_DELAY)
                continue
            if status >= 500 and attempt < retries - 1:
                time.sleep(min(2 ** attempt, 30))
                continue
            content_type = resp_headers.get("Content-Type", "")
            parsed: Any = raw
            if "json" in content_type:
                try:
                    parsed = json.loads(raw.decode("utf-8", errors="replace"))
                except (ValueError, UnicodeDecodeError) as exc:
                    last_error = exc
            return status, parsed, resp_headers
        raise RivernetError(f"exhausted retries for {url}: {last_error}")

    def get_json(self, path: str, **params: Any) -> Tuple[int, Any]:
        status, parsed, _ = self.request(path, params=params)
        return status, parsed

    # -- auth --------------------------------------------------------------

    def login(self) -> str:
        if not self.username or not self.password:
            raise RivernetError(
                "RIVERNET_USERNAME / RIVERNET_PASSWORD are required for authenticated endpoints. "
                "Ask the RIVERNET.LK site owner for a login before enabling history collection."
            )
        status, payload, _ = self.request(
            "/login", method="POST", form={"username": self.username, "password": self.password},
            auth_retry=False, retries=3,
        )
        if status != 200 or not isinstance(payload, dict):
            raise RivernetError(
                f"login failed (HTTP {status}): {payload if not isinstance(payload, (bytes, bytearray)) else payload[:200]!r}"
            )
        token = payload.get("token")
        if not token:
            raise RivernetError(f"login response contained no token: {str(payload)[:200]}")
        self.token = str(token)
        return self.token

    # -- data --------------------------------------------------------------

    def latest_status(self, device_type: str, page: int = 1) -> Any:
        if device_type not in DEVICE_TYPES:
            raise RivernetError(f"unknown device type {device_type!r}")
        status, payload = self.get_json(
            "/overview/latest-status-paginated",
            deviceType=device_type, page=page, isTableView="1",
        )
        if status != 200:
            raise RivernetError(f"latest-status failed (HTTP {status}): {str(payload)[:200]}")
        return payload

    def all_latest(self, device_type: str) -> List[Any]:
        """Return every device snapshot for a device type (follows pagination)."""
        devices: List[Any] = []
        page = 1
        while True:
            data = self.latest_status(device_type, page=page)
            results = data.get("results") or {}
            devices.extend(results.get("data") or [])
            pagination = results.get("pagination") or {}
            if page >= int(pagination.get("last_page") or 1):
                break
            page += 1
            time.sleep(CRAWL_DELAY)
        return devices

    def chart(self, device_type: str, device_key: str, from_ms: int, to_ms: int,
              last_24h: bool = False) -> Any:
        """Fetch a station's history over [from_ms, to_ms] (epoch milliseconds).

        Public endpoint: keys is the snapshot API's deviceKey field. With
        last24HoursData=1 the server returns ~1-minute points for the current
        day; with =0 it returns 5-minute points for the requested range.
        """
        if device_type not in DEVICE_TYPES:
            raise RivernetError(f"unknown device type {device_type!r}")
        path = (
            f"/reports/{REPORT_TYPES[device_type]}/chart/minute"
            f"/{int(from_ms)}/{int(to_ms)}"
        )
        params = {
            "keys": device_key,
            "last24HoursData": "1" if last_24h else "0",
            "isPublic": "1",
            "isPublicHistory": "1",
        }
        status, payload = self.get_json(path, **params)
        if status != 200:
            raise RivernetError(
                f"chart failed ({device_key}, HTTP {status}): {str(payload)[:200]}"
            )
        return payload

    def history_excel(self, mode: str, from_us: int, to_us: int,
                      rainfall_keys: Iterable[str] = (),
                      river_level_keys: Iterable[str] = (),
                      river_rain_keys: Iterable[str] = ()) -> bytes:
        """Bulk history download (returns .xlsx bytes). Requires auth."""
        params = {
            "rainfallKeys": "[" + ",".join(f'"{k}"' for k in rainfall_keys) + "]",
            "riverLevelKeys": "[" + ",".join(f'"{k}"' for k in river_level_keys) + "]",
            "riverRainKeys": "[" + ",".join(f'"{k}"' for k in river_rain_keys) + "]",
        }
        status, raw, headers = self.request(
            f"/reports/history/excel/{mode}/{int(from_us)}/{int(to_us)}",
            params=params,
        )
        if status != 200:
            raise RivernetError(f"history/excel failed (HTTP {status}): {str(raw)[:200]}")
        return raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode()


# -- row extraction -------------------------------------------------------------

def snapshot_row(device: dict) -> Optional[Dict[str, Any]]:
    """Normalise one latest-status-paginated device into an archive row."""
    latest = device.get("latest")
    if not latest:
        return None
    rainy = device.get("additional") or {}
    value_dt = parse_iso(latest.get("datetime") or latest.get("time"))
    received_dt = parse_iso(
        (latest.get("latestRecord") or {}).get("received_at")
    ) or value_dt
    if value_dt is None:
        return None
    return {
        "unit_id": device.get("unitId") or device.get("deviceKey") or "?",
        "device_key": device.get("deviceKey") or "",
        "type": device.get("type") or "",
        "region": device.get("region") or "",
        "location": device.get("location") or latest.get("name") or "",
        "lat": (rainy.get("coordinates") or {}).get("latitude"),
        "lon": (rainy.get("coordinates") or {}).get("longitude"),
        "max_level": rainy.get("maxLevel"),
        "alert_type": latest.get("alertType") or "",
        "value": _to_float(latest.get("latestLevel")),
        "datetime_utc": iso_utc(value_dt),
        "datetime_local_530": iso_local(value_dt),
        "received_at_utc": iso_utc(received_dt),
        "source": "snapshot",
    }


def _first_not_none(item: dict, *keys: str) -> Any:
    """First key present with a non-None value (0 is a valid value)."""
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _chart_wallclock(value: Any) -> Optional[datetime]:
    """Chart stamps carry Sri Lanka wall-clock time, labeled as if UTC.

    Verified: the server emits telemetry timestamps in local (+05:30) wall
    time with a "+00:00"/"Z" suffix (and matching epoch x values), so the
    stamp's wall clock IS the local time. Interpret it as such.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=None).replace(tzinfo=SRI_LANKA)


def _chart_epoch(value: Any) -> Optional[datetime]:
    """Highcharts-style numeric x values are ms (µs) since epoch of the
    local-labeled stamp; the derived wall clock is the local time."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not 1e10 <= number <= 9e18:  # plausible ms/µs since epoch
        return None
    if number >= 1e14:  # microseconds
        number = number / 1e6
    return datetime.fromtimestamp(number, tz=timezone.utc).replace(tzinfo=SRI_LANKA)


def chart_points(payload: Any, device_key: str) -> List[Dict[str, Any]]:
    """Extract {datetime_utc/datetime_local/value} points from a chart payload.

    The exact JSON shape differs per report type and server version, so this
    normalises several candidate layouts. Unknown schemas dump a raw sample to
    state/schema/ for inspection instead of silently dropping data.
    """
    results = payload.get("results") if isinstance(payload, dict) else None
    if results is None:
        return []
    points: List[Dict[str, Any]] = []
    candidates: List[Any] = []

    def collect(container: Any) -> None:
        if isinstance(container, dict):
            for key in ("data", "points", "series", "lineData", "rows"):
                item = container.get(key)
                if isinstance(item, list):
                    candidates.append(item)
                elif isinstance(item, dict) and isinstance(item.get("data"), list):
                    candidates.append(item["data"])
            for value in container.values():
                collect(value)
        elif isinstance(container, list):
            for item in container:
                if isinstance(item, (dict, list)):
                    collect(item)

    collect(results)

    for candidate in candidates:
        for item in candidate:
            if not isinstance(item, dict):
                continue
            dt = _chart_wallclock(
                item.get("datetime") or item.get("date") or item.get("time")
                or item.get("t")  # verified schema: {"x": ms, "y": val, "t": ISO}
            )
            if dt is None:
                dt = _chart_epoch(item.get("x") or item.get("datetime"))
            value = _to_float(_first_not_none(item, "value", "level", "y"))
            if dt is not None and value is not None:
                points.append({
                    "device_key": device_key,
                    "datetime_utc": iso_utc(dt),
                    "datetime_local_530": iso_local(dt),
                    "value": value,
                    "source": "chart",
                })
    return points