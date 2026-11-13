"""The shared client and the exit contract every container script follows."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

import stack_api
from fakes import SCRIPTS_DIR, TOKEN
from stack_api import Api, ScriptError


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
