"""keycloak_login_tracker.py: Keycloak logins with location and travel checks, pushed to Loki."""

from __future__ import annotations

import ipaddress
import json
import time
from pathlib import Path
from typing import Any

import pytest

import keycloak_login_tracker as tracker
import mmdb_writer
from fakes import FakeService, Request
from stack_api import ScriptError

REALM = "platform"
SECRET = "tracker-secret"
NOW_MS = 1_794_400_000_000  # 2026-11-11 12:26:40 UTC, a fixed clock
HOUR_MS = 3_600_000
MINUTE_MS = 60_000

# Public addresses with made-up places (the file is written by the test).
LONDON_IP, SEATTLE_IP, PORTLAND_IP, STOCKHOLM_IP = "81.2.69.142", "216.160.83.56", "50.114.0.1", "89.160.20.112"
PLACES = [
    ("81.2.69.0/24", mmdb_writer.city("London", "England", "United Kingdom", "GB", 51.5142, -0.0931)),
    ("216.160.83.0/24", mmdb_writer.city("Seattle", "Washington", "United States", "US", 47.6062, -122.3321)),
    ("50.114.0.0/16", mmdb_writer.city("Portland", "Oregon", "United States", "US", 45.5152, -122.6784)),
    ("89.160.20.0/24", mmdb_writer.city("Stockholm", "Stockholm", "Sweden", "SE", 59.3293, 18.0686)),
]
INTERNAL = tracker.parse_networks("172.16.0.0/12")


def event(
    minutes: float,
    ip: str,
    *,
    user: str = "alice",
    kind: str = "LOGIN",
    error: str | None = None,
    client: str = "open-webui",
    event_id: str | None = None,
) -> dict[str, Any]:
    """A Keycloak EventRepresentation, `minutes` after NOW_MS minus a day."""
    stamp = NOW_MS - 24 * HOUR_MS + int(minutes * MINUTE_MS)
    return {
        "id": event_id or f"ev-{user}-{stamp}-{ip}",
        "time": stamp,
        "type": kind,
        "realmId": "realm-uuid",
        "clientId": client,
        "userId": None if kind == "LOGIN_ERROR" else f"id-{user}",
        "sessionId": "s1",
        "ipAddress": ip,
        "error": error,
        "details": {"username": user, "auth_method": "openid-connect"},
    }


@pytest.fixture
def geoip(tmp_path: Path) -> Path:
    return mmdb_writer.write(tmp_path / "dbip-city-lite.mmdb", PLACES, database_type="DBIP-City-Lite")


@pytest.fixture
def analyzer(geoip: Path) -> tracker.Analyzer:
    geo = tracker.Geo(geoip, lambda message: None)
    geo.refresh()
    return tracker.Analyzer(geo, INTERNAL, tracker.Rules(max_speed_kmh=1000, min_distance_km=300))


# --- addresses and places -------------------------------------------------


@pytest.mark.parametrize(
    ("address", "scope"),
    [
        ("8.8.8.8", "public"),
        ("2a02:aa1::1", "public"),
        ("::ffff:8.8.8.8", "public"),
        ("10.0.108.121", "lan"),
        ("192.168.1.20", "lan"),
        ("100.64.3.4", "lan"),  # carrier-grade NAT / Tailscale
        ("fd00::5", "lan"),
        ("::ffff:10.0.0.1", "lan"),
        ("172.19.0.1", "stack-internal"),
        ("127.0.0.1", "stack-internal"),
        ("::1", "stack-internal"),
        ("", "unknown"),
        ("null", "unknown"),
        ("not-an-address", "unknown"),
    ],
)
def test_classifies_addresses(address, scope):
    assert tracker.classify(address, INTERNAL)[0] == scope


def test_internal_networks_are_configurable():
    networks = tracker.parse_networks("192.168.50.0/24, fd12::/16")
    assert tracker.classify("192.168.50.7", networks)[0] == "stack-internal"
    assert tracker.classify("172.19.0.1", networks)[0] == "lan"
    with pytest.raises(ScriptError, match="internal-networks"):
        tracker.parse_networks("10.0.0.0/33")


def test_distance_matches_known_city_pairs():
    assert tracker.distance_km(51.5074, -0.1278, 48.8566, 2.3522) == pytest.approx(343.5, abs=2)
    assert tracker.distance_km(47.6062, -122.3321, 40.7128, -74.0060) == pytest.approx(3866, abs=10)
    assert tracker.distance_km(10, 20, 10, 20) == 0


def test_describes_a_city_record_and_its_place():
    found = tracker.describe(PLACES[1][1])
    assert found == {
        "country": "United States",
        "country_code": "US",
        "region": "Washington",
        "city": "Seattle",
        "lat": 47.6062,
        "lon": -122.3321,
    }
    assert tracker.place(found) == "Seattle, Washington, US"
    assert tracker.place({"country": "Sweden", "country_code": "SE"}) == "SE"
    assert tracker.place({}) == "unknown"
    assert tracker.describe({"country": {"iso_code": "US"}, "location": {"latitude": "x"}}) == {
        "country": "",
        "country_code": "US",
        "region": "",
        "city": "",
    }
    assert tracker.describe(None) == {}


# --- analysis ----------------------------------------------------------------


def test_a_lan_login_has_no_location_and_is_not_compared(analyzer):
    record = analyzer.record(event(0, "10.0.108.121"))
    assert record["ip_scope"] == "lan"
    assert record["location"] == "LAN"
    assert "lat" not in record
    assert record["country"] == record["city"] == ""
    assert record["result"] == "success"
    assert record["username"] == "alice"
    assert record["flag"] == ""
    assert record["level"] == "info"
    assert "alice" not in analyzer.users and "id-alice" not in analyzer.users


def test_docker_addresses_are_stack_internal(analyzer):
    assert analyzer.record(event(0, "172.19.0.1"))["location"] == "stack-internal"


def test_a_public_login_gets_its_place(analyzer):
    record = analyzer.record(event(0, SEATTLE_IP))
    assert record["ip_scope"] == "public"
    assert record["location"] == "Seattle, Washington, US"
    assert (record["country"], record["country_code"], record["city"]) == ("United States", "US", "Seattle")
    assert (record["lat"], record["lon"]) == (47.6062, -122.3321)
    assert record["new_country"] is False  # the first located login is the baseline
    assert record["impossible_travel"] is False
    assert "prev_location" not in record


def test_a_public_address_missing_from_the_database_is_unknown(analyzer):
    record = analyzer.record(event(0, "8.8.8.8"))
    assert record["ip_scope"] == "public"
    assert record["location"] == "unknown"
    assert "lat" not in record


def test_flags_a_jump_too_fast_to_travel(analyzer):
    analyzer.record(event(0, SEATTLE_IP))
    record = analyzer.record(event(60, LONDON_IP))  # 7700 km in an hour
    assert record["impossible_travel"] is True
    assert record["new_country"] is True
    assert record["flag"] == "impossible travel, new country"
    assert record["level"] == "warn"
    assert record["prev_location"] == "Seattle, Washington, US"
    assert record["distance_km"] == pytest.approx(7700, abs=5)
    assert record["hours_since_prev"] == 1.0
    assert record["speed_kmh"] == pytest.approx(7700, abs=5)


def test_a_plausible_flight_is_not_flagged(analyzer):
    analyzer.record(event(0, SEATTLE_IP))
    record = analyzer.record(event(12 * 60, LONDON_IP))  # 640 km/h
    assert record["impossible_travel"] is False
    assert record["new_country"] is True
    assert record["flag"] == "new country"


def test_nearby_places_are_never_flagged_however_fast(analyzer):
    analyzer.record(event(0, SEATTLE_IP))
    record = analyzer.record(event(0, PORTLAND_IP))  # 234 km, the same minute
    assert record["distance_km"] < 300
    assert record["hours_since_prev"] == 0.02  # counted as one minute
    assert record["speed_kmh"] > 10_000
    assert record["impossible_travel"] is False
    assert record["new_country"] is False


def test_lan_logins_between_located_ones_are_skipped(analyzer):
    analyzer.record(event(0, LONDON_IP))
    analyzer.record(event(10, "10.0.0.5"))
    record = analyzer.record(event(30, SEATTLE_IP))
    assert record["prev_location"] == "London, England, GB"
    assert record["impossible_travel"] is True


def test_failed_logins_are_recorded_but_change_no_history(analyzer):
    analyzer.record(event(0, SEATTLE_IP))
    failed = analyzer.record(event(5, STOCKHOLM_IP, kind="LOGIN_ERROR", error="invalid_user_credentials"))
    assert failed["result"] == "failed"
    assert failed["error"] == "invalid_user_credentials"
    assert failed["user_id"] == ""
    assert failed["username"] == "alice"
    assert failed["location"] == "Stockholm, SE"  # the region repeats the city
    assert failed["flag"] == ""
    later = analyzer.record(event(6 * 60, PORTLAND_IP))
    assert later["prev_location"] == "Seattle, Washington, US"
    assert later["new_country"] is False


def test_users_are_compared_only_with_themselves(analyzer):
    analyzer.record(event(0, SEATTLE_IP, user="alice"))
    record = analyzer.record(event(10, LONDON_IP, user="bob"))
    assert record["flag"] == ""
    assert "prev_location" not in record


def test_new_country_looks_back_over_the_history_window(geoip):
    geo = tracker.Geo(geoip, lambda message: None)
    geo.refresh()
    rules = tracker.Rules(max_speed_kmh=1000, min_distance_km=300, history_ms=30 * tracker.DAY_MS)
    analyzer = tracker.Analyzer(geo, INTERNAL, rules)
    analyzer.record(event(0, SEATTLE_IP))
    assert analyzer.record(event(60 * 24 * 10, LONDON_IP))["new_country"] is True  # 10 days later
    # Back to the US 25 days after the first login: US is still in the window.
    assert analyzer.record(event(60 * 24 * 25, PORTLAND_IP))["new_country"] is False
    # Sweden on day 60: every earlier login has aged out, so Sweden is the new baseline.
    assert analyzer.record(event(60 * 24 * 60, STOCKHOLM_IP))["new_country"] is False
    # The previous login is older than the window: no comparison at all.
    record = analyzer.record(event(60 * 24 * 100, LONDON_IP))
    assert "prev_location" not in record
    assert record["new_country"] is False


def test_without_a_database_public_logins_have_no_place(tmp_path):
    notes: list[str] = []
    geo = tracker.Geo(tmp_path / "missing.mmdb", notes.append)
    geo.refresh()
    analyzer = tracker.Analyzer(geo, INTERNAL, tracker.Rules())
    assert analyzer.record(event(0, SEATTLE_IP))["location"] == "unknown"
    assert notes == [f"no GeoIP database at {tmp_path / 'missing.mmdb'}: public addresses get no location"]


def test_the_database_is_reopened_when_replaced(tmp_path):
    path = mmdb_writer.write(tmp_path / "geo.mmdb", PLACES[:1])
    notes: list[str] = []
    geo = tracker.Geo(path, notes.append)
    geo.refresh()
    assert geo.lookup(ipaddress.ip_address(SEATTLE_IP)) == {}
    replacement = mmdb_writer.write(tmp_path / "geo.new", PLACES)
    replacement.replace(path)
    geo.refresh()
    assert geo.lookup(ipaddress.ip_address(SEATTLE_IP))["city"] == "Seattle"
    # A damaged replacement keeps the working copy.
    broken = tmp_path / "geo.bad"
    broken.write_text("<html>404</html>", encoding="utf-8")
    broken.replace(path)
    geo.refresh()
    assert geo.lookup(ipaddress.ip_address(SEATTLE_IP))["city"] == "Seattle"
    assert any("cannot read the GeoIP database" in note for note in notes)
    # Gone: the open copy still answers.
    path.unlink()
    geo.refresh()
    assert geo.lookup(ipaddress.ip_address(SEATTLE_IP))["city"] == "Seattle"
    assert notes[-1] == f"{path} is gone; still using the copy opened before"


# --- Keycloak and Loki --------------------------------------------------------


class FakeKeycloak(FakeService):
    """The token endpoint and GET /admin/realms/<realm>/events, newest first."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []
        self.tokens = 0
        self.valid = {"token-1"}
        self.forbidden = False

    def handle(self, request: Request) -> tuple[int, Any]:
        if request.method == "POST" and request.path == f"/realms/{REALM}/protocol/openid-connect/token":
            form = dict(pair.split("=", 1) for pair in request.body.decode().split("&"))
            if form != {"grant_type": "client_credentials", "client_id": "login-tracker", "client_secret": SECRET}:
                return 401, {"error": "invalid_client", "error_description": "Invalid client credentials"}
            self.tokens += 1
            return 200, {"access_token": f"token-{self.tokens}", "expires_in": 300}
        if request.method == "GET" and request.path == f"/admin/realms/{REALM}/events":
            if request.headers.get("Authorization", "").removeprefix("Bearer ") not in self.valid:
                return 401, {"error": "HTTP 401 Unauthorized"}
            if self.forbidden:
                return 403, {"error": "unknown_error"}
            types = request.query_all.get("type", [])
            assert types == ["LOGIN", "LOGIN_ERROR"]
            assert request.query["direction"] == "desc"
            day = request.query["dateFrom"]
            first, size = int(request.query["first"]), int(request.query["max"])
            matching = [
                item
                for item in sorted(self.events, key=lambda item: -item["time"])
                if item["type"] in types and time.strftime("%Y-%m-%d", time.gmtime(item["time"] / 1000)) >= day
            ]
            return 200, matching[first : first + size]
        return 404, {"error": "not found"}

    def event_requests(self) -> list[Request]:
        return [request for request in self.requests if request.path.endswith("/events")]


class FakeLoki(FakeService):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[tuple[dict[str, str], int, dict[str, Any]]] = []
        self.down = False

    def handle(self, request: Request) -> tuple[int, Any]:
        if self.down:
            return 503, {"detail": "Ingester not ready"}
        if request.method == "POST" and request.path == "/loki/api/v1/push":
            for stream in request.json()["streams"]:
                for stamp, line in stream["values"]:
                    self.lines.append((stream["stream"], int(stamp), json.loads(line)))
            return 204, None
        if request.method == "GET" and request.path == "/loki/api/v1/query_range":
            assert request.query["query"] == f'{{service_name="keycloak-logins", realm="{REALM}"}}'
            start, end = int(request.query["start"]), int(request.query["end"])
            found = [(stamp, record) for _, stamp, record in self.lines if start <= stamp <= end]
            found.sort(key=lambda item: -item[0])
            found = found[: int(request.query["limit"])]
            values = [[str(stamp), json.dumps(record)] for stamp, record in found]
            return 200, {"status": "success", "data": {"resultType": "streams", "result": [{"values": values}]}}
        return 404, None

    def pushed(self) -> list[str]:
        return [record["event_id"] for _, _, record in self.lines]


class Clock:
    def __init__(self, now_ms: int = NOW_MS) -> None:
        self.now = now_ms / 1000

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def keycloak():
    service = FakeKeycloak()
    yield service
    service.close()


@pytest.fixture
def loki():
    service = FakeLoki()
    yield service
    service.close()


def make_tracker(keycloak, loki, analyzer, *, check=False, clock=None, notes=None):
    clock = clock or Clock()
    client = tracker.Keycloak(keycloak.url, REALM, "login-tracker", SECRET, clock=clock)
    settings = tracker.Settings(REALM, history_days=30, backfill_days=6, check=check)
    log = notes.append if notes is not None else (lambda message: None)
    return tracker.Tracker(client, tracker.Loki(loki.url), analyzer, settings, clock=clock, log=log)


def test_keycloak_pages_through_events_and_reuses_its_token(keycloak):
    keycloak.events = [event(index, LONDON_IP) for index in range(250)]
    client = tracker.Keycloak(keycloak.url, REALM, "login-tracker", SECRET, clock=Clock())
    everything = client.events(0, 10_000)
    assert len(everything) == 250
    assert [request.query["first"] for request in keycloak.event_requests()] == ["0", "100", "200"]
    keycloak.forget_requests()
    newest = client.events(keycloak.events[-30]["time"], 10_000)
    assert len(newest) == 30
    assert len(keycloak.event_requests()) == 1  # the first page already reaches back far enough
    assert keycloak.tokens == 1


def test_keycloak_asks_for_events_from_the_day_before(keycloak):
    client = tracker.Keycloak(keycloak.url, REALM, "login-tracker", SECRET, clock=Clock())
    client.events(NOW_MS, 10)
    assert keycloak.event_requests()[0].query["dateFrom"] == time.strftime(
        "%Y-%m-%d", time.gmtime((NOW_MS - tracker.DAY_MS) / 1000)
    )


def test_keycloak_fetches_a_new_token_after_a_401(keycloak):
    client = tracker.Keycloak(keycloak.url, REALM, "login-tracker", SECRET, clock=Clock())
    client.events(0, 10)
    keycloak.valid = {"token-2"}  # the first token was revoked
    client.events(0, 10)
    assert keycloak.tokens == 2


def test_keycloak_errors_name_the_cause(keycloak):
    wrong = tracker.Keycloak(keycloak.url, REALM, "login-tracker", "wrong", clock=Clock())
    with pytest.raises(ScriptError, match="Invalid client credentials"):
        wrong.events(0, 10)
    keycloak.forbidden = True
    client = tracker.Keycloak(keycloak.url, REALM, "login-tracker", SECRET, clock=Clock())
    with pytest.raises(ScriptError, match="view-events"):
        client.events(0, 10)
    unreachable = tracker.Keycloak("http://127.0.0.1:9", REALM, "login-tracker", SECRET, timeout=2)
    with pytest.raises(ScriptError, match="no answer"):
        unreachable.events(0, 10)


def test_start_replays_history_and_backfills_what_loki_lacks(keycloak, loki, analyzer):
    old = event(-8 * 24 * 60, SEATTLE_IP)  # 9 days ago: history only
    pushed_before = event(-60, SEATTLE_IP)
    fresh = event(0, LONDON_IP)
    keycloak.events = [old, pushed_before, fresh]
    loki.lines.append(({}, pushed_before["time"] * 1_000_000, {"event_id": pushed_before["id"]}))
    run = make_tracker(keycloak, loki, analyzer)
    records = run.run_once()
    assert [record["event_id"] for record in records] == [fresh["id"]]
    assert loki.pushed() == [pushed_before["id"], fresh["id"]]
    # The fresh login is compared with the one pushed before, which the replay learned.
    assert records[0]["prev_location"] == "Seattle, Washington, US"
    assert records[0]["impossible_travel"] is True


def test_the_pushed_lines(keycloak, loki, analyzer):
    keycloak.events = [event(0, SEATTLE_IP), event(1, "10.0.0.9", kind="LOGIN_ERROR", error="user_not_found")]
    make_tracker(keycloak, loki, analyzer).run_once()
    (good_labels, good_stamp, good), (bad_labels, _, bad) = sorted(loki.lines, key=lambda line: line[1])
    assert good_labels == {"service_name": "keycloak-logins", "realm": REALM, "result": "success"}
    assert bad_labels == {"service_name": "keycloak-logins", "realm": REALM, "result": "failed"}
    assert good_stamp == keycloak.events[0]["time"] * 1_000_000
    assert good["time"] == tracker.iso(keycloak.events[0]["time"])
    assert good["client_id"] == "open-webui"
    assert good["ip"] == SEATTLE_IP and good["city"] == "Seattle"
    assert bad["location"] == "LAN" and bad["error"] == "user_not_found"
    assert (good["level"], bad["level"]) == ("info", "warn")


def test_polls_push_each_new_login_once(keycloak, loki, analyzer):
    clock = Clock()
    run = make_tracker(keycloak, loki, analyzer, clock=clock)
    keycloak.events = [event(0, SEATTLE_IP)]
    run.run_once()
    keycloak.events.append(event(24 * 60 + 1, PORTLAND_IP))  # one minute after "now"
    clock.now += 120
    run.run_once()
    clock.now += 30
    run.run_once()
    assert loki.pushed() == [item["id"] for item in keycloak.events]


def test_logins_wait_while_loki_is_down(keycloak, loki, analyzer):
    clock = Clock()
    run = make_tracker(keycloak, loki, analyzer, clock=clock)
    keycloak.events = [event(24 * 60 - 30, SEATTLE_IP)]  # half an hour ago
    run.run_once()
    keycloak.events.append(event(24 * 60 + 1, LONDON_IP))
    loki.down = True
    clock.now += 60
    with pytest.raises(ScriptError, match="Loki refused"):
        run.run_once()
    loki.down = False
    clock.now += 60
    run.run_once()
    assert loki.pushed() == [item["id"] for item in keycloak.events]
    assert loki.lines[-1][2]["impossible_travel"] is True


def test_start_waits_for_loki(keycloak, loki, analyzer):
    keycloak.events = [event(0, SEATTLE_IP)]
    loki.down = True
    run = make_tracker(keycloak, loki, analyzer)
    with pytest.raises(ScriptError, match="Loki did not answer"):
        run.run_once()
    assert not run.started
    loki.down = False
    run.run_once()
    assert loki.pushed() == [keycloak.events[0]["id"]]


def test_a_check_run_pushes_nothing(keycloak, loki, analyzer):
    keycloak.events = [event(0, SEATTLE_IP)]
    run = make_tracker(keycloak, loki, analyzer, check=True)
    report = run.report(run.run_once())
    assert loki.lines == []
    assert report["check"] is True
    assert report["changed"] is True
    assert report["actions"] == ["push 1 login(s) to Loki"]
    assert report["logins"][0]["location"] == "Seattle, Washington, US"


def test_main_runs_one_pass(keycloak, loki, geoip, monkeypatch, capsys):
    monkeypatch.setenv("LOGIN_TRACKER_CLIENT_SECRET", SECRET)
    keycloak.events = [event(24 * 60 - 5, SEATTLE_IP)]  # five minutes ago by the real clock below
    keycloak.events[0]["time"] = int(time.time() * 1000) - 5 * MINUTE_MS
    report = tracker.main(
        ["--once", "--keycloak-url", keycloak.url, "--loki-url", loki.url, "--geoip-db", str(geoip), "--realm", REALM]
    )
    assert report["actions"] == ["push 1 login(s) to Loki"]
    assert loki.pushed() == [keycloak.events[0]["id"]]
    assert "GeoIP database DBIP-City-Lite" in capsys.readouterr().err


def test_main_refuses_bad_options(monkeypatch):
    monkeypatch.setenv("LOGIN_TRACKER_CLIENT_SECRET", SECRET)
    with pytest.raises(ScriptError, match="--check only works with --once"):
        tracker.main(["--check"])
    with pytest.raises(ScriptError, match="1 to 7"):
        tracker.main(["--once", "--backfill-days", "8"])
    monkeypatch.delenv("LOGIN_TRACKER_CLIENT_SECRET")
    with pytest.raises(ScriptError, match="LOGIN_TRACKER_CLIENT_SECRET"):
        tracker.main(["--once"])


def test_the_healthcheck_reads_the_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setattr(tracker, "HEARTBEAT", tmp_path / "heartbeat")
    assert tracker.healthy() is False
    tracker.beat(30)
    assert tracker.healthy() is True
    assert tracker.healthy(now=time.time() + 300) is False
