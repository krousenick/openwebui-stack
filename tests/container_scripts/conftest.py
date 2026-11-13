"""Fixtures for the container script tests: fake services, sample skills, stdin payloads."""

from __future__ import annotations

import base64
import io
import json
import pathlib
from typing import Any

import pytest

from fakes import TOKEN, FakeLiteLLM, FakeOpenWebUI, write_skill


@pytest.fixture
def litellm(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", TOKEN)
    service = FakeLiteLLM()
    yield service
    service.close()


@pytest.fixture
def openwebui(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENWEBUI_API_KEY", TOKEN)
    service = FakeOpenWebUI()
    yield service
    service.close()


@pytest.fixture
def skills_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """A skills root holding two valid packages, one per description shape."""
    root = tmp_path / "skills"
    write_skill(root, "alpha", "description: Does the alpha thing.")
    write_skill(root, "beta-two", "description: >-\n  Does the beta thing,\n  over two lines.\nlicense: MIT")
    return root


@pytest.fixture
def stdin_payload(monkeypatch: pytest.MonkeyPatch):
    """Put a payload on stdin the way tasks/container_script.yml does."""

    def feed(payload: dict[str, Any]) -> None:
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        monkeypatch.setattr("sys.stdin", io.StringIO(encoded))

    return feed
