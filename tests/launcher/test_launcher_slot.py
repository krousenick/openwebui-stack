"""vllm-launcher-slot.py: one model at a time in the LLM Launcher (role vllm_compute).

The module hooks the launcher's API module, advisor and server manager.
These tests drive it against small fakes of those internals, shaped after
Local-LLM-Launcher-GUI v0.6.1 (api.py, registry.py, engines/base.py).
"""

from __future__ import annotations

import importlib.util
import pathlib
import shlex
import sys
import threading
from typing import Any

import pytest

SLOT_FILE = pathlib.Path(__file__).resolve().parents[2] / "ansible/roles/vllm_compute/files/vllm-launcher-slot.py"
_spec = importlib.util.spec_from_file_location("stack_slot", SLOT_FILE)
stack_slot = importlib.util.module_from_spec(_spec)
sys.modules["stack_slot"] = stack_slot
_spec.loader.exec_module(stack_slot)

GEMMA = "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"
QWEN = "Qwen/Qwen3-8B"
TOTAL = 24564
GEMMA_CONFIG = {
    "port": 8000,
    "served_model_name": "gemma-4-26b-a4b",
    "max_model_len": 262144,
    "gpu_memory_utilization": 0.9,
    "reasoning_parser": "gemma4",
}


class FakeServer:
    def __init__(self, server_id: str, model: str, config: dict[str, Any], *, engine: str = "vllm-native") -> None:
        self.server_id = server_id
        self.model_label = model
        self.engine = engine
        self.port = int(config.get("port", 8000))
        self.config = {key: value for key, value in config.items() if key not in ("host", "hf_token", "api_key")}
        self.config_save_error = None
        self.load_status = "loading"
        self.started_at = f"2026-11-11T00:00:{len(server_id):02d}"
        self.running = True
        self.healthy = False
        self.notes: list[str] = []

    def is_running(self) -> bool:
        return self.running

    def health(self) -> bool:
        return self.running and self.healthy

    def note(self, text: str) -> None:
        self.notes.append(text)

    def status(self) -> dict[str, Any]:
        return {"id": self.server_id, "model": self.model_label, "port": self.port, "running": self.running}


class FakeServers:
    """registry.ServerManager: launch, stop, remove, list, get, build_spec."""

    def __init__(self) -> None:
        self.servers: dict[str, FakeServer] = {}
        self.launches: list[tuple[str, str, dict[str, Any]]] = []
        self.failures: list[str] = []

    def add(self, model: str, config: dict[str, Any], *, healthy: bool = True) -> FakeServer:
        server = FakeServer(f"srv{len(self.servers) + 1}", model, config)
        server.healthy = healthy
        server.load_status = "successful" if healthy else "loading"
        self.servers[server.server_id] = server
        return server

    def launch(self, engine_mode, model, config, llamacpp_binary=None, vllm_binary=None, hardware=None):
        self.launches.append((engine_mode, model["repo_id"], dict(config)))
        if self.failures:
            raise RuntimeError(self.failures.pop(0))
        server = self.add(model["repo_id"], config, healthy=False)
        server.engine = engine_mode
        return server

    def stop(self, server_id: str) -> bool:
        self.servers[server_id].running = False
        return True

    def remove(self, server_id: str) -> bool:
        self.servers[server_id].running = False
        del self.servers[server_id]
        return True

    def list(self) -> list[dict[str, Any]]:
        return [server.status() for server in self.servers.values()]

    def get(self, server_id: str) -> FakeServer | None:
        return self.servers.get(server_id)

    def build_spec(self, engine_mode, model, config, llamacpp_binary=None, vllm_binary=None):
        if config.get("extra_args") == "--broken '":
            raise ValueError("Extra arguments have invalid quoting")
        argv = ["vllm", "serve", model["repo_id"]]
        if config.get("tool_call_parser"):
            argv += ["--tool-call-parser", config["tool_call_parser"]]
        argv += shlex.split(config.get("extra_args") or "")
        return {"argv": argv, "port": config["port"]}


class LaunchRequest:
    def __init__(self, engine_mode: str, repo_id: str, config: dict[str, Any]) -> None:
        self.engine_mode = engine_mode
        self.repo_id = repo_id
        self.config = config


class FakeApi:
    """local_llm_launcher.api: module globals the slot replaces or calls."""

    LaunchRequest = LaunchRequest

    def __init__(self, servers: FakeServers) -> None:
        self.servers = servers
        self.free_mb = TOTAL - 400
        self.models = [{"repo_id": "a/first"}, {"repo_id": GEMMA}, {"repo_id": QWEN}]
        self._hw_lock = threading.Lock()
        self._hw_cache: dict[str, Any] = {"data": "cached"}
        self.relaunches: list[LaunchRequest] = []

    def get_hardware(self, max_age: float = 5.0) -> dict[str, Any]:
        return {"gpus": [{"index": 0, "name": "RTX 4090", "vram_total_mb": TOTAL, "vram_free_mb": self.free_mb}]}

    def installed_models(self) -> list[dict[str, Any]]:
        return list(self.models)

    def api_launch(self, body: LaunchRequest) -> dict[str, Any]:
        # The real api_launch ends in servers.launch, looked up at call time.
        self.relaunches.append(body)
        return self.servers.launch(body.engine_mode, {"repo_id": body.repo_id}, dict(body.config)).status()


class FakeAdvisor:
    def __init__(self) -> None:
        self.free_seen: list[int] = []

    def advise(self, engine, model, config, hw):
        self.free_seen.append(hw["gpus"][0]["vram_free_mb"])
        return {"overall": {"level": "green", "headline": "Fits", "details": []}, "flags": {}}

    def presets(self, engine, model, hw):
        self.free_seen.append(hw["gpus"][0]["vram_free_mb"])
        return [{"name": "Safe (recommended)", "config": {"max_model_len": 8192}}]


class FakeCatalog:
    @staticmethod
    def safe_launch_config(engine_mode, config):
        return {k: v for k, v in config.items() if k not in ("host", "hf_token", "api_key")}, None


class FakeBackends:
    @staticmethod
    def validate(mode, model, config, hardware=None, binary="vllm", *, wait=True):
        if config.get("attention_backend") == "B12X_ATTN":
            raise ValueError("vLLM v0.30 names the attention backend B12X, not B12X_ATTN. Select B12X.")
        return {}


class FakeMtp:
    @staticmethod
    def resolve(*args, **kwargs):
        return {"level": "green", "message": "", "args": []}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


SETTINGS = stack_slot.SlotSettings(
    node="vllm-node-01",
    port=8000,
    api_base="http://192.0.2.20:8000/v1",
    configured_repo_id=GEMMA,
    configured_served_name="gemma-4-26b-a4b",
    reasoning_efforts=("none", "low", "medium", "high"),
    repetition_detection={"min_count": 4},
    profiles={
        "gemma-4-26b-a4b": {
            "reasoning_efforts": ["none", "low", "medium", "high"],
            "reasoning_effort": "medium",
            "adaptive_router_preferences": {"quality_tier": 2},
        }
    },
)


@pytest.fixture
def env():
    servers = FakeServers()
    api = FakeApi(servers)
    advisor = FakeAdvisor()
    clock = Clock()
    spawned: list[Any] = []
    logs: list[str] = []
    slot = stack_slot.Slot(
        SETTINGS,
        api=api,
        advisor=advisor,
        catalog=FakeCatalog(),
        vllm_backends=FakeBackends(),
        mtp=FakeMtp(),
        port_in_use=lambda port: any(s.running and s.port == port for s in servers.servers.values()),
        # nvidia-smi: 400 MiB for other programs, plus what each running server holds.
        read_gpus=lambda: {0: (TOTAL, 400 + 21764 * sum(s.running for s in servers.servers.values()))},
        read_served=lambda port: {"id": "Qwen3-8B", "max_model_len": 40960},
        clock=clock,
        sleep=clock.sleep,
        log=logs.append,
    )
    slot.spawn = spawned.append
    slot.install()

    class Env:
        pass

    e = Env()
    e.servers, e.api, e.advisor, e.clock, e.slot, e.spawned, e.logs = servers, api, advisor, clock, slot, spawned, logs
    return e


def gemma_running(env, *, sample_first: bool = True) -> FakeServer:
    """Gemma on port 8000, after the slot saw the GPU while nothing ran (400 MiB in use)."""
    if sample_first:
        env.api.get_hardware()
    server = env.servers.add(GEMMA, GEMMA_CONFIG)
    env.api.free_mb = 2000
    return server


# ------------------------------------------------------------------ fit advice


def test_advice_rates_the_whole_gpu_not_what_the_running_model_leaves(env):
    gemma_running(env)
    report = env.slot.advise("vllm", {"repo_id": QWEN}, {}, env.api.get_hardware())
    assert env.advisor.free_seen == [TOTAL - 400]
    assert report["overall"]["details"][0].startswith("Rated for the whole GPU. Launching stops gemma-4-26b-a4b")
    assert "gemma-4-26b-a4b starts again" in report["overall"]["details"][0]


def test_the_dashboard_still_sees_the_real_free_memory(env):
    gemma_running(env)
    assert env.api.get_hardware()["gpus"][0]["vram_free_mb"] == 2000


def test_without_a_sample_only_a_small_reserve_counts_as_used(env):
    gemma_running(env, sample_first=False)
    env.slot.advise("vllm", {"repo_id": QWEN}, {}, env.api.get_hardware())
    assert env.advisor.free_seen == [TOTAL - stack_slot.UNSAMPLED_RESERVE_MB]


def test_no_sample_is_taken_right_after_a_stop(env):
    gemma = gemma_running(env)
    env.slot.stop(gemma.server_id)
    env.api.free_mb = 3000  # memory not released yet
    env.api.get_hardware()
    assert env.slot.baseline.other_used(0) == 400
    env.clock.sleep(20)
    env.api.free_mb = TOTAL - 300
    env.api.get_hardware()
    assert env.slot.baseline.other_used(0) == 300


def test_advice_on_the_running_model_says_it_restarts(env):
    gemma_running(env)
    report = env.slot.advise("vllm", {"repo_id": GEMMA}, {}, env.api.get_hardware())
    assert report["overall"]["details"][0].startswith("Rated for the whole GPU. Launching restarts gemma-4-26b-a4b")


def test_another_port_is_flagged_because_the_slot_port_wins(env):
    report = env.slot.advise("vllm", {"repo_id": QWEN}, {"port": 8100}, env.api.get_hardware())
    assert report["flags"]["port"]["level"] == "yellow"
    assert "port 8000" in report["flags"]["port"]["message"]


def test_nested_advice_calls_are_not_adjusted_twice(env):
    gemma_running(env)
    hardware = {**env.api.get_hardware(), stack_slot.ADJUSTED: True}
    env.slot.advise("vllm", {"repo_id": QWEN}, {}, hardware)
    assert env.advisor.free_seen == [2000]


# ------------------------------------------------------------------- tuning


def test_the_running_model_opens_first_with_its_settings_as_a_preset(env):
    gemma_running(env)
    assert env.slot.installed_models()[0]["repo_id"] == GEMMA
    presets = env.slot.presets("vllm", {"repo_id": GEMMA}, env.api.get_hardware())
    assert presets[0]["name"] == "Running now"
    assert presets[0]["config"] == {k: v for k, v in GEMMA_CONFIG.items() if k != "port"}
    assert presets[1]["name"] == "Safe (recommended)"


def test_other_models_get_their_usual_presets(env):
    gemma_running(env)
    presets = env.slot.presets("vllm", {"repo_id": QWEN}, env.api.get_hardware())
    assert [p["name"] for p in presets] == ["Safe (recommended)"]


# ------------------------------------------------------------------ swapping


def test_a_launch_stops_the_running_model_and_takes_the_slot_port(env):
    gemma = gemma_running(env)
    server = env.slot.launch("vllm-native", {"repo_id": QWEN}, {"port": 8100, "max_model_len": 32768})
    assert gemma.running is False
    assert gemma.server_id in env.slot.intentional
    assert env.servers.launches[-1] == (
        "vllm-native",
        QWEN,
        {"port": 8000, "max_model_len": 32768, "served_model_name": "Qwen3-8B", "tool_call_parser": "hermes"},
    )
    assert server.notes[-1] == f"stack: replaced gemma-4-26b-a4b (server {gemma.server_id}) on port 8000"
    assert len(env.spawned) == 1
    assert env.api._hw_cache["data"] is None
    assert env.logs == []  # the GPU memory came back before the new model started


def test_the_configured_model_keeps_its_configured_name(env):
    env.slot.launch("vllm-native", {"repo_id": GEMMA}, {})
    assert env.servers.launches[-1][2]["served_model_name"] == "gemma-4-26b-a4b"


# -------------------------------------------------------------- tool calling


@pytest.mark.parametrize(
    ("repo", "parser"),
    [
        ("QuantTrio/Qwen3-Coder-30B-A3B-Instruct-AWQ", "qwen3_coder"),
        ("Qwen/Qwen3-0.6B", "hermes"),
        ("Qwen/Qwen2.5-7B-Instruct", "hermes"),
        ("RedHatAI/Qwen3.8-27B-INT4", "hermes"),
        (GEMMA, "gemma4"),
        ("meta-llama/Llama-3.1-8B-Instruct", "llama3_json"),
        ("mistralai/Devstral-Small-2505", "mistral"),
        ("microsoft/phi-4", None),
    ],
)
def test_tool_formats_follow_the_model_family(repo, parser):
    assert stack_slot.tool_parser_for(repo) == parser


def test_a_launch_without_a_tool_format_gets_the_family_one_and_auto_tool_choice(env):
    server = env.slot.launch("vllm-native", {"repo_id": QWEN}, {})
    config = env.servers.launches[-1][2]
    assert config["tool_call_parser"] == "hermes"
    assert server.notes[0].startswith("stack: tool-calling format hermes chosen")
    spec = env.api.servers.build_spec("vllm-native", {"repo_id": QWEN}, config)
    assert spec["argv"][-3:] == ["--tool-call-parser", "hermes", "--enable-auto-tool-choice"]


def test_a_chosen_tool_format_is_kept_and_auto_tool_choice_not_repeated(env):
    config = {"tool_call_parser": "mistral", "extra_args": "--enable-auto-tool-choice"}
    env.slot.launch("vllm-native", {"repo_id": QWEN}, config)
    launched = env.servers.launches[-1][2]
    assert launched["tool_call_parser"] == "mistral"
    argv = env.api.servers.build_spec("vllm-native", {"repo_id": QWEN}, launched)["argv"]
    assert argv.count("--enable-auto-tool-choice") == 1


def test_advice_names_the_tool_format_or_warns_without_one(env):
    known = env.slot.advise("vllm", {"repo_id": QWEN}, {}, env.api.get_hardware())
    assert known["flags"]["tool_call_parser"]["level"] == "green"
    assert "'hermes'" in known["flags"]["tool_call_parser"]["message"]
    unknown = env.slot.advise("vllm", {"repo_id": "microsoft/phi-4"}, {}, env.api.get_hardware())
    assert unknown["flags"]["tool_call_parser"]["level"] == "yellow"
    chosen = env.slot.advise("vllm", {"repo_id": QWEN}, {"tool_call_parser": "hermes"}, env.api.get_hardware())
    assert "tool_call_parser" not in chosen["flags"]


@pytest.mark.parametrize(
    "config",
    [{"extra_args": "--broken '"}, {"attention_backend": "B12X_ATTN"}],
    ids=["bad extra arguments", "refused backend"],
)
def test_settings_the_launcher_refuses_never_stop_the_running_model(env, config):
    gemma = gemma_running(env)
    with pytest.raises(ValueError):
        env.slot.launch("vllm-native", {"repo_id": QWEN}, config)
    assert gemma.running is True
    assert env.servers.launches == []


def test_a_port_held_by_something_else_stops_nothing(env):
    env.slot.port_in_use = lambda port: True
    with pytest.raises(RuntimeError, match="did not start"):
        env.slot.launch("vllm-native", {"repo_id": QWEN}, {})
    assert env.servers.launches == []


def test_a_second_launch_during_a_swap_is_refused(env):
    holder = threading.Thread(target=env.slot.swap_lock.acquire)
    holder.start()
    holder.join()
    with pytest.raises(RuntimeError, match="Another launch"):
        env.slot.launch("vllm-native", {"repo_id": QWEN}, {})


# ------------------------------------------------------------------ rollback


def test_a_launch_that_fails_to_start_puts_the_previous_model_back(env):
    gemma_running(env)
    env.servers.failures = ["The server process failed to start. Check the logs for details."]
    with pytest.raises(RuntimeError, match="gemma-4-26b-a4b was started again"):
        env.slot.launch("vllm-native", {"repo_id": QWEN}, {})
    (relaunch,) = env.api.relaunches
    assert (relaunch.repo_id, relaunch.config["served_model_name"]) == (GEMMA, "gemma-4-26b-a4b")
    assert env.servers.launches[-1][2]["port"] == 8000


def test_a_model_that_exits_while_loading_is_rolled_back(env):
    gemma_running(env)
    qwen = env.slot.launch("vllm-native", {"repo_id": QWEN}, {})
    qwen.running = False  # out of memory while loading
    env.spawned[0]()
    (relaunch,) = env.api.relaunches
    assert relaunch.repo_id == GEMMA
    assert any("starting gemma-4-26b-a4b again" in note for note in qwen.notes)


def test_a_model_that_answers_is_kept(env):
    gemma_running(env)
    qwen = env.slot.launch("vllm-native", {"repo_id": QWEN}, {})
    qwen.healthy = True
    env.spawned[0]()
    qwen.running = False
    assert env.api.relaunches == []


def test_stopping_the_new_model_by_hand_is_not_a_failure(env):
    gemma_running(env)
    qwen = env.slot.launch("vllm-native", {"repo_id": QWEN}, {})
    env.slot.stop(qwen.server_id)
    env.spawned[0]()
    assert env.api.relaunches == []


def test_no_rollback_target_when_the_old_model_never_answered(env):
    gemma = gemma_running(env)
    gemma.healthy = False
    gemma.load_status = "loading"
    env.slot.launch("vllm-native", {"repo_id": QWEN}, {})
    assert env.spawned == []


# --------------------------------------------------------------- slot status


def test_the_status_route_needs_the_token(env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(stack_slot.TOKEN_ENV, raising=False)
    assert env.slot.status("Bearer x")[0] == 404
    monkeypatch.setenv(stack_slot.TOKEN_ENV, "s3cret-token-value")
    assert env.slot.status(None)[0] == 401
    assert env.slot.status("Bearer wrong")[0] == 401
    assert env.slot.status("Bearer s3cret-token-value")[0] == 200


def test_the_status_route_works_through_fastapi(env, monkeypatch: pytest.MonkeyPatch):
    """The route as the launcher serves it: FastAPI must hand the handler the request, not a query field."""
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    router = fastapi.APIRouter(prefix="/api")
    stack_slot.add_status_route(router, env.slot)
    app = fastapi.FastAPI()
    app.include_router(router)
    client = TestClient(app)
    monkeypatch.setenv(stack_slot.TOKEN_ENV, "s3cret-token-value")
    assert client.get("/api/stack/slot").status_code == 401
    answer = client.get("/api/stack/slot", headers={"Authorization": "Bearer s3cret-token-value"})
    assert (answer.status_code, answer.json()["state"]) == (200, "empty")


def test_status_of_an_empty_and_a_loading_slot(env):
    assert env.slot.slot_document()["state"] == "empty"
    env.servers.add(QWEN, {"port": 8000, "served_model_name": "Qwen3-8B"}, healthy=False)
    document = env.slot.slot_document()
    assert (document["state"], document["repo_id"]) == ("loading", QWEN)
    assert "litellm" not in document


def test_status_of_a_served_model_uses_what_vllm_reports(env):
    env.servers.add(QWEN, {"port": 8000, "served_model_name": "Qwen3-8B", "reasoning_parser": "qwen3"})
    document = env.slot.slot_document()
    assert document["state"] == "serving"
    assert document["litellm"] == {
        "model_name": "Qwen3-8B",
        "api_base": "http://192.0.2.20:8000/v1",
        "max_input_tokens": 40960,
        "max_tokens": 40960,
        "supports_reasoning": True,
        "reasoning_efforts": ["none", "low", "medium", "high"],
        "reasoning_effort": None,
        "repetition_detection": {"min_count": 4},
        "adaptive_router_preferences": None,
        "description": f"Qwen3-8B - vLLM on vllm-node-01 ({QWEN}, started from the LLM Launcher)",
    }


def test_the_configured_model_keeps_its_profile(env):
    entry = env.slot.litellm_entry("gemma-4-26b-a4b", 262144, GEMMA_CONFIG, GEMMA)
    assert entry["reasoning_effort"] == "medium"
    assert entry["adaptive_router_preferences"] == {"quality_tier": 2}


def test_a_model_without_a_reasoning_parser_does_not_reason(env):
    entry = env.slot.litellm_entry("plain", 8192, {"served_model_name": "plain"}, "org/plain")
    assert (entry["supports_reasoning"], entry["reasoning_efforts"], entry["reasoning_effort"]) == (False, [], None)


def test_settings_are_read_from_the_rendered_file(tmp_path):
    path = tmp_path / "slot.json"
    path.write_text('{"node": "n", "port": 8000, "rollback": false, "profiles": {"m": {"reasoning_effort": "low"}}}')
    settings = stack_slot.SlotSettings.load(path)
    assert (settings.port, settings.rollback, settings.api_base) == (8000, False, "http://127.0.0.1:8000/v1")
    with pytest.raises(ValueError, match="incomplete"):
        stack_slot.SlotSettings.from_dict({"node": "n"})
