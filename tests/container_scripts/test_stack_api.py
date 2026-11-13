"""The shared client and the exit contract every container script follows."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

import stack_api
from fakes import SCRIPTS_DIR, TOKEN, FakeService
from stack_api import Api, ScriptError


class RateLimited(FakeService):
    """Answers 429 to the first `busy` requests, then 200; serves raw bytes at /raw."""

    def __init__(self, busy: int, retry_after: str | None = "7") -> None:
        super().__init__()
        self.busy = busy
        self.retry_after = retry_after

    def handle(self, request):
        if request.path == "/raw":
            return 200, b"# Not JSON\n"
        if self.busy:
            self.busy -= 1
            return 429, {"detail": "slow down"}, {"Retry-After": self.retry_after} if self.retry_after else {}
        return 200, {"ok": True}


@pytest.fixture
def waits(monkeypatch):
    """Record the sleeps a rate-limited write takes, without sleeping."""
    taken: list[float] = []
    monkeypatch.setattr(stack_api.time, "sleep", taken.append)
    return taken


@pytest.fixture
def limited():
    services = []

    def start(busy: int, retry_after: str | None = "7") -> RateLimited:
        services.append(RateLimited(busy, retry_after))
        return services[-1]

    yield start
    for service in services:
        service.close()


def test_request_returns_status_and_decoded_body(openwebui):
    api = Api(openwebui.url, TOKEN)
    assert api.request("GET", "/api/v1/configs/models") == (200, openwebui.config)
    assert api.request("GET", "/nowhere")[0] == 404


def test_request_reports_a_transport_failure_as_status_zero():
    status, payload = Api("http://127.0.0.1:1", timeout=2).request("GET", "/")
    assert status == 0
    assert "detail" in payload


def test_get_raises_outside_2xx(openwebui):
    with pytest.raises(ScriptError, match="GET /nowhere returned 404"):
        Api(openwebui.url, TOKEN).get("/nowhere")


def test_write_records_the_action_and_sends_it(openwebui):
    api = Api(openwebui.url, TOKEN)
    api.write("set config", "POST", "/api/v1/configs/models", json_body={"DEFAULT_MODELS": "x"})
    assert openwebui.config == {"DEFAULT_MODELS": "x"}
    assert api.report(extra=1) == {"changed": True, "check": False, "actions": ["set config"], "extra": 1}


def test_write_on_a_check_run_records_but_sends_nothing(openwebui):
    api = Api(openwebui.url, TOKEN, check=True)
    assert api.write("set config", "POST", "/api/v1/configs/models", json_body={}) is None
    assert openwebui.requests == []
    assert api.report() == {"changed": True, "check": True, "actions": ["set config"]}


def test_write_raises_with_the_action_and_status(openwebui):
    with pytest.raises(ScriptError, match="create thing: POST /nowhere returned 404"):
        Api(openwebui.url, TOKEN).write("create thing", "POST", "/nowhere")


def test_request_raw_returns_the_body_undecoded(limited):
    assert Api(limited(0).url).request_raw("GET", "/raw") == (200, b"# Not JSON\n")


def test_write_retries_a_rate_limited_request_after_retry_after(limited, waits):
    api = Api(limited(2).url, rate_limit_retries=3)
    assert api.write("upload", "POST", "/upload", json_body={}) == {"ok": True}
    assert waits == [7.0, 7.0]
    assert api.actions == ["upload"]


def test_write_gives_up_on_a_rate_limit_after_its_retries(limited, waits):
    with pytest.raises(ScriptError, match="upload: POST /upload returned 429"):
        Api(limited(5).url, rate_limit_retries=1).write("upload", "POST", "/upload", json_body={})
    assert waits == [7.0]


def test_write_does_not_retry_unless_asked(limited, waits):
    with pytest.raises(ScriptError, match="returned 429"):
        Api(limited(1).url).write("upload", "POST", "/upload", json_body={})
    assert waits == []


@pytest.mark.parametrize(
    ("retry_after", "wait"),
    [(None, 60.0), ("0", 1.0), ("2.5", 2.5), ("100000", 300.0), ("Wed, 21 Oct 2026 07:28:00 GMT", 60.0)],
)
def test_a_rate_limit_waits_what_retry_after_asks_within_bounds(limited, waits, retry_after, wait):
    Api(limited(1, retry_after).url, rate_limit_retries=1).write("upload", "POST", "/upload", json_body={})
    assert waits == [wait]


def test_secret_requires_the_environment_variable(monkeypatch):
    monkeypatch.delenv("SOME_KEY", raising=False)
    with pytest.raises(ScriptError, match="SOME_KEY is not set"):
        stack_api.secret("SOME_KEY")


def test_read_payload_decodes_base64_json(stdin_payload):
    stdin_payload({"enabled": True, "models": ["a"]})
    assert stack_api.read_payload() == {"enabled": True, "models": ["a"]}


def test_read_payload_rejects_bare_json(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"enabled": true}'))
    with pytest.raises(ScriptError, match="not a base64-encoded JSON payload"):
        stack_api.read_payload()


@pytest.mark.parametrize(
    ("value", "expected"), [(True, True), ("True", True), ("false", False), (0, False), ("", False)]
)
def test_truthy_reads_booleans_and_their_string_forms(value, expected):
    assert stack_api.truthy(value) is expected


def run_script(name: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a script by path, the way the container does."""
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *args],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ.get("PATH", ""), **env},
    )


def test_a_script_prints_one_json_report_and_exits_zero(openwebui):
    result = run_script(
        "openwebui_default_models.py", "--url", openwebui.url, "--models", "auto", env={"OPENWEBUI_API_KEY": TOKEN}
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "changed": True,
        "check": False,
        "actions": ["set default models to auto"],
        "default_models": "auto",
        "missing": ["auto"],
    }


def test_a_failing_script_exits_one_with_the_reason_on_stderr(openwebui):
    result = run_script("openwebui_default_models.py", "--url", openwebui.url, "--models", "auto", env={})
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "error: environment variable OPENWEBUI_API_KEY is not set"
