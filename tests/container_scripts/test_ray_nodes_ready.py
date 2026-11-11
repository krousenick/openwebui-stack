"""The Ray readiness check the vllm_compute role runs in the Ray head container."""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def ray_nodes_ready(monkeypatch):
    """Import the script with a stand-in for the ray package, which only the container has."""
    ray = types.ModuleType("ray")
    ray.cluster = [{"Alive": True}, {"Alive": True}, {"Alive": False}]
    ray.init = lambda address: None
    ray.nodes = lambda: ray.cluster
    monkeypatch.setitem(sys.modules, "ray", ray)
    monkeypatch.delitem(sys.modules, "ray_nodes_ready", raising=False)
    import ray_nodes_ready

    return ray_nodes_ready


@pytest.mark.parametrize(("expect", "exit_code"), [("2", 0), ("1", 0), ("3", 1)])
def test_exit_code_tells_whether_enough_nodes_are_alive(ray_nodes_ready, capsys, expect, exit_code):
    assert ray_nodes_ready.main(["--expect", expect]) == exit_code
    assert capsys.readouterr().out == f"2 of {expect} Ray nodes alive\n"
