"""Record Keycloak logins with their location for the Grafana Keycloak dashboard.

Runs as the keycloak-login-tracker service (docker-compose.yml, rendered when
keycloak_login_tracker_enabled) on the litellm image's Python. Keycloak saves
the realm's logins in its event store (events_enabled in
tasks/keycloak_bootstrap.yml, kept 30 days); its log shows only failures.
Every --interval seconds the tracker:

  1. reads new LOGIN and LOGIN_ERROR events through the admin API
     (GET /admin/realms/<realm>/events) as the login-tracker client, whose
     service account holds only realm-management's view-events role;
  2. classifies the address Keycloak recorded. A public address is looked up
     in the local GeoIP file (--geoip-db, DB-IP City Lite, fetched by
     tasks/geoip.yml); a private one is "LAN" and one in --internal-networks
     (Docker's) "stack-internal", both without a location;
  3. compares each successful login that has a location with the same
     user's previous one. impossible_travel: more than --min-distance-km
     apart, and covering that distance in the time between them needs more
     than --max-speed-kmh. new_country: the user has located logins in the
     last --history-days, none from this country. LAN logins and failed
     logins are not compared;
  4. pushes one JSON line per login to Loki (service_name="keycloak-logins"),
     which the Keycloak dashboard's login panels and the KeycloakLogins alert
     rules (grafana/provisioning/alerting/keycloak-alerts.yml) read.

State is kept in memory. At start the tracker replays the event store for
--history-days to learn each user's history, reads from Loki which logins it
already pushed, and pushes the others that are at most --backfill-days old
(Loki refuses entries older than a week). It writes nothing to Keycloak.

--once runs one pass and prints the usual JSON report; with --check it
pushes nothing. --healthcheck exits 0 while the loop's heartbeat is fresh.

Environment: LOGIN_TRACKER_CLIENT_SECRET, the login-tracker client's secret.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import ipaddress
import json
import math
import os
import sys
import time
import urllib.parse
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import mmdb
import stack_api
from stack_api import Api, ScriptError

EVENT_TYPES = ("LOGIN", "LOGIN_ERROR")
STREAM = "keycloak-logins"
HEARTBEAT = Path(os.environ.get("LOGIN_TRACKER_HEARTBEAT", "/tmp/keycloak-login-tracker.heartbeat"))
DAY_MS = 86_400_000
# Events are re-read this far behind the newest one seen, in case Keycloak
# commits an event a little after its timestamp.
OVERLAP_MS = 10 * 60_000
PAGE = 100
MAX_HISTORY_EVENTS = 50_000
# Logins waiting for Loki beyond this are dropped, oldest first.
MAX_PENDING = 10_000
LOKI_PAGE = 5_000
EARTH_RADIUS_KM = 6371.0088
SCOPE_LABELS = {"lan": "LAN", "stack-internal": "stack-internal", "unknown": "unknown address"}


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    half_dphi = (phi2 - phi1) / 2
    half_dlambda = math.radians(lon2 - lon1) / 2
    a = math.sin(half_dphi) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(half_dlambda) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def parse_networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    try:
        return [ipaddress.ip_network(part.strip(), strict=False) for part in raw.split(",") if part.strip()]
    except ValueError as error:
        raise ScriptError(f"--internal-networks: {error}") from error


def classify(
    address: str, internal: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    """("public" | "lan" | "stack-internal" | "unknown", the parsed address)."""
    try:
        ip = ipaddress.ip_address(str(address or "").strip())
    except ValueError:
        return "unknown", None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback or any(ip.version == net.version and ip in net for net in internal):
        return "stack-internal", ip
    if not ip.is_global:
        return "lan", ip
    return "public", ip


def _name(node: Any) -> str:
    names = node.get("names") if isinstance(node, dict) else None
    return str(names.get("en") or "") if isinstance(names, dict) else ""


def describe(record: Any) -> dict[str, Any]:
    """The location fields of a GeoIP2/GeoLite2/DB-IP City record."""
    if not isinstance(record, dict):
        return {}
    country = record.get("country") or record.get("registered_country") or {}
    subdivisions = record.get("subdivisions") or []
    location = record.get("location") or {}
    found: dict[str, Any] = {
        "country": _name(country),
        "country_code": str(country.get("iso_code") or "") if isinstance(country, dict) else "",
        "region": _name(subdivisions[0]) if isinstance(subdivisions, list) and subdivisions else "",
        "city": _name(record.get("city")),
    }
    if isinstance(location, dict):
        lat, lon = location.get("latitude"), location.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            found["lat"], found["lon"] = round(float(lat), 4), round(float(lon), 4)
    return found


def place(fields: dict[str, Any]) -> str:
    """'City, Region, CC' with whatever the record has."""
    parts = [fields.get("city"), fields.get("region"), fields.get("country_code") or fields.get("country")]
    unique: list[str] = []
    for part in parts:
        if part and part not in unique:
            unique.append(str(part))
    return ", ".join(unique) or "unknown"


class Geo:
    """The GeoIP file, reopened when the playbook replaces it."""

    def __init__(self, path: str | Path, log: Callable[[str], None]):
        self.path = Path(path)
        self.log = log
        self.reader: mmdb.Reader | None = None
        self.signature: tuple[int, int, int] | None = None
        self.note = ""

    def _say(self, note: str) -> None:
        if note != self.note:
            self.note = note
            self.log(note)

    def refresh(self) -> None:
        try:
            stat = self.path.stat()
        except OSError:
            if self.reader is None:
                self._say(f"no GeoIP database at {self.path}: public addresses get no location")
            else:
                # The open mapping keeps the old copy readable.
                self._say(f"{self.path} is gone; still using the copy opened before")
            return
        signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if signature == self.signature:
            return
        self.signature = signature
        try:
            reader = mmdb.Reader(self.path)
        except (OSError, mmdb.InvalidDatabaseError) as error:
            self._say(f"cannot read the GeoIP database {self.path}: {error}")
            return
        old, self.reader = self.reader, reader
        if old is not None:
            old.close()
        built = dt.datetime.fromtimestamp(reader.build_epoch, dt.UTC).date() if reader.build_epoch else "unknown"
        self._say(f"GeoIP database {reader.database_type or self.path.name}, built {built}")

    def lookup(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> dict[str, Any]:
        if self.reader is None:
            return {}
        try:
            return describe(self.reader.get(ip))
        except (ValueError, mmdb.InvalidDatabaseError):
            return {}


@dataclasses.dataclass
class Located:
    time_ms: int
    lat: float
    lon: float
    location: str


@dataclasses.dataclass
class History:
    last: Located | None = None
    # country code -> time of the newest login from it
    countries: dict[str, int] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Rules:
    max_speed_kmh: float = 1000.0
    min_distance_km: float = 300.0
    history_ms: int = 30 * DAY_MS


def iso(time_ms: int) -> str:
    return dt.datetime.fromtimestamp(time_ms / 1000, dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def event_time(event: dict[str, Any]) -> int:
    try:
        return int(event.get("time") or 0)
    except (TypeError, ValueError):
        return 0


def event_id(event: dict[str, Any]) -> str:
    """Keycloak's event id; older releases have none, so a composite key stands in."""
    if event.get("id"):
        return str(event["id"])
    parts = (event_time(event), event.get("type"), event.get("userId"), event.get("sessionId"), event.get("ipAddress"))
    return ":".join(str(part or "") for part in parts)


class Analyzer:
    """Turns events into login records, keeping each user's history to compare against."""

    def __init__(self, geo: Geo, internal: list[ipaddress.IPv4Network | ipaddress.IPv6Network], rules: Rules):
        self.geo = geo
        self.internal = internal
        self.rules = rules
        self.users: dict[str, History] = {}
        # user id -> username, learned from the events that carry both
        self.names: dict[str, str] = {}

    def record(self, event: dict[str, Any]) -> dict[str, Any]:
        time_ms = event_time(event)
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        success = event.get("type") == "LOGIN"
        user_id = str(event.get("userId") or "")
        if user_id == "null":
            user_id = ""
        username = str(details.get("username") or "")
        # A failed second factor names the user only by id.
        if user_id and username:
            self.names[user_id] = username
        elif user_id:
            username = self.names.get(user_id, "")
        address = str(event.get("ipAddress") or "")
        scope, ip = classify(address, self.internal)
        record: dict[str, Any] = {
            "event_id": event_id(event),
            "time": iso(time_ms),
            # Set below. Loki takes a line's level from this field; without
            # it every line counts as an error for holding an "error" key.
            "level": "info",
            "type": str(event.get("type") or ""),
            "result": "success" if success else "failed",
            "error": str(event.get("error") or ""),
            "username": username or user_id,
            "user_id": user_id,
            "client_id": str(event.get("clientId") or ""),
            "ip": str(ip) if ip is not None else address,
            "ip_scope": scope,
            "country": "",
            "country_code": "",
            "region": "",
            "city": "",
            "location": SCOPE_LABELS.get(scope, "unknown"),
            "impossible_travel": False,
            "new_country": False,
            "flag": "",
        }
        if scope == "public" and ip is not None:
            found = self.geo.lookup(ip)
            record.update(found)
            record["location"] = place(found)
        if success and (user_id or username):
            self._compare(user_id or username, time_ms, record)
        flags = []
        if record["impossible_travel"]:
            flags.append("impossible travel")
        if record["new_country"]:
            flags.append("new country")
        record["flag"] = ", ".join(flags)
        record["level"] = "warn" if flags or not success else "info"
        return record

    def _compare(self, user: str, time_ms: int, record: dict[str, Any]) -> None:
        if "lat" not in record:
            return
        history = self.users.setdefault(user, History())
        cutoff = time_ms - self.rules.history_ms
        history.countries = {code: seen for code, seen in history.countries.items() if seen >= cutoff}
        previous = history.last
        if previous is not None and previous.time_ms >= cutoff:
            distance = distance_km(previous.lat, previous.lon, record["lat"], record["lon"])
            # Logins in the same minute count as one minute apart.
            hours = max((time_ms - previous.time_ms) / 3_600_000, 1 / 60)
            speed = distance / hours
            record.update(
                prev_location=previous.location,
                prev_time=iso(previous.time_ms),
                distance_km=round(distance, 1),
                hours_since_prev=round(hours, 2),
                speed_kmh=round(speed),
                impossible_travel=distance > self.rules.min_distance_km and speed > self.rules.max_speed_kmh,
            )
        code = record.get("country_code") or ""
        record["new_country"] = bool(code and history.countries and code not in history.countries)
        history.last = Located(time_ms, record["lat"], record["lon"], record["location"])
        if code:
            history.countries[code] = time_ms


class Keycloak:
    """The realm's event store, read as the login-tracker client (client credentials grant)."""

    def __init__(
        self,
        url: str,
        realm: str,
        client_id: str,
        secret: str,
        *,
        timeout: float = 15.0,
        clock: Callable[[], float] = time.time,
    ):
        self.api = Api(url, timeout=timeout)
        self.realm = realm
        self.client_id = client_id
        self.secret = secret
        self.clock = clock
        self._token = ""
        self._expires = 0.0

    def _realm_path(self) -> str:
        return urllib.parse.quote(self.realm, safe="")

    def token(self) -> str:
        if self._token and self.clock() < self._expires:
            return self._token
        form = {"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.secret}
        status, payload = self.api.request(
            "POST",
            f"/realms/{self._realm_path()}/protocol/openid-connect/token",
            body=urllib.parse.urlencode(form).encode(),
            content_type="application/x-www-form-urlencoded",
        )
        if status != 200 or not isinstance(payload, dict) or not payload.get("access_token"):
            detail = payload
            if isinstance(payload, dict):
                detail = payload.get("error_description") or payload.get("error") or payload.get("detail")
            raise ScriptError(f"Keycloak gave no token to {self.client_id} ({status or 'no answer'}): {detail}")
        self._token = str(payload["access_token"])
        lifetime = float(payload.get("expires_in") or 60)
        self._expires = self.clock() + max(lifetime - 30, lifetime / 2)
        return self._token

    def _get(self, path: str) -> Any:
        for attempt in (1, 2):
            self.api.token = self.token()
            status, payload = self.api.request("GET", path)
            if status == 401 and attempt == 1:
                self._token = ""
                continue
            if status == 403:
                raise ScriptError(
                    f"{self.client_id} may not read the realm's events (403): "
                    "its service account needs realm-management's view-events role"
                )
            if status != 200:
                raise ScriptError(f"GET {path.split('?')[0]} returned {status or 'no answer'}: {str(payload)[:200]}")
            return payload
        raise ScriptError("unreachable")  # pragma: no cover

    def events(self, since_ms: int, limit: int) -> list[dict[str, Any]]:
        """LOGIN and LOGIN_ERROR events at or after since_ms (newest first), at most limit.

        dateFrom takes a day, read in the server's time zone; asking from
        the day before covers any zone, and the times are filtered here.
        """
        day = dt.datetime.fromtimestamp(max(since_ms - DAY_MS, 0) / 1000, dt.UTC).strftime("%Y-%m-%d")
        found: list[dict[str, Any]] = []
        first = 0
        while len(found) < limit:
            query = [("type", kind) for kind in EVENT_TYPES]
            query += [("dateFrom", day), ("direction", "desc"), ("first", str(first)), ("max", str(PAGE))]
            page = self._get(f"/admin/realms/{self._realm_path()}/events?{urllib.parse.urlencode(query)}")
            if not isinstance(page, list):
                raise ScriptError("the events API did not answer with a list")
            events = [event for event in page if isinstance(event, dict)]
            found.extend(event for event in events if event_time(event) >= since_ms)
            # Newest first: once a page reaches back past since_ms, nothing older is needed.
            if len(page) < PAGE or any(event_time(event) < since_ms for event in events):
                break
            first += PAGE
        return found[:limit]


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class Loki:
    """The login stream in Loki: what is already there, and pushing new logins."""

    def __init__(self, url: str, *, timeout: float = 15.0):
        self.api = Api(url, timeout=timeout)

    def selector(self, realm: str) -> str:
        return f'{{service_name="{STREAM}", realm="{_label(realm)}"}}'

    def pushed_ids(self, realm: str, since_ms: int, until_ms: int) -> set[str]:
        """Event ids of the logins already in Loki between the two times."""
        ids: set[str] = set()
        end_ns = until_ms * 1_000_000
        while True:
            query = {
                "query": self.selector(realm),
                "start": str(since_ms * 1_000_000),
                "end": str(end_ns),
                "limit": str(LOKI_PAGE),
                "direction": "backward",
            }
            status, payload = self.api.request("GET", f"/loki/api/v1/query_range?{urllib.parse.urlencode(query)}")
            if status != 200 or not isinstance(payload, dict):
                raise ScriptError(
                    f"Loki did not answer the login query ({status or 'no answer'}): {str(payload)[:200]}"
                )
            streams = (payload.get("data") or {}).get("result") or []
            values = [value for stream in streams for value in stream.get("values") or []]
            for _, line in values:
                with contextlib.suppress(ValueError, TypeError, AttributeError):
                    ids.add(str(json.loads(line)["event_id"]))
            if len(values) < LOKI_PAGE:
                return ids
            oldest = min(int(stamp) for stamp, _ in values)
            if oldest + 1 >= end_ns:
                return ids
            end_ns = oldest + 1

    def push(self, realm: str, records: list[dict[str, Any]], stamps: list[int]) -> None:
        streams: dict[str, dict[str, Any]] = {}
        for stamp, record in sorted(zip(stamps, records, strict=True), key=lambda pair: pair[0]):
            result = record["result"]
            stream = streams.setdefault(
                result, {"stream": {"service_name": STREAM, "realm": realm, "result": result}, "values": []}
            )
            stream["values"].append([str(stamp * 1_000_000), json.dumps(record, ensure_ascii=False)])
        status, payload = self.api.request("POST", "/loki/api/v1/push", json_body={"streams": list(streams.values())})
        if status not in (200, 204):
            raise ScriptError(f"Loki refused the logins ({status or 'no answer'}): {str(payload)[:300]}")


@dataclasses.dataclass
class Settings:
    realm: str
    history_days: int = 30
    backfill_days: int = 6
    check: bool = False


class Tracker:
    def __init__(
        self,
        keycloak: Keycloak,
        loki: Loki,
        analyzer: Analyzer,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] | None = None,
    ):
        self.keycloak = keycloak
        self.loki = loki
        self.analyzer = analyzer
        self.settings = settings
        self.clock = clock
        self.log = log or (lambda message: print(message, flush=True))
        self.started = False
        self.cursor_ms = 0
        self.seen: dict[str, int] = {}
        self.pending: list[tuple[int, dict[str, Any]]] = []
        self.note = ""

    def say(self, note: str) -> None:
        """Log a problem or a recovery once, so a steady state logs nothing."""
        if note != self.note:
            self.note = note
            self.log(note)

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def _take(self, events: list[dict[str, Any]], push_from_ms: int, skip: set[str]) -> None:
        for event in sorted(events, key=lambda event: (event_time(event), event_id(event))):
            key = event_id(event)
            if key in self.seen:
                continue
            stamp = event_time(event)
            record = self.analyzer.record(event)
            self.seen[key] = stamp
            self.cursor_ms = max(self.cursor_ms, stamp)
            if stamp >= push_from_ms and key not in skip:
                self.pending.append((stamp, record))

    def start(self) -> None:
        """Learn each user's history from the event store and what Loki already has."""
        now = self._now_ms()
        push_from = now - self.settings.backfill_days * DAY_MS
        already = self.loki.pushed_ids(self.settings.realm, push_from, now + 60_000)
        events = self.keycloak.events(now - self.settings.history_days * DAY_MS, MAX_HISTORY_EVENTS)
        self.cursor_ms = now - OVERLAP_MS
        self._take(events, push_from, already)
        self.started = True

    def poll(self) -> None:
        since = self.cursor_ms - OVERLAP_MS
        self._take(self.keycloak.events(since, MAX_HISTORY_EVENTS), since, set())
        horizon = self.cursor_ms - 3 * OVERLAP_MS
        self.seen = {key: stamp for key, stamp in self.seen.items() if stamp >= horizon}

    def deliver(self) -> list[dict[str, Any]]:
        """Push what is waiting; returns the records pushed (or, on a check run, that would be)."""
        if len(self.pending) > MAX_PENDING:
            dropped = len(self.pending) - MAX_PENDING
            self.pending = self.pending[dropped:]
            self.log(f"Loki has been unreachable too long: dropped the {dropped} oldest logins")
        records = [record for _, record in self.pending]
        if not records or self.settings.check:
            return records
        self.loki.push(self.settings.realm, records, [stamp for stamp, _ in self.pending])
        self.pending = []
        return records

    def run_once(self) -> list[dict[str, Any]]:
        self.analyzer.geo.refresh()
        if not self.started:
            self.start()
        else:
            self.poll()
        return self.deliver()

    def report(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """The JSON report of a --once run (changed and actions as in stack_api.Api.report)."""
        actions = [f"push {len(records)} login(s) to Loki"] if records else []
        logins = [
            {key: record.get(key) for key in ("time", "username", "result", "ip", "location", "flag")}
            for record in records
        ]
        return {"changed": bool(actions), "check": self.settings.check, "actions": actions, "logins": logins}

    def loop(self, interval: float) -> None:
        self.log(f"tracking logins of realm {self.settings.realm} every {interval:.0f} s")
        while True:
            try:
                records = self.run_once()
            except ScriptError as error:
                self.say(f"pass failed: {error}")
            except Exception as error:  # noqa: BLE001 - the service must outlive one bad pass
                self.say(f"pass failed: {error!r}")
            else:
                self.say("reading Keycloak and writing to Loki")
                if records:
                    flagged = [record for record in records if record["flag"]]
                    self.log(f"pushed {len(records)} login(s) to Loki, {len(flagged)} flagged")
                    for record in flagged:
                        self.log(
                            f"flagged {record['username']} at {record['time']}: {record['flag']} "
                            f"({record.get('prev_location', '-')} -> {record['location']})"
                        )
            beat(interval)
            time.sleep(interval)


def beat(interval: float) -> None:
    with contextlib.suppress(OSError):
        HEARTBEAT.write_text(json.dumps({"at": time.time(), "interval": interval}), encoding="utf-8")


def healthy(now: float | None = None) -> bool:
    """True while the loop wrote its heartbeat within three intervals (at least two minutes)."""
    try:
        beat_data = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        age = (now if now is not None else time.time()) - float(beat_data["at"])
        return age < max(3 * float(beat_data.get("interval") or 30), 120)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keycloak-url", default="http://keycloak:8080", help="Keycloak (%(default)s)")
    parser.add_argument("--realm", default="platform", help="realm whose logins are tracked (%(default)s)")
    parser.add_argument("--client-id", default="login-tracker", help="client with view-events (%(default)s)")
    parser.add_argument("--loki-url", default="http://lgtm:3100", help="Loki (%(default)s)")
    parser.add_argument("--geoip-db", default="/geoip/dbip-city-lite.mmdb", help="MaxMind DB file (%(default)s)")
    parser.add_argument(
        "--internal-networks", default="172.16.0.0/12", help="comma-separated stack-internal networks (%(default)s)"
    )
    parser.add_argument("--max-speed-kmh", type=float, default=1000.0, help="impossible-travel speed (%(default)s)")
    parser.add_argument("--min-distance-km", type=float, default=300.0, help="never flag closer logins (%(default)s)")
    parser.add_argument("--history-days", type=int, default=30, help="history kept per user (%(default)s)")
    parser.add_argument("--backfill-days", type=int, default=6, help="oldest login pushed at start (%(default)s)")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between passes (%(default)s)")
    parser.add_argument("--timeout", type=float, default=15.0, help="seconds to wait for each answer (%(default)s)")
    parser.add_argument("--once", action="store_true", help="run one pass, print the JSON report and exit")
    parser.add_argument("--check", action="store_true", help="with --once: push nothing; report what would be pushed")
    parser.add_argument("--healthcheck", action="store_true", help="exit 0 while the loop's heartbeat is fresh")
    return parser


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = argument_parser().parse_args(argv)
    if args.check and not args.once:
        raise ScriptError("--check only works with --once")
    if not 0 < args.backfill_days <= 7:
        raise ScriptError("--backfill-days must be 1 to 7: Loki refuses entries older than a week")
    stream = sys.stderr if args.once else sys.stdout

    def log(message: str) -> None:
        # A --once run keeps stdout for the JSON report.
        print(message, file=stream, flush=True)

    secret = stack_api.secret("LOGIN_TRACKER_CLIENT_SECRET")
    keycloak = Keycloak(args.keycloak_url, args.realm, args.client_id, secret, timeout=args.timeout)
    rules = Rules(args.max_speed_kmh, args.min_distance_km, args.history_days * DAY_MS)
    analyzer = Analyzer(Geo(args.geoip_db, log), parse_networks(args.internal_networks), rules)
    settings = Settings(args.realm, args.history_days, args.backfill_days, args.check)
    tracker = Tracker(keycloak, Loki(args.loki_url, timeout=args.timeout), analyzer, settings, log=log)
    if args.once:
        return tracker.report(tracker.run_once())
    tracker.loop(args.interval)
    return tracker.report([])


if __name__ == "__main__":
    if "--healthcheck" in sys.argv[1:]:
        sys.exit(0 if healthy() else 1)
    stack_api.run(main)
