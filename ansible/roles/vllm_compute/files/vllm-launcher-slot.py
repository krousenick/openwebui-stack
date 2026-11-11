"""Single-model slot for the stack's LLM Launcher (role: vllm_compute).

stack_entry.py loads this module when the role renders the slot settings
(vllm_launcher_swap on a node whose launcher owns the primary model). The
GPU then holds one model at a time, on the port LiteLLM routes to:

  Launch replaces   Every launch (Launch page, Favorites, a rollback) first
                    runs the checks the launcher runs before it starts a
                    process, so settings it would refuse never stop the
                    running model. Then it stops what runs, waits until the
                    port and the GPU memory are free, and starts the new
                    server on the primary port.
  Rollback          When the new server exits before it answers /health,
                    the settings it replaced are started again.
  Whole-GPU fit     The fit advice and the Models page badges count the
                    memory the launcher's own server holds as free, because
                    a launch frees it first. Memory other programs hold
                    still counts as used: it is sampled while no launcher
                    server runs.
  Tune              The Launch page opens on the running model, and the
                    settings it runs with are its first preset ("Running
                    now"): change any of them and launch to restart it.
  Slot status       GET /api/stack/slot tells the stack what is served here
                    and how LiteLLM should list it (launcher_follower.py on
                    the stack host). The gate lets this one route through
                    without a sign-in; the bearer token in
                    LAUNCHER_STACK_STATUS_TOKEN guards it.

Fails closed: install() looks up every launcher internal it hooks before it
changes anything, and stack_entry.py refuses to start when one is missing
(a new upstream version moved it), instead of running a launcher whose
launches would pile models onto a full GPU.
"""

from __future__ import annotations

import dataclasses
import hmac
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

SLOT_FILE_ENV = "LAUNCHER_STACK_SLOT_FILE"
DEFAULT_SLOT_FILE = "/etc/stack/slot.json"
TOKEN_ENV = "LAUNCHER_STACK_STATUS_TOKEN"
API_KEY_ENV = "VLLM_API_KEY"

PRESET_NAME = "Running now"
# hw dicts this module already adjusted: advise() calls itself many times
# while presets() searches for the longest context, and must not redo it.
ADJUSTED = "_stack_whole_gpu"
# Memory a GPU is assumed to keep for the driver and other programs when no
# sample without a launcher server exists yet.
UNSAMPLED_RESERVE_MB = 512
# The GPU counts as released once each card is back within this much of
# what other programs held before the launch.
RELEASE_SLACK_MB = 1024
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
BUSY = "Another launch is replacing the model right now. Wait until it finishes, then try again."


@dataclasses.dataclass(frozen=True)
class SlotSettings:
    """What the role renders into the slot file (templates/vllm-launcher-slot.json.j2)."""

    node: str
    port: int
    api_base: str
    rollback: bool = True
    load_timeout: float = 2700.0
    release_timeout: float = 90.0
    configured_repo_id: str = ""
    configured_served_name: str = ""
    reasoning_efforts: tuple[str, ...] = ()
    reasoning_effort: str = ""
    repetition_detection: dict[str, Any] | None = None
    profiles: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SlotSettings:
        try:
            port = int(data["port"])
            return cls(
                node=str(data["node"]),
                port=port,
                api_base=str(data.get("api_base") or f"http://127.0.0.1:{port}/v1"),
                rollback=bool(data.get("rollback", True)),
                load_timeout=float(data.get("load_timeout", 2700)),
                release_timeout=float(data.get("release_timeout", 90)),
                configured_repo_id=str(data.get("configured_repo_id") or ""),
                configured_served_name=str(data.get("configured_served_name") or ""),
                reasoning_efforts=tuple(str(level) for level in data.get("reasoning_efforts") or ()),
                reasoning_effort=str(data.get("reasoning_effort") or ""),
                repetition_detection=dict(data["repetition_detection"]) if data.get("repetition_detection") else None,
                profiles={str(name): dict(profile) for name, profile in (data.get("profiles") or {}).items()},
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"the slot settings are incomplete or malformed: {error!r}") from error

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> SlotSettings:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclasses.dataclass(frozen=True)
class Previous:
    """The server a launch replaced, kept so a failed launch can start it again."""

    server_id: str
    engine: str
    repo_id: str
    config: dict[str, Any]

    @property
    def label(self) -> str:
        return str(self.config.get("served_model_name") or self.repo_id)


def gpu_memory() -> dict[int, tuple[int, int]] | None:
    """Return {gpu index: (total MiB, used MiB)} from nvidia-smi, or None when it cannot be read."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    readings = {}
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            readings[int(parts[0])] = (int(float(parts[1])), int(float(parts[2])))
        except ValueError:
            continue
    return readings or None


class GpuBaseline:
    """GPU memory other programs hold: what each card uses while no launcher server runs.

    A sample is only taken while no launcher server runs and no start or
    stop happened in the last quiet_seconds, so memory a stopping server
    still holds, or a starting one already holds, never counts as theirs.
    """

    def __init__(self, *, quiet_seconds: float = 15.0, clock: Callable[[], float] = time.monotonic):
        self.quiet_seconds = quiet_seconds
        self.clock = clock
        self.used_mb: dict[int, int] = {}
        self._quiet_after = 0.0
        self._lock = threading.Lock()

    def hold(self) -> None:
        """A server starts or stops: take no sample for a while."""
        with self._lock:
            self._quiet_after = self.clock() + self.quiet_seconds

    def observe(self, gpus: list[dict[str, Any]], *, launcher_running: bool) -> None:
        with self._lock:
            if launcher_running or self.clock() < self._quiet_after:
                return
            for gpu in gpus:
                try:
                    self.used_mb[int(gpu["index"])] = max(0, int(gpu["vram_total_mb"]) - int(gpu["vram_free_mb"]))
                except (KeyError, TypeError, ValueError):
                    continue

    def other_used(self, index: int) -> int | None:
        with self._lock:
            return self.used_mb.get(index)

    def whole_gpu(self, gpus: list[dict[str, Any]], *, launcher_running: bool) -> list[dict[str, Any]]:
        """The GPUs as a launch finds them, once the launcher's own servers are stopped."""
        if not launcher_running:
            return gpus
        adjusted = []
        for gpu in gpus:
            try:
                total = int(gpu["vram_total_mb"])
                free = int(gpu["vram_free_mb"])
                other = self.other_used(int(gpu["index"]))
            except (KeyError, TypeError, ValueError):
                adjusted.append(gpu)
                continue
            if other is None:
                other = UNSAMPLED_RESERVE_MB
            adjusted.append({**gpu, "vram_free_mb": max(free, total - other)})
        return adjusted

    def released(self, readings: dict[int, tuple[int, int]] | None) -> bool:
        """True when every sampled card is back near what other programs held."""
        if not readings:
            return True
        for index, (_total, used) in readings.items():
            other = self.other_used(index)
            if other is not None and used > other + RELEASE_SLACK_MB:
                return False
        return True


def plain_http_opener() -> urllib.request.OpenerDirector:
    """An opener for http:// only.

    urllib's default opener builds a TLS context up front, which fails on a
    FIPS host whose image cannot run OpenSSL in FIPS mode, even for a plain
    HTTP request. Everything this module calls is plain HTTP on loopback.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.HTTPHandler(), urllib.request.HTTPDefaultErrorHandler()):
        opener.add_handler(handler)
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    return opener


def served_model(port: int, *, timeout: float = 5.0) -> dict[str, Any] | None:
    """vLLM's own description of what it serves on this node: the first card of /v1/models."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models")
    key = os.environ.get(API_KEY_ENV, "")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    try:
        with plain_http_opener().open(request, timeout=timeout) as response:
            cards = json.loads(response.read()).get("data") or []
    except (OSError, ValueError, AttributeError, urllib.error.URLError):
        return None
    return cards[0] if cards and isinstance(cards[0], dict) else None


def bearer_matches(authorization: str | None, token: str) -> bool:
    """Constant-time check of an Authorization header against the status token."""
    if not token or not authorization:
        return False
    return hmac.compare_digest(authorization.strip().encode(), f"Bearer {token}".encode())


class Slot:
    """The hooks around the launcher's API module. See the module docstring."""

    def __init__(
        self,
        settings: SlotSettings,
        *,
        api: Any,
        advisor: Any,
        catalog: Any,
        vllm_backends: Any,
        mtp: Any,
        port_in_use: Callable[[int], bool],
        read_gpus: Callable[[], dict[int, tuple[int, int]] | None] = gpu_memory,
        read_served: Callable[[int], dict[str, Any] | None] = served_model,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] | None = None,
    ):
        self.settings = settings
        self.api = api
        self.advisor = advisor
        self.catalog = catalog
        self.vllm_backends = vllm_backends
        self.mtp = mtp
        self.port_in_use = port_in_use
        self.read_gpus = read_gpus
        self.read_served = read_served
        self.clock = clock
        self.sleep = sleep
        self.log = log or (lambda message: print(f"stack_slot: {message}", flush=True))
        self.baseline = GpuBaseline(clock=clock)
        # Reentrant: a rollback runs inside a launch and goes through
        # launch() again on the same thread.
        self.swap_lock = threading.RLock()
        # Servers stopped on purpose (Stop, Remove, or replaced by a launch):
        # their exit is never a reason to roll back.
        self.intentional: set[str] = set()
        # Runs the rollback watcher; a daemon thread, replaceable in tests.
        self.spawn: Callable[[Callable[[], None]], None] = self._start_thread
        servers = api.servers
        self._launch = servers.launch
        self._stop = servers.stop
        self._remove = servers.remove
        self._get_hardware = api.get_hardware
        self._installed_models = api.installed_models
        self._advise = advisor.advise
        self._presets = advisor.presets

    # ------------------------------------------------------------------ setup

    def install(self) -> None:
        """Replace the launcher's functions with the slot's. Nothing is patched before all exist."""
        servers = self.api.servers
        servers.launch = self.launch
        servers.stop = self.stop
        servers.remove = self.remove
        # api.py looks these up as module globals at call time, so replacing
        # the module attributes reaches every caller (advise, presets,
        # launch, models, favorites).
        self.api.get_hardware = self.get_hardware
        self.api.installed_models = self.installed_models
        self.advisor.advise = self.advise
        self.advisor.presets = self.presets

    # -------------------------------------------------------------- queries

    def running(self) -> list[Any]:
        """The launcher's servers whose process runs, as server objects."""
        found = []
        for status in self.api.servers.list():
            if status.get("running"):
                server = self.api.servers.get(status["id"])
                if server is not None:
                    found.append(server)
        return found

    def get_hardware(self, max_age: float = 5.0) -> dict[str, Any]:
        hardware = self._get_hardware(max_age)
        self.baseline.observe(hardware.get("gpus") or [], launcher_running=bool(self.running()))
        return hardware

    def _whole(self, hardware: dict[str, Any], running: list[Any]) -> dict[str, Any]:
        return {
            **hardware,
            "gpus": self.baseline.whole_gpu(hardware.get("gpus") or [], launcher_running=bool(running)),
            ADJUSTED: True,
        }

    def _forget_hardware(self) -> None:
        """Drop the launcher's 5-second hardware cache, so the next advice reads the GPU anew."""
        lock = getattr(self.api, "_hw_lock", None)
        cache = getattr(self.api, "_hw_cache", None)
        if lock is None or not isinstance(cache, dict):
            return
        with lock:
            cache["data"] = None

    def installed_models(self) -> list[dict[str, Any]]:
        """Installed models with the running one first: the Launch page opens on the first."""
        models = self._installed_models()
        running = {server.model_label for server in self.running()}
        return sorted(models, key=lambda model: model.get("repo_id") not in running)

    # --------------------------------------------------------------- advice

    def advise(self, engine: str, model: dict[str, Any], config: dict[str, Any], hw: dict[str, Any]) -> dict[str, Any]:
        if hw.get(ADJUSTED):
            return self._advise(engine, model, config, hw)
        running = self.running()
        report = self._advise(engine, model, config, self._whole(hw, running))
        overall = report.get("overall")
        if running and isinstance(overall, dict):
            overall.setdefault("details", []).insert(0, self._replace_note(model, running))
        requested = (config or {}).get("port")
        if requested not in (None, "") and str(requested) != str(self.settings.port):
            report.setdefault("flags", {})["port"] = {
                "level": "yellow",
                "message": (
                    f"This node serves one model at a time on port {self.settings.port}, the port LiteLLM "
                    f"routes to, so the launch uses port {self.settings.port}."
                ),
            }
        return report

    def _replace_note(self, model: dict[str, Any], running: list[Any]) -> str:
        current = running[0]
        name = str((current.config or {}).get("served_model_name") or current.model_label)
        port = self.settings.port
        if current.model_label == model.get("repo_id"):
            note = f"Rated for the whole GPU. Launching restarts {name} on port {port} with these settings."
            if self.settings.rollback:
                note += " If they fail to load, it starts again with the settings it runs with now."
        else:
            note = f"Rated for the whole GPU. Launching stops {name} and starts this model on port {port} in its place."
            if self.settings.rollback:
                note += f" If this model fails to load, {name} starts again with its current settings."
        return note

    def presets(self, engine: str, model: dict[str, Any], hw: dict[str, Any]) -> list[dict[str, Any]]:
        if hw.get(ADJUSTED):
            return self._presets(engine, model, hw)
        running = self.running()
        presets = self._presets(engine, model, self._whole(hw, running))
        for server in running:
            server_engine = "vllm" if server.engine.startswith("vllm") else server.engine
            if server.model_label != model.get("repo_id") or server_engine != engine or not server.config:
                continue
            config = {key: value for key, value in server.config.items() if key not in ("port", "host")}
            name = str(config.get("served_model_name") or server.model_label)
            presets = [
                {
                    "name": PRESET_NAME,
                    "config": config,
                    "description": (
                        f"The settings {name} runs with now. Change any of them and launch to restart it "
                        "with your changes."
                    ),
                },
                *presets,
            ]
            break
        return presets

    # --------------------------------------------------------------- launch

    def default_served_name(self, repo_id: str) -> str:
        """The name LiteLLM lists a model under when the launch form leaves it empty."""
        if self.settings.configured_served_name and repo_id == self.settings.configured_repo_id:
            return self.settings.configured_served_name
        return repo_id.rstrip("/").split("/")[-1]

    def launch(
        self,
        engine_mode: str,
        model: dict[str, Any],
        config: dict[str, Any],
        llamacpp_binary: str | None = None,
        vllm_binary: str | None = None,
        hardware: dict[str, Any] | None = None,
    ) -> Any:
        if not self.swap_lock.acquire(blocking=False):
            raise RuntimeError(BUSY)
        try:
            return self._swap(engine_mode, model, config, llamacpp_binary, vllm_binary, hardware)
        finally:
            self.swap_lock.release()

    def _precheck(
        self,
        engine_mode: str,
        model: dict[str, Any],
        config: dict[str, Any],
        llamacpp_binary: str | None,
        vllm_binary: str | None,
        hardware: dict[str, Any] | None,
    ) -> None:
        """The launcher's own checks before it starts a process (registry.ServerManager.launch)."""
        self.vllm_backends.validate(engine_mode, model, config, hardware, vllm_binary or "vllm")
        if config.get("use_mtp") and engine_mode in ("llamacpp", "vllm-native", "vllm-docker"):
            decision = self.mtp.resolve(
                engine_mode,
                model,
                config,
                vllm_binary=vllm_binary or "vllm",
                llama_binary=llamacpp_binary or "llama-server",
            )
            if decision["level"] == "red":
                raise ValueError(decision["message"])
        if engine_mode.startswith("vllm"):
            # Builds the command line without starting it: bad extra
            # arguments or placement fail here, before anything is stopped.
            self.api.servers.build_spec(engine_mode, model, dict(config), llamacpp_binary, vllm_binary)

    def _rollback_target(self, running: list[Any]) -> Previous | None:
        """The server to start again if the launch fails: the one on the slot port, if it was serving."""
        on_port = [server for server in running if server.port == self.settings.port]
        candidates = on_port or sorted(running, key=lambda server: server.started_at or "", reverse=True)
        for server in candidates:
            if server.config is None or server.config_save_error:
                continue
            if server.load_status == "successful" or server.health():
                return Previous(server.server_id, server.engine, server.model_label, dict(server.config))
        return None

    def _wait_released(self) -> None:
        deadline = self.clock() + self.settings.release_timeout
        while self.clock() < deadline:
            if not self.port_in_use(self.settings.port) and self.baseline.released(self.read_gpus()):
                return
            self.sleep(1.0)
        self.log(
            f"the GPU memory or port {self.settings.port} was not free after "
            f"{self.settings.release_timeout:.0f} s; starting anyway"
        )

    def _swap(
        self,
        engine_mode: str,
        model: dict[str, Any],
        config: dict[str, Any],
        llamacpp_binary: str | None,
        vllm_binary: str | None,
        hardware: dict[str, Any] | None,
    ) -> Any:
        port = self.settings.port
        config = dict(config or {})
        config["port"] = port
        if engine_mode.startswith("vllm") and not str(config.get("served_model_name") or "").strip():
            config["served_model_name"] = self.default_served_name(model["repo_id"])
        self._precheck(engine_mode, model, config, llamacpp_binary, vllm_binary, hardware)

        running = self.running()
        if self.port_in_use(port) and not any(server.port == port for server in running):
            raise RuntimeError(
                f"Port {port} is in use by a program this launcher did not start, so no model can be started "
                "there. Check that vllm-serve is stopped (systemctl status vllm-serve)."
            )
        previous = self._rollback_target(running)
        for server in running:
            server.note(f"stack: stopping to make room for {model['repo_id']} on port {port}")
            if not self.stop(server.server_id):
                raise RuntimeError(
                    f"{server.model_label} did not stop, so {model['repo_id']} was not started. "
                    "Check its log on the Servers page."
                )
        if running:
            self._wait_released()

        self.baseline.hold()
        try:
            server = self._launch(
                engine_mode, model, config, llamacpp_binary=llamacpp_binary, vllm_binary=vllm_binary, hardware=hardware
            )
        except (RuntimeError, ValueError) as error:
            if previous is not None and self.settings.rollback and self._restore(previous):
                kind = ValueError if isinstance(error, ValueError) else RuntimeError
                raise kind(f"{error} {previous.label} was started again with its previous settings.") from error
            raise
        finally:
            self._forget_hardware()

        if previous is not None:
            server.note(f"stack: replaced {previous.label} (server {previous.server_id}) on port {port}")
            if self.settings.rollback:
                self.spawn(lambda: self._watch(server, previous))
        return server

    @staticmethod
    def _start_thread(target: Callable[[], None]) -> None:
        threading.Thread(target=target, name="stack-rollback", daemon=True).start()

    def stop(self, server_id: str) -> bool:
        self.intentional.add(server_id)
        self.baseline.hold()
        try:
            return self._stop(server_id)
        finally:
            self.baseline.hold()
            self._forget_hardware()

    def remove(self, server_id: str) -> bool:
        self.intentional.add(server_id)
        self.baseline.hold()
        try:
            return self._remove(server_id)
        finally:
            self.baseline.hold()
            self._forget_hardware()

    # ------------------------------------------------------------- rollback

    def _restore(self, previous: Previous) -> bool:
        """Start the replaced settings again, through the launcher's own launch path."""
        config, error = self.catalog.safe_launch_config(previous.engine, previous.config)
        if error:
            self.log(f"cannot start {previous.label} again: {error}")
            return False
        try:
            self.api.api_launch(
                self.api.LaunchRequest(engine_mode=previous.engine, repo_id=previous.repo_id, config=config)
            )
        except Exception as error:  # noqa: BLE001 - HTTPException and the launcher's own errors alike
            self.log(f"cannot start {previous.label} again: {getattr(error, 'detail', None) or error}")
            return False
        self.log(f"started {previous.label} again with its previous settings")
        return True

    def _watch(self, server: Any, previous: Previous) -> None:
        """Roll back when the new server exits before it ever answers /health."""
        deadline = self.clock() + self.settings.load_timeout
        while self.clock() < deadline:
            self.sleep(5.0)
            if server.server_id in self.intentional or self.api.servers.get(server.server_id) is not server:
                return
            if server.is_running():
                if server.health():
                    return
                continue
            with self.swap_lock:
                if server.server_id in self.intentional or self.running():
                    return
                server.note(
                    f"stack: exited before it answered on port {self.settings.port}; "
                    f"starting {previous.label} again with its previous settings"
                )
                self._restore(previous)
            return
        server.note(
            f"stack: no answer after {self.settings.load_timeout / 60:.0f} minutes; "
            f"{previous.label} was not started again automatically"
        )

    # ---------------------------------------------------------- slot status

    def litellm_entry(self, name: str, max_len: Any, config: dict[str, Any], repo_id: str) -> dict[str, Any]:
        """How LiteLLM should list the served model; launcher_follower.py applies it."""
        profile = self.settings.profiles.get(name) or {}
        reasoning = bool(config.get("reasoning_parser"))
        efforts = list(profile.get("reasoning_efforts", self.settings.reasoning_efforts)) if reasoning else []
        effort = str(profile.get("reasoning_effort", self.settings.reasoning_effort) or "") if reasoning else ""
        try:
            tokens = int(max_len) if max_len not in (None, "") else None
        except (TypeError, ValueError):
            tokens = None
        return {
            "model_name": name,
            "api_base": self.settings.api_base,
            "max_input_tokens": tokens,
            "max_tokens": tokens,
            "supports_reasoning": reasoning,
            "reasoning_efforts": [level for level in efforts if level in EFFORT_LEVELS],
            "reasoning_effort": effort or None,
            "repetition_detection": self.settings.repetition_detection,
            "adaptive_router_preferences": profile.get("adaptive_router_preferences") or None,
            "description": f"{name} - vLLM on {self.settings.node} ({repo_id}, started from the LLM Launcher)",
        }

    def slot_document(self) -> dict[str, Any]:
        port = self.settings.port
        document: dict[str, Any] = {"version": 1, "node": self.settings.node, "port": port}
        server = next((server for server in self.running() if server.port == port), None)
        if server is None:
            return {**document, "state": "empty"}
        document.update(
            server_id=server.server_id, repo_id=server.model_label, engine=server.engine, started_at=server.started_at
        )
        if not server.health():
            return {**document, "state": "loading"}
        config = server.config or {}
        card = self.read_served(port) or {}
        name = str(card.get("id") or config.get("served_model_name") or server.model_label)
        max_len = card.get("max_model_len") or config.get("max_model_len")
        return {
            **document,
            "state": "serving",
            "litellm": self.litellm_entry(name, max_len, config, server.model_label),
        }

    def status(self, authorization: str | None) -> tuple[int, dict[str, Any]]:
        """(HTTP status, body) for GET /api/stack/slot."""
        token = os.environ.get(TOKEN_ENV, "")
        if not token:
            return 404, {"detail": "Not Found"}
        if not bearer_matches(authorization, token):
            return 401, {"detail": "A valid bearer token is required."}
        return 200, self.slot_document()


REQUIRED = (
    ("api", "servers"),
    ("api", "get_hardware"),
    ("api", "installed_models"),
    ("api", "api_launch"),
    ("api", "LaunchRequest"),
    ("api", "router"),
    ("advisor", "advise"),
    ("advisor", "presets"),
    ("catalog", "safe_launch_config"),
    ("vllm_backends", "validate"),
    ("mtp", "resolve"),
)
SERVER_METHODS = ("launch", "stop", "remove", "list", "get", "build_spec")


def add_status_route(router: Any, slot: Slot) -> None:
    """Serve GET <router prefix>/stack/slot from slot.status().

    The Request annotation is set as an object after the def, not written in
    the signature: this module's postponed annotations would hand FastAPI
    the string "Request", which it cannot resolve here and would read as a
    required query parameter (every request then answers 422).
    """
    from fastapi import HTTPException, Request

    def stack_slot_status(request):
        code, body = slot.status(request.headers.get("authorization"))
        if code != 200:
            raise HTTPException(code, body["detail"])
        return body

    stack_slot_status.__annotations__ = {"request": Request}
    router.add_api_route("/stack/slot", stack_slot_status, methods=["GET"], include_in_schema=False)


def install(slot_file: str | os.PathLike[str] | None = None) -> Slot:
    """Load the slot settings, check the launcher internals, hook them, and add the status route.

    Must run before the launcher builds its FastAPI app: create_app() copies
    the router's routes at that point.
    """
    from local_llm_launcher import advisor, api, catalog
    from local_llm_launcher.engines import mtp, vllm_backends
    from local_llm_launcher.registry import port_in_use

    settings = SlotSettings.load(slot_file or os.environ.get(SLOT_FILE_ENV) or DEFAULT_SLOT_FILE)
    modules = {"api": api, "advisor": advisor, "catalog": catalog, "vllm_backends": vllm_backends, "mtp": mtp}
    missing = [f"{module}.{name}" for module, name in REQUIRED if not hasattr(modules[module], name)]
    missing += [f"api.servers.{name}" for name in SERVER_METHODS if not callable(getattr(api.servers, name, None))]
    if missing:
        raise RuntimeError(
            f"the launcher has no {', '.join(missing)}: this launcher version moved what the single-model "
            "slot hooks. Update vllm-launcher-slot.py, or set vllm_launcher_swap: false."
        )
    if any(getattr(route, "path", "") == "/api/stack/slot" for route in api.router.routes):
        raise RuntimeError("the launcher already has a route /api/stack/slot")

    slot = Slot(
        settings,
        api=api,
        advisor=advisor,
        catalog=catalog,
        vllm_backends=vllm_backends,
        mtp=mtp,
        port_in_use=port_in_use,
    )
    slot.install()
    add_status_route(api.router, slot)
    return slot
