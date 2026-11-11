"""Shared plumbing for the provisioning scripts in this directory.

The compose template mounts this directory read-only into the litellm and
open-webui containers (container_scripts_mount, /opt/stack-scripts), and
ansible/roles/openwebui_stack/tasks/container_script.yml runs one script
per task with that container's own Python:

    docker exec [-i] [--env=NAME ...] <container> \
        python3 /opt/stack-scripts/<script>.py [options] [--check]

The scripts run there because the LiteLLM and Open WebUI admin APIs only
listen on container-local ports. Every script follows one contract, so the
task file can treat them alike:

  * Options arrive as arguments. Secrets arrive as environment variables,
    never as arguments: argv shows in `ps` and in a failed task's output.
    Structured input, for the scripts that take any, is a base64-encoded
    JSON object on stdin (read_payload).
  * Success exits 0 and prints one JSON object on stdout, the report:
    "changed" (bool), "check" (bool), "actions" (what was written, or with
    --check what a real run would write) and the script's own keys.
  * Failure exits 1 with "error: <reason>" on stderr.
  * --check writes nothing. Every write goes through Api.write, which
    records the action and skips the request on a check run.

Standard library only: the scripts must run on whatever Python the litellm
and open-webui images ship.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, NoReturn


class ScriptError(Exception):
    """A provisioning step failed. The message is what the operator reads."""


def secret(name: str) -> str:
    """Return a credential from the environment, where the playbook puts it."""
    value = os.environ.get(name, "")
    if not value:
        raise ScriptError(f"environment variable {name} is not set")
    return value


def read_payload() -> dict[str, Any]:
    """Decode the structured input: a base64-encoded JSON object on stdin.

    base64 because Ansible's templating turns a bare JSON string back into
    a dict and would deliver its Python repr instead.
    """
    try:
        payload = json.loads(base64.b64decode(sys.stdin.read().strip(), validate=True))
    except ValueError as error:
        raise ScriptError(f"stdin is not a base64-encoded JSON payload: {error}") from error
    if not isinstance(payload, dict):
        raise ScriptError("the stdin payload must be a JSON object")
    return payload


def truthy(value: Any) -> bool:
    """Read a flag that may arrive as a real boolean or as its string form.

    Ansible renders booleans inside a payload as true or "True" depending
    on its version, and Open WebUI has returned both for stored flags.
    """
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def argument_parser(description: str | None, *, url: str) -> argparse.ArgumentParser:
    """Build a parser with the options every script accepts."""
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--url", default=url, help="base URL of the service inside the container (default: %(default)s)"
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="seconds to wait for each API response (default: %(default)s)"
    )
    parser.add_argument("--check", action="store_true", help="write nothing; report what a real run would change")
    return parser


def _decode(raw: bytes) -> Any:
    """Parse a response body: JSON when it is JSON, else the text under "detail"."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"detail": raw.decode(errors="replace")[:500]}


class Api:
    """JSON-over-HTTP client for one service, with an optional bearer token."""

    def __init__(self, base_url: str, token: str | None = None, *, check: bool = False, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.check = check
        self.timeout = timeout
        self.actions: list[str] = []

    @classmethod
    def from_args(cls, args: argparse.Namespace, token: str | None = None) -> Api:
        return cls(args.url, token, check=args.check, timeout=args.timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        json_body: Any = None,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, Any]:
        """Send one request and return (status, decoded body).

        HTTP error statuses are returned, not raised, so a caller can
        branch on them (a 404 often means "create it"). A transport
        failure (refused connection, timeout) comes back as status 0.
        """
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if json_body is not None:
            body = json.dumps(json_body).encode()
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _decode(error.read())
        except OSError as error:
            return 0, {"detail": str(error)}

    def get(self, path: str, *, query: Mapping[str, str] | None = None) -> Any:
        """GET a resource that must exist; any status outside 2xx is an error."""
        status, payload = self.request("GET", path, query=query)
        if not 200 <= status < 300:
            raise ScriptError(f"GET {path} returned {status}: {json.dumps(payload)[:300]}")
        return payload

    def write(self, action: str, method: str, path: str, **request: Any) -> Any:
        """Record a change and make it, unless this is a check run.

        Returns the decoded response, or None on a check run, where the
        caller has to do without server-assigned values such as new ids.
        """
        self.actions.append(action)
        if self.check:
            return None
        status, payload = self.request(method, path, **request)
        if not 200 <= status < 300:
            raise ScriptError(f"{action}: {method} {path} returned {status}: {json.dumps(payload)[:300]}")
        return payload

    def report(self, **details: Any) -> dict[str, Any]:
        """Build the JSON report: the common keys, then the script's own."""
        return {"changed": bool(self.actions), "check": self.check, "actions": self.actions, **details}


def run(main: Callable[[], Mapping[str, Any]]) -> NoReturn:
    """Run a script's main() and turn its outcome into the exit contract."""
    try:
        report = main()
    except ScriptError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(report))
    sys.exit(0)
