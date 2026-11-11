# vLLM Inference

How all vLLM inferencing is configured and deployed in this stack: the architecture, the parallelism modes, the variables that control them, and a complete setup walkthrough.

Everything is deployed by the **`vllm_compute` Ansible role** (`ansible/roles/vllm_compute/`) via `ansible-playbook playbooks/inference.yml`. It runs on the `vllm_compute` inventory group after the global `prerequisites` check role (Docker, `docker compose`, and — for this group — the NVIDIA driver via `prerequisites_check_nvidia: true`).

There are three independent serving layers in the role:

1. **Primary model** — one chat/completion model, optionally sharded across the cluster (Ray pipeline or tensor parallelism, or standalone replicas).
2. **Flux image-generation servers** — a list of independent single-model `vllm serve` instances, one per configured model, running on every node.
3. **Aux model servers** — a list of independent single-model `vllm serve` instances, each placed on a chosen subset of nodes via its `hosts` key (`vllm_aux_models`).

All render into `litellm/vllm-models.yaml` (see [Register with LiteLLM](#register-with-litellm)).

---

## Supported operating systems

**Fedora** is the supported and tested target (current releases; the integration rig runs Fedora 44 arm64 with GPU pieces stubbed, production nodes are Fedora x86_64). The role's requirements are RHEL-family behavior, not Fedora specifically — any distro providing the pieces below works:

| Requirement | What uses it |
|---|---|
| `dnf` package management | `prerequisites` role base-package task; install guidance in fail messages |
| **`firewalld`** + `python3-firewall`/`python3-dbus` | `vllm_compute` firewall tasks (`tasks/firewall.yml`) drive the `ansible.posix.firewalld` module; the python packages are its runtime deps (installed by `prerequisites` on RedHat-family hosts) |
| `systemd` | the `vllm-ray`, `vllm-serve`, `vllm-flux-*` units |
| NVIDIA driver | installed when missing, and its kernel module rebuilt after a kernel update, by the `nvidia_driver` role (see [NVIDIA driver](#nvidia-driver-the-nvidia_driver-role)) |
| Docker Engine + compose plugin **or podman**, NVIDIA container toolkit | container runtime; checked by `prerequisites` (installed per-node by hand, or by the role with `prerequisites_install_missing: true`) |

| OS | Status | Notes |
|---|---|---|
| Fedora (current) | **Supported, tested** | Reference platform; rig + production |
| RHEL / Alma / Rocky / other RHEL-family | Expected to work, untested | Needs `firewalld` installed/enabled |
| VMware Photon OS | Partial | The `prerequisites` package task covers it (`tdnf`), **but the vllm firewall tasks are not Photon-compatible** — Photon does not ship `firewalld`. Set `vllm_manage_firewall: false` and restrict the Ray/API ports with Photon's own firewall (iptables rules), or expect the `firewall-cmd` tasks to fail. This combination is untested. |
| Debian-family (Ubuntu, Debian, etc.) | Not supported | `prerequisites` installs base packages via `apt`, but nothing in `vllm_compute` handles Debian: no `firewalld`, and the fail-message install guidance is dnf/RHEL-specific. Would need a firewall-task variant and doc changes first. |

The [Setup](#setup) steps and every install command in the role's fail messages assume Fedora/RHEL-family.

---

## Architecture

```
                       ┌──────────────────────────┐
 Open WebUI ──────────▶ │ LiteLLM (docker compose) │  model_list entries:
                       └───────────┬──────────────┘  vllm-models.yaml merged
                                   │  http://node:8000/v1  (primary)
                                   │  http://node:8005/v1  (flux)
                                   ▼
        compute segment (10/25GbE, firewalld zone "vllm-cluster")
   ┌──────────────────────────────────────────────────────────────┐
   │ vllm-node-01 (head)      vllm-node-02        vllm-node-03   │
   │ ┌────────────────┐       ┌─────────────┐    ┌─────────────┐│
   │ │ vllm-serve.svc │──┐    │ vllm-ray.svc│    │ vllm-ray.svc││
   │ │ (port 8000)    │  │    └─────────────┘    └─────────────┘│
   │ └────────────────┘  │  Ray cluster (GCS :6379)             │
   │ ┌────────────────┐  │                                        │
   │ │ vllm-ray.svc   │◀┘  pipeline/tensor shard across nodes    │
   │ └────────────────┘                                          │
    │ ┌──────────────────────┐ ┌──────────────────────┐           │
    │ │ vllm-flux-flux.svc   │ │ vllm-flux-flux-…     │           │
    │ │ (port 8005)          │ │ (port 8006)          │           │
    │ └──────────────────────┘ └──────────────────────┘           │
    │ ┌──────────────────────┐ ┌──────────────────────┐           │
    │ │ vllm-aux-<name>.svc  │ │ …per hosts placement │           │
    │ │ (port 8010+)         │ │ (subset of nodes)    │           │
    │ └──────────────────────┘ └──────────────────────┘           │
    │ shared HF cache: /mnt/data1/huggingface                     │
    └──────────────────────────────────────────────────────────────┘
```

Key points:

- **The compute nodes are separate hosts from the Open WebUI stack.** Containers run under **systemd** (`vllm-ray`, `vllm-serve`, `vllm-flux-<name>`), not docker compose. Each unit runs a generated launch script from `vllm_install_dir` (default `/opt/vllm`) that does `{{ vllm_container_cli }} run` with the rendered flags — Docker or podman per node group; podman nodes drop the `docker.service` requirement (podman is daemonless). The role pulls and runs the fully-qualified image reference (`vllm_image_fq`): podman with the Fedora default `short-name-mode=enforcing` cannot resolve an unqualified reference non-interactively, so an unqualified `vllm_image` is prefixed with `docker.io/`. On podman nodes booted with `fips=1`, set `vllm_mask_fips_flag: true`: it bind-mounts `/dev/null` over `/proc/sys/crypto/fips_enabled`, which otherwise makes Ubuntu-based images auto-activate FIPS mode and fail every `SSLContext` creation (no `fips.so` in the image).
- **All servers share one Hugging Face cache** (`vllm_hf_cache_dir`, default `/mnt/data1/huggingface`), so each node downloads a model once even when the primary model and several flux servers need weights.
- **The unit files handle container lifecycle**: `--rm` does not fire when systemd stops the `docker run` client, so every unit has `ExecStop`/`ExecStopPost` that removes the container explicitly, plus `KillMode=control-group`. This makes `systemctl restart` clean (no leftover container holding the name).
- **Requests come through LiteLLM**, which authenticates with `VLLM_API_KEY`. Ray traffic itself is unauthenticated and can execute arbitrary code — the `vllm-cluster` firewalld zone restricts it to the compute CIDR (`vllm_firewall_zone_source`).

### Files in the role

| File | Purpose |
|---|---|
| `defaults/main.yml` | Every variable with a default + full explanations. The reference for what each knob does. |
| `tasks/main.yml` | Task flow: validate config → detect GPUs → resolve topology → firewall → services → flux → aux → LiteLLM render (in that order). |
| `tasks/container_runtime.yml` | How every container is started: the init process (tini) and, on podman nodes, the unprivileged service account the containers run as (see [Rootless containers and tini](#rootless-containers-and-tini)). Runs before every task file that deploys a unit. |
| `tasks/services.yml` | Primary-model scripts, units, startup, health waits. |
| `tasks/flux.yml` | Flux list merge/validation, per-model scripts and units, cleanup of removed models, health waits. |
| `tasks/aux.yml` | Aux list merge/validation with per-node placement, per-model scripts and units, cleanup of removed or re-hosted models, health waits. |
| `tasks/firewall.yml` | Creates the `vllm-cluster` firewalld zone via the `ansible.posix.firewalld` module, restricts to `vllm_firewall_zone_source`, admits the `ssh` service (source binding routes ALL subnet traffic into the zone — without it management SSH is cut off), opens the pinned Ray + API + flux + aux ports. |
| `tasks/launcher_placement.yml` | Decides which nodes run an LLM Launcher and whether it owns the primary model slot (runs before `services.yml`). |
| `tasks/launcher.yml` | LLM Launcher GUI and its oauth2-proxy gate: image build, secrets, scripts and units, firewall, health waits, teardown (see [LLM Launcher](#llm-launcher-web-control-panel-per-inference-server)). |
| `tasks/litellm_registration.yml` | Renders `litellm/vllm-models.yaml` in the repo (delegated to localhost). |
| `templates/vllm-serve.sh.j2` | `vllm serve` launch script with the rendered parallelism flags. |
| `templates/ray-start.sh.j2` | Ray daemon launch script (head/worker). |
| `templates/vllm-*.service.j2` | systemd units with the container-lifecycle fix above. |
| `templates/litellm-models.yaml.j2` | The LiteLLM snippet template. |
| `tests/vllm-rig/` | Disposable-VM integration rig (see [Testing](#testing-the-role-locally)). |

---

## Parallelism modes (primary model)

Choose with `vllm_deployment_mode` (+ `vllm_cross_node_tensor_parallel`) in `ansible/group_vars/vllm_compute/vars.yml`:

| Mode | What runs where | Commands rendered | Best for |
|---|---|---|---|
| `ray` (default) | Ray head + workers; `vllm serve` **only on the head** | `--pipeline-parallel-size=<nodes> --distributed-executor-backend=ray` | Models too big for one node; 10GbE fabric |
| `ray` + `vllm_cross_node_tensor_parallel: true` | Same Ray cluster; TP instead of PP | `--tensor-parallel-size=<TP/node × nodes>` | Latency-sensitive; **25GbE+ required** |
| `standalone` | Full model replica on **every node** | no Ray flags; plain `vllm serve` | Models that fit one node; max throughput via LiteLLM load balancing |

### How the sizes are computed

`vllm_tensor_parallel_size` is GPUs **within** a node (what NCCL all-reduces over NVLink/PCIe). `vllm_pipeline_parallel_size` is the number of pipeline **stages** — default one per node, each node holding a contiguous layer range, so inter-node traffic is only the activations at stage boundaries (fits 10GbE).

The role computes what `vllm serve` actually receives (`tasks/main.yml`, "Compute effective parallel sizes"):

```yaml
vllm_effective_tp: tensor_parallel_size × pipeline_parallel_size   # ray + cross-node TP only
vllm_effective_pp: pipeline_parallel_size                          # ray, otherwise 1
```

Cross-node TP multiplies the per-node TP across the cluster and collapses PP to 1 — every layer's all-reduce then crosses the fabric each step, hence the 25GbE+ requirement.

The role **asserts** `vllm_tensor_parallel_size <= GPUs detected on each node` (autodetected via `nvidia-smi --list-gpus` when `vllm_gpus_per_node: 0`), and in ray mode that exactly one inventory host carries `vllm_ray_head: true`.

---

## Flux image generation (multi-model)

Flux servers are configured by the `vllm_flux_models` list — this is how you switch models or run several simultaneously. Each entry deploys **one standalone single-model server on every node**, each on its own host port with its own systemd unit (`vllm-flux-<name>`). No Ray, no sharding; `tensor_parallel_size` spans that node's GPUs only.

`ansible/group_vars/vllm_compute/vars.yml`:

```yaml
vllm_flux_enabled: true

vllm_flux_models:
  - name: flux                      # -> unit vllm-flux-flux, port 8005
    model: black-forest-labs/FLUX.1-dev
  - name: flux-schnell              # -> unit vllm-flux-flux-schnell, port 8006
    model: black-forest-labs/FLUX.1-schnell
    gpu_memory_utilization: 0.90    # per-entry overrides
    extra_args: ["--enforce-eager"]
```

Per-entry keys (all optional except `model`; unset keys take the `vllm_flux_default_*` role defaults):

| Key | Default | Meaning |
|---|---|---|
| `name` | model basename | Unit name suffix; also the LiteLLM model name when `served_model_name` is unset |
| `model` | `vllm_flux_default_model` | HF repo id or local path |
| `port` | `vllm_flux_base_port + index` (8005, 8006, …) | Host port this server binds |
| `tensor_parallel_size` | 1 | GPUs this server uses on each node |
| `max_model_len` | 2048 | Context length (Flux prompts are short) |
| `gpu_memory_utilization` | 0.95 | Fraction of GPU memory the server may claim |
| `extra_args` | `[]` | Raw vLLM CLI flags appended to `vllm serve` |

**Switching models** is editing this list and re-running the playbook: adding an entry deploys it, changing `model`/flags restarts that unit, and removing an entry stops + disables its unit and deletes the unit file and launch script. Setting `vllm_flux_enabled: false` tears every flux server down.

**GPU budget:** each entry consumes `tensor_parallel_size` GPUs on **every** node while running. The role validates each entry's TP against the detected GPUs but not the *total* across servers — plan simultaneously-running servers to fit the node (e.g. four TP=1 servers on a 4-GPU node, or one TP=2 flux server plus the primary model at TP=2).

---

## Aux model servers (per-node placement)

Aux servers are the hardware-aware orchestration layer: a list of additional single-model `vllm serve` servers, **each placed on a chosen subset of nodes** via its `hosts` key. This is how you send different models to different compute nodes — a 24GB-class vision-language model to the node with the big GPU, a 0.6B gatekeeper/decider next to other work on a smaller card, and so on. Like flux entries each server gets its own systemd unit (`vllm-aux-<name>`), launch script, and host port; unlike flux entries it runs only on the nodes the `hosts` key selects.

`ansible/group_vars/vllm_compute/vars.yml`:

```yaml
vllm_aux_enabled: true

vllm_aux_models:
  - name: vision-analyst          # -> unit vllm-aux-vision-analyst, port 8010
    model: Qwen/Qwen3.5-VL-8B    # HF repo id or local path
    hosts: [vllm-node-01]        # this node only
  - name: gatekeeper              # -> unit vllm-aux-gatekeeper, port 8011
    model: Qwen/Qwen3-0.6B
    hosts: all                    # every vllm_compute node
    max_model_len: 4096          # per-entry overrides
    extra_args: ["--enable-auto-tool-choice"]
```

Per-entry keys (all optional except `model`; unset keys take the `vllm_aux_default_*` role defaults):

| Key | Default | Meaning |
|---|---|---|
| `name` | model basename | Unit name suffix; also the LiteLLM model name when `served_model_name` is unset |
| `model` | `vllm_aux_default_model` | HF repo id or local path |
| `hosts` | `all` | Inventory hostnames this entry deploys on: a list (`[vllm-node-01]`), a comma string (`vllm-node-01,vllm-node-02`), or `all` |
| `port` | `vllm_aux_base_port + index` (8010, 8011, …) | Host port this server binds |
| `tensor_parallel_size` | 1 | GPUs this server uses on each node |
| `max_model_len` | 8192 | Context length |
| `gpu_memory_utilization` | 0.90 | Fraction of GPU memory the server may claim |
| `extra_args` | `[]` | Raw vLLM CLI flags appended to `vllm serve` |
| `api_key` | `vllm_aux_api_key` (defaults to `vllm_api_key`) | API key this endpoint requires |
| `reasoning_parser` | `""` | vLLM reasoning parser (`qwen3`, `gemma4`, `deepseek_r1`, …): renders `--reasoning-parser`, and the LiteLLM `model_info` gets `supports_reasoning: true` |
| `chat_template_kwargs` | `{}` | Rendered as `--default-chat-template-kwargs '<json>'`: server-side thinking defaults (e.g. `{enable_thinking: true}` for Gemma 4, `{enable_thinking: false}` to turn Qwen3 thinking off) |
| `reasoning_effort` | `""` | Default `reasoning_effort` in the LiteLLM `litellm_params` (`none`…`max`): used when a request sends no effort; Open WebUI / MAGIE per-request effort overrides it |
| `reasoning_efforts` | `[]` | Levels the opencode / MAGIE thinking picker offers: each renders `supports_<level>_reasoning_effort: true` in the LiteLLM `model_info` (same as `vllm_reasoning_efforts` on the primary) |
| `repetition_detection` | `{max_pattern_size: 20, min_pattern_size: 3, min_count: 4}` | Default vLLM N-gram loop detection in LiteLLM `litellm_params`; detects 3–20-token phrases repeated four times and terminates generation |

Behavior:

- **Placement** is evaluated per node: an entry deploys only on hosts its `hosts` key names. Removing a host from the list (or the whole entry) on the next run stops + disables that node's unit and deletes the unit file and launch script. `vllm_aux_enabled: false` tears every aux server down everywhere.
- **Ports must be unique** across the aux, flux, and primary API ports; the role asserts this.
- **GPU budget**: same rule as flux — the role validates each entry's TP against the node's GPUs but not the total; plan co-running servers (aux + flux + primary) to fit the node.
- **LiteLLM**: each (node, entry) placement renders one `model_list` entry in `litellm/vllm-models.yaml`; a model placed on several nodes appears several times under the same `model_name`, and LiteLLM load balances those replicas.

---

## Reasoning models (thinking traces)

For models that emit reasoning traces (DeepSeek R1, Qwen3, Gemma 4 with thinking on, …), vLLM needs a parser to split the trace from the answer, and LiteLLM needs to know the model is reasoning-capable. Both are one knob — no SQL edits, no manual LiteLLM config.

**How to enable reasoning, step by step:**

1. **Find your parser name** in the [vLLM reasoning docs](https://docs.vllm.ai/en/stable/features/reasoning_outputs.html) (table below for common ones). If the model is not listed, it does not emit structured traces and there is nothing to enable.

2. **Check whether thinking is on by default** for your model (vLLM's docs note this per series) — if it is off, you also need the matching chat-template kwargs:

   | Parser | Model series | Thinks by default? | `chat_template_kwargs` to set |
   |---|---|---|---|
   | `deepseek_r1` | DeepSeek R1, R1-Distill, QwQ | yes | — |
   | `qwen3` | Qwen3 | yes | `{enable_thinking: false}` to **disable** |
   | `gemma4` | Gemma 4 | **no** | `{enable_thinking: true}` to **enable** (the template ignores `thinking`) |
   | `deepseek_v3` | DeepSeek-V3.1 | **no** | see vLLM docs (`thinking: true`) |
   | `glm45` | GLM-4.5 | yes | — |
   | `granite` | IBM Granite 3.2 | **no** | `{thinking: true}` to **enable** |
   | `ernie45` | ERNIE-4.5-VL / 4.5-21B | varies | see vLLM docs |

3. **Set the knobs** in `ansible/group_vars/vllm_compute/vars.yml` (primary) or in the entry (aux) — then re-run the playbook:

   ```yaml
   # primary (Gemma 4: parser + thinking is off by default)
   vllm_reasoning_parser: gemma4
   vllm_chat_template_kwargs:
     enable_thinking: true
   # levels the opencode / MAGIE thinking picker offers
   vllm_reasoning_efforts: [none, low, medium, high]

   # aux (Qwen3 thinks by default; no kwargs needed)
   vllm_aux_models:
     - name: gatekeeper
       model: Qwen/Qwen3-0.6B
       reasoning_parser: qwen3
   ```

   - **Primary**: `vllm_reasoning_parser` renders `--reasoning-parser <name>` on `vllm serve`; `vllm_chat_template_kwargs` renders `--default-chat-template-kwargs '<json>'`. Aux entries carry the same two as per-entry keys (`reasoning_parser`, `chat_template_kwargs`; defaults `vllm_aux_default_reasoning_parser` / `vllm_aux_default_chat_template_kwargs`). Flux servers have no reasoning path, so no knob exists there.

4. **Re-run `ansible-playbook playbooks/inference.yml`.** The unit restarts with the new flags, and — with `LITELLM_MASTER_KEY` exported — the LiteLLM registration automatically picks up `supports_reasoning: true` (the stale-prune check compares it, so flipping a parser on/off re-registers the model like a `max_model_len` change; no restart of the gateway, no manual config edit). Without the key, merge the regenerated `litellm/vllm-models.yaml` snippet into `litellm/config.yaml` and `docker compose restart litellm` instead.

5. **Verify:**

   ```bash
   # the rendered launch script carries the flags
   grep -E "reasoning-parser|default-chat-template-kwargs" /opt/vllm/vllm-serve.sh
   # a chat request returns the trace in the reasoning field, not inline
   curl -s http://<node>:8000/v1/chat/completions -H "Authorization: Bearer $VLLM_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"model":"<served_model_name>","messages":[{"role":"user","content":"9.11 vs 9.8, which is greater?"}]}' \
     | python3 -c "import json,sys; m=json.load(sys.stdin)['choices'][0]['message']; print('reasoning:', bool(m.get('reasoning')))"
   # through LiteLLM, the model advertises reasoning support
   curl -sk https://litellm.localhost/model_group/info -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     | python3 -c "import json,sys; print([(m['model_group'], m.get('supports_reasoning')) for m in json.load(sys.stdin)['data']])"
   ```

**Turning reasoning off** is the same steps in reverse: clear `vllm_reasoning_parser` (and `vllm_chat_template_kwargs`) — or the aux entry's `reasoning_parser` — and re-run; the prune + re-register cycle clears `supports_reasoning` automatically.

### Controlling the reasoning effort (thinking level) from the clients

The parser makes vLLM accept `reasoning_effort` on every request; effort level is a **request-time** parameter, not a server flag. Three layers control it, in precedence order (request wins):

1. **Per request** — the client sends `reasoning_effort: none|minimal|low|medium|high|xhigh|max` on `/v1/chat/completions`. vLLM maps it onto the chat template itself: any of `low`/`medium`/`high` (and stronger) injects `enable_thinking: true`, `none` injects `enable_thinking: false` (docs: [vLLM reasoning outputs](https://docs.vllm.ai/en/stable/features/reasoning_outputs.html)). So a Gemma 4 served with `--reasoning-parser gemma4` **does not need** `vllm_chat_template_kwargs` when the request always carries an effort.
2. **Per model, as a LiteLLM default** — `vllm_reasoning_effort` (primary) or an aux entry's `reasoning_effort` key (default `vllm_aux_default_reasoning_effort`). The role puts it in the LiteLLM deployment's `litellm_params` (both the `/model/new` POST and the rendered snippet), and LiteLLM merges it into requests that send no effort of their own; a request-level value overrides it ([LiteLLM reasoning docs](https://docs.litellm.ai/docs/reasoning_content)).
3. **Server default** — `vllm_chat_template_kwargs` (e.g. `{enable_thinking: true}` for Gemma 4), which the request's `chat_template_kwargs` and the effort injection both override.

```yaml
# primary: default effort for requests that don't ask for one
vllm_reasoning_effort: medium
```

How to set it per request from each client:

- **Open WebUI**: the **Thinking** switch in the chat input's `+` menu, also shown as a chip beside `+` while it is on. On sends the level picked in the switch's settings (click the chip, or the sliders icon on its row in the `+` menu: low / medium / high, per user); off (the chip's x, or the switch in the menu) sends `reasoning_effort: none`. The playbook puts the switch only on models LiteLLM reports as reasoning-capable (`tasks/openwebui_reasoning.yml`, details in the next section). **Controls → Advanced Params → Reasoning Effort** (per chat) still works and overrides the switch; Workspace → Models → Edit Model → Advanced Parameters → Reasoning Effort (per model) overrides both. Values map straight through to `reasoning_effort` on the request ([docs](https://docs.openwebui.com/features/chat-conversations/chat-features/reasoning-models)). The thinking trace renders in the collapsible "Thinking" block above the answer once the parser splits it into the `reasoning` field.
- **opencode**: the built-in variant picker. `/variants` (or `ctrl+x v`, bound by the opencode role) opens "Select variant" with the model's levels; `ctrl+t` cycles them. The levels are the variants the LiteLLM discovery plugin builds from the `supports_<level>_reasoning_effort` flags, so a model with an empty `vllm_reasoning_efforts` shows no picker at all. No extra plugin is involved.
- **MAGIE Coder**: models registered through the LiteLLM provider carry the `reasoning` capability (the plugin maps `supports_reasoning` from `/v1/model/info`), and the thinking selector sends `reasoning_effort` on the request. **Auto** follows the LiteLLM default (`vllm_reasoning_effort`, or none if unset); a named level requests that effort explicitly.
- **curl / API**: `"reasoning_effort": "high"` in the chat-completions body, or `?reasoning-effort=` where a client supports it.

Verify the effort reaches the model (LiteLLM logs the merged request; with `LITELLM_LOG=INFO` the proxy prints what it sent):

```bash
curl -sk https://litellm.localhost/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"<served_model_name>","reasoning_effort":"high","messages":[{"role":"user","content":"9.11 vs 9.8?"}]}' \
  | python3 -c "import json,sys; m=json.load(sys.stdin)['choices'][0]['message']; print('reasoning len:', len(m.get('reasoning') or m.get('reasoning_content') or ''))"
```

If the model's chat template does not recognize the effort value, vLLM's `--reasoning-effort-map` (via `vllm_extra_args`) can rewrite it to one it knows.

### The Thinking switch in Open WebUI

Open WebUI 0.13 has no dropdown slot in the chat input, only switches (toggle filters) with a per-user settings panel. The reasoning selector is therefore two filter functions that the `openwebui_stack` role installs through the admin API (no container recreate):

| Function | Role |
|---|---|
| `reasoning_switch` ("Thinking") | The switch. On: `reasoning_effort` = the user's level from the switch's settings (`low` / `medium` / `high`, default `medium`). |
| `reasoning_off` ("Thinking Off State") | Always on for the same models. Off: `reasoning_effort: none`. Open WebUI does not run a toggle filter while its switch is off, so the off state needs its own filter; without it an off switch would fall through to the LiteLLM default effort and the model would keep thinking. |

Where it shows (Open WebUI 0.13.3): the switch is a row in the chat input's `+` menu, with a sliders icon that opens the level setting. While it is on it is also a **Thinking** chip beside `+`: click the chip for the level, its x turns thinking off for the chat. The chip needs the per-user interface setting **Show Input Selections** (Settings → Interface), which 0.13.3 added switched off. The role turns it on for everyone through Open WebUI's default interface settings (`openwebui_default_interface_settings`, `tasks/openwebui_interface_defaults.yml`); a user who switches it off keeps their choice. With it on, the chat's selected tools and skills show as chips too.

Which models get it is read from LiteLLM on every run, not listed by hand:

- `model_info.supports_reasoning: true` → the switch is attached (the model's Workspace entry gets it in `meta.filterIds`, and in `meta.defaultFilterIds` so new chats start with it on).
- `model_info.supports_none_reasoning_effort: true` as well → the off-state companion is attached too. A model that cannot switch thinking off never receives `none`; there an off switch sends no effort and the provider default applies.
- A router (`auto`, `adaptive`) has no capability flags of its own, so it takes them from the models it can route to: it gets the switch when every one of them is reasoning-capable, and the off-state companion when every one of them accepts `none`. With Gemini models in the pool, an off switch on a router therefore sends no effort and each model's own default applies. A router with a target that cannot reason, or that LiteLLM does not serve, gets nothing.

For a vLLM model both flags come from `vllm_reasoning_parser` and `vllm_reasoning_efforts` (include `none` in the list). After `playbooks/inference.yml` registers or retires a reasoning model, rerun the sync:

```bash
cd ansible
ansible-playbook playbooks/openwebui_reasoning.yml --check   # preview, writes nothing
ansible-playbook playbooks/openwebui_reasoning.yml
# or as part of the role: ansible-playbook playbooks/site.yml --tags openwebui-config
```

Knobs (`ansible/roles/openwebui_stack/defaults/main.yml`):

| Variable | Default | Meaning |
|---|---|---|
| `openwebui_reasoning_switch_enabled` | `true` | `false` removes the switch from every model and disables both functions |
| `openwebui_reasoning_switch_default_on` | `true` | New chats start with the switch on |
| `openwebui_reasoning_discover_models` | `true` | Read the model list from LiteLLM |
| `openwebui_reasoning_models_extra` | `[]` | Extra `model_name`s that get the switch |
| `openwebui_reasoning_off_models_extra` | `[]` | Extra `model_name`s that accept `none` |
| `openwebui_reasoning_models_exclude` | `[]` | Never attach, whatever LiteLLM reports |
| `openwebui_default_interface_settings` | `{showInputSelections: true}` | Site-wide defaults for Settings → Interface; `showInputSelections` shows the Thinking chip beside `+`. Only the listed keys are set, and a user's own choice wins |

Behavior to know:

- **Precedence** (highest first): Reasoning Effort in the Workspace model's Advanced Parameters → Reasoning Effort in the chat's Controls → the switch → the LiteLLM default (`vllm_reasoning_effort`). The LiteLLM default now only applies to clients that send no effort (API callers, MAGIE's Auto); Open WebUI always sends one for a model with the off-state companion.
- **Background calls** (title and tag generation) carry no switch, so on those models they run with `none` and no longer spend tokens on thinking.
- **Model entries**: attaching creates a Workspace entry for the base model when it has none. It is created without access grants, which is the visibility an unconfigured model already has (admins only); an existing entry keeps its grants, name, parameters and other settings, and Workspace presets are never touched. Reruns remove the two filter ids from entries whose model stopped being reasoning-capable.
- **Gemma 4** thinking is on/off, so `low`, `medium` and `high` behave the same there.

### Repetition-loop mitigation

Gemma 4 can repeat indefinitely when grammar-constrained decoding is active (for example, JSON schemas or tool arguments). The role sends `vllm_repetition_detection` to vLLM through LiteLLM on every request by default. Its defaults (`max_pattern_size: 20`, `min_pattern_size: 3`, `min_count: 4`) match [vLLM PR #40099](https://github.com/vllm-project/vllm/pull/40099): vLLM stops output after a 3–20-token phrase repeats four times. A request-level `repetition_detection` object overrides it; set `vllm_repetition_detection: {}` to disable it.

This is a safety stop, not a repair for malformed prompts. It may return a partial response when a genuine phrase repeats, so validate structured output and retry only that request. Run the inference playbook after changing this setting; registration detects the changed LiteLLM parameter, replaces the stored deployment, and does not require a gateway restart.

---

## LLM Launcher (web control panel per inference server)

The role can put a browser GUI in front of each independent inference server: [Local-LLM-Launcher-GUI](https://github.com/jimdawdy-hub/Local-LLM-Launcher-GUI) (MIT), pinned to a commit (`vllm_launcher_version`, v0.6.1). It searches and downloads Hugging Face models, shows every `vllm serve` setting as a form control with a green/yellow/red rating against the node's GPU and a plain-language reason, starts and stops servers, shows their logs, and saves launch settings as favorites. It is off by default (`vllm_launcher_enabled`).

The launcher only manages the machine it runs on, so placement follows the topology:

| `vllm_deployment_mode` | Launchers | Why |
|---|---|---|
| `standalone` | One on every `vllm_compute` node | Every node is its own inference server |
| `ray` | One, on the `vllm_ray_head` node | The head drives the single sharded server |

`vllm_launcher_hosts` (a list of inventory hostnames) overrides the rule. A node that stops qualifying has its launcher units removed on the next run.

### Request path

```
 browser ── https://launcher-<node>.<domain> ──▶ Traefik (stack host, TLS)
                                                   │ http://<node>:4180
                                                   ▼
        compute node:  vllm-launcher-auth (oauth2-proxy)  ◀── signs in against Keycloak
                                   │ http://127.0.0.1:8765      on the stack host (auth.<domain>)
                                   ▼
                       vllm-launcher (GUI + API, loopback only)
                                   │ child processes
                                   ▼
                       vllm serve … (the models started from the GUI)

 stack host: launcher-follower ── http://<node>:<gate port>/api/stack/slot ──▶ gate ──▶ launcher
             (every 30 s, bearer token; Traefik does not publish /api/stack/)
             └──▶ LiteLLM entry for http://<node>:8000/v1 renamed to what is served ──▶ Open WebUI
```

- **`vllm-launcher`** is a container built on the node from the node's own vLLM image (`vllm_launcher_base_image`, default `vllm_image`), so the `vllm` it starts is the same build the Ansible units run. It mounts the same Hugging Face cache, so models the node already has show up as installed. It runs with host networking and listens on `127.0.0.1:8765` only; that port is never opened in the firewall. Its state (settings, favorites, benchmark records, server logs) lives in `vllm_launcher_data_dir` (`/opt/vllm/launcher/data`, mode `0700`).
- **`vllm-launcher-auth`** is oauth2-proxy (same version and settings as the voice gate in `docker-compose.yml`) on port 4180, the only way in from the network. It owns the OAuth flow, admits the roles in `vllm_launcher_allowed_roles` (`launcher:launcher_admin`), and forwards to the launcher. The port opens in the `vllm-cluster` firewalld zone. `--add-host` pins `auth.<domain>` to the stack host (`vllm_launcher_keycloak_host_ip`, default `vllm_controller_host`), so the node needs no DNS for it.
- **Traefik on the stack host** publishes each gate as `https://launcher-<inventory hostname>.<domain>` from a file-provider config that `playbooks/site.yml` renders (`traefik/dynamic/launcher.yml` in the deployed tree). The same run creates the `launcher` Keycloak client with one redirect URI per portal and the `launcher_admin` client role, mapped to the `platform-admins` and `admin` groups.
- **Sign-in chip and Log out**: the image build adds two files next to the launcher's compiled frontend and two tags to its `index.html` (`files/vllm-launcher-patch-ui.py`). The script reads `/oauth2/userinfo`, shows the signed-in user in the top bar, and links Log out to `/oauth2/sign_out?rd=<Keycloak end_session_endpoint>`, the same single-logout mechanism as the voice portal.
- **No chat**: the launcher is a control panel, not a chat client. Its "Test chat" box and its "Open WebUI" panel (which would start a second Open WebUI on the node with no login) are switched off; see [Chat is off in the launcher](#chat-is-off-in-the-launcher).

Only admins get in on purpose. The launcher starts processes, accepts free-text engine arguments, downloads models onto the node's disk, and can hold a Hugging Face token, while the realm allows self-registration. Set `vllm_launcher_allowed_roles: []` to admit every signed-in user.

### Enabling it

Order matters: the gates need the Keycloak client and its secret, which the stack playbook creates.

1. **Stack side.** In `ansible/group_vars/all/vars.yml` set `launcher_enabled: true`. Portals default to every `vllm_compute` host of the stack inventory at its `ansible_host`. When the nodes are deployed from a different inventory (`tests/vllm-remote`) or the stack inventory holds placeholder addresses, name the real ones as inventory vars in `inventory/hosts.local.yml` (gitignored):

   ```yaml
   all:
     vars:
       launcher_nodes: [vllm-node-01]
       launcher_node_addresses:
         vllm-node-01: 192.0.2.20   # address the stack host reaches the node on
   ```

   The names must be the inventory hostnames `inference.yml` deploys under: each gate answers as `launcher-<its inventory hostname>.<domain>`, and Keycloak only accepts redirect URIs it was given.
2. **DNS and the network.** Add a record for each `launcher-<node>.<domain>` next to the other service names, pointing at the **stack host** like them: Traefik there serves HTTPS for the portal and forwards to the node. A record that points at the node itself reaches nothing, because the gate speaks plain HTTP on its own port. A `*.<domain>` certificate already covers the names. The stack host must reach the gate port on the node (`vllm_launcher_auth_port`, 4180): the role opens it in the node's `vllm-cluster` zone, and a router between the two networks has to pass it too. If the router passes only some ports, pick one it passes and set it on both sides, `launcher_auth_port` for the stack and `vllm_launcher_auth_port` for the node.
3. **Run `playbooks/site.yml`.** It generates `launcher_oidc_client_secret`, creates the client and role mappings, renders the Traefik routes and writes `LAUNCHER_OIDC_CLIENT_SECRET` and `LAUNCHER_SSL_INSECURE_SKIP_VERIFY` into `.env`.
4. **Node side.** Set `vllm_launcher_enabled: true` in the group_vars the node is deployed from (`ansible/group_vars/vllm_compute/vars.yml`, or `ansible/tests/vllm-remote/group_vars/vllm_compute/vars.yml` for the remote node) and run `playbooks/inference.yml` (`tests/vllm-remote/run.sh inference`).

The node play resolves the stack's domain, realm, client secret and TLS-verify setting in order: `vllm_launcher_domain` / `_keycloak_realm` / `_oidc_client_secret` / `_ssl_insecure_skip_verify` → the controller environment (`DOMAIN`, `KEYCLOAK_REALM`, `LAUNCHER_OIDC_CLIENT_SECRET`, `LAUNCHER_SSL_INSECURE_SKIP_VERIFY`; the vllm-remote runner sources the deployed `.env`) → the deployed `.env`, read from the stack host in the inventory or from the controller itself when the inventory has none. It fails before touching the node when the domain or secret is missing.

A run on a launcher node: installs `git` if missing → checks out the source → builds `localhost/vllm-launcher:<commit>` (the launcher runs from its source tree and nothing is installed when the vLLM image already ships its dependencies, which `vllm/vllm-openai` does, so the build needs no network) → generates the gate's cookie secret once → renders scripts and units → opens port 4180 and `vllm_launcher_model_ports` → starts both units → waits for `/api/about` and `/ping`.

### Swapping and tuning the node's model from the GUI

By default the launcher runs next to the Ansible-managed units and shares the GPU with them. On a node whose GPU `vllm-serve` already fills, the GUI rates every launch red until the primary slot is handed over:

```yaml
vllm_launcher_owns_primary: true   # group var, or a host var for one node
```

`vllm-serve` then stays installed but stopped and disabled, its `/health` wait is skipped, and the GPU and `vllm_port` are free for the GUI. Flip it back and the next run restores the Ansible-managed server. Standalone mode only.

With the primary slot handed over, the launcher runs **one model at a time** on `vllm_port` (`vllm_launcher_swap`, on by default), and LiteLLM and Open WebUI follow whatever it serves:

- **Swap.** Find a model on the Models page (search Hugging Face, Download), then **Launch…** it, or pick it on the Launch page. Launching first runs the checks the launcher would run anyway, so settings it refuses never stop anything; then it stops the running model, waits until its GPU memory and port are free, and starts the new one on port 8000 whatever the form's Port says. The served name is the form's, or the last part of the repo id (`Qwen/Qwen3-8B` → `Qwen3-8B`); the configured model (`vllm_model`) keeps `vllm_served_model_name`.
- **Tune.** The Launch page opens on the running model, with the settings it runs with as its first preset, **Running now**. Change any of them and launch: the model restarts with them. **Tune settings** on the running server's card (Servers page) opens the same page.
- **Fit.** The verdict and the Models page's Fits / Tight / Won't fit rate each model against the **whole GPU**, not against what the running model leaves free: memory the launcher's own server holds counts as free, because the launch frees it first. Memory other programs hold still counts as used; it is sampled while no launcher server runs. The Dashboard keeps showing the real free memory. The verdict also says what the launch replaces.
- **Rollback.** When the new model exits before it answers `/health` (out of memory, a wrong parser, a bad flag), the settings it replaced start again by themselves (`vllm_launcher_swap_rollback`). The failed attempt and its log stay on the Servers page; the log says what was started again. Stopping the new model by hand is not a failure, and a model still loading after `vllm_launcher_swap_load_timeout` (45 minutes) is left alone.
- **Open WebUI.** The stack's launcher follower (service `launcher-follower`, `launcher_follow_enabled`) reads each node's slot every 30 seconds through its gate. Once the new model answers, it renames LiteLLM's entry for the node's port to the served name and sets its context length (vLLM's `max_model_len`), reasoning flags, default effort and repetition detection, then puts the Thinking switch on the model in Open WebUI when it reasons. Open WebUI lists the new name after a page reload, usually within a minute of the model answering. Until then, and while a model loads, requests to the node fail. The entry keeps its id, endpoint and stored API key; `inference.yml` only creates it when it is missing and no longer prunes or replaces it.

How LiteLLM lists a model started from the GUI:

| | The configured model (`vllm_served_model_name`) | Any other model |
|---|---|---|
| Reasoning capable | with a reasoning parser in the launch settings | with a reasoning parser in the launch settings |
| Effort levels (opencode picker, Thinking switch off state) | `vllm_reasoning_efforts` | `vllm_launcher_reasoning_efforts` (`[none, low, medium, high]`) |
| Default effort | `vllm_reasoning_effort` | `vllm_launcher_reasoning_effort` (none) |
| Adaptive-router preferences | from `vllm_adaptive_quality_tier` / `_strengths` | none |
| Repetition detection | `vllm_repetition_detection` | `vllm_repetition_detection` |

Tool calling is not optional on this stack: LiteLLM's web search and Open WebUI's MCP tool servers send tools with every chat, and vLLM refuses such a request ("auto tool choice requires --enable-auto-tool-choice and --tool-call-parser") from a server started without both. So a launch that leaves **Tool-calling format** empty gets the one for the model's family (Qwen3-Coder: `qwen3_coder`; other Qwen and QwQ: `hermes`; Gemma 4: `gemma4`; Llama 3: `llama3_json`; Mistral, Devstral and the like: `mistral`), and every launch with a format gets `--enable-auto-tool-choice` added. The Launch page says which format it will use, and warns when the family is unknown: pick one there, or every chat with that model fails. Thinking still needs the reasoning parser in the form (the advisor suggests one by family); a model launched without one is listed as not reasoning. The configured model's full settings, copied from the Ansible server's command line (`/opt/vllm/vllm-serve.sh` on the node, from `vllm_extra_args`, `vllm_reasoning_parser` and `vllm_chat_template_kwargs`), are saved as a favorite; for the Gemma 4 model on the remote test node they are:

| Form field | Value |
|---|---|
| Reasoning parser | `gemma4` (without it, thinking is not separated from the answer) |
| Tool call parser | `gemma4` |
| KV cache dtype, max num seqs, enforce eager, prefix caching | `fp8`, `16`, on, on |
| Extra arguments | `--enable-auto-tool-choice --default-chat-template-kwargs '{"enable_thinking": true}' --disable-log-requests` |

The chat-template default is what makes thinking the default, as the Thinking switch in Open WebUI expects. The same launch through the API, on the node:

```bash
curl -s -X POST http://127.0.0.1:8765/api/servers -H 'Content-Type: application/json' -d '{
  "engine_mode": "vllm-native", "repo_id": "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
  "config": {"port": 8000, "served_model_name": "gemma-4-26b-a4b", "max_model_len": 262144,
             "gpu_memory_utilization": 0.9, "tensor_parallel_size": 1, "max_num_seqs": 16,
             "kv_cache_dtype": "fp8", "enforce_eager": true, "enable_prefix_caching": true,
             "reasoning_parser": "gemma4", "tool_call_parser": "gemma4",
             "extra_args": "--enable-auto-tool-choice --default-chat-template-kwargs '\''{\"enable_thinking\": true}'\'' --disable-log-requests"}}'
```

Save it as a favorite to relaunch in one click (`POST /api/servers/<id>/favorites` with a `name`; relaunch with `POST /api/favorites/<favorite id>/launch`). A favorite's launch is a swap like any other.

Knobs (`roles/vllm_compute/defaults/main.yml`, and `roles/openwebui_stack/defaults/main.yml` for the follower):

| Variable | Default | Meaning |
|---|---|---|
| `vllm_launcher_swap` | `true` | One model at a time on `vllm_port`, with `vllm_launcher_owns_primary`. `false`: launches start next to each other again, on free ports, and LiteLLM is not followed |
| `vllm_launcher_swap_rollback` | `true` | Start the replaced settings again when a new model exits before it answers |
| `vllm_launcher_swap_load_timeout` | `2700` | Seconds the rollback watcher waits for a new model to answer |
| `vllm_launcher_swap_release_timeout` | `90` | Seconds to wait for the stopped model's GPU memory and port before starting anyway |
| `vllm_launcher_reasoning_efforts` / `_effort` | `[none, low, medium, high]` / `""` | How LiteLLM lists a GUI-started reasoning model other than the configured one |
| `launcher_follow_enabled` | `true` | The `launcher-follower` service on the stack host |
| `launcher_follow_interval` | `30` | Seconds between its passes |

The follower reads `GET /api/stack/slot` on each node's gate. The gate lets that one route through without a sign-in, and the launcher answers it only with the bearer token `launcher_status_token` (generated by `site.yml`, `LAUNCHER_STATUS_TOKEN` in `.env`, rendered into the node's `/opt/vllm/launcher/launcher.env` by `inference.yml`). Traefik does not publish `/api/stack/` on the portal. Run `site.yml` before `inference.yml`: the node play stops when it finds no token.

### Chat is off in the launcher

People talk to models through Open WebUI or an API client (opencode, an IDE), so the launcher's two chat paths are off by default (`vllm_launcher_chat_enabled: false`):

| Launcher feature | What it did | Now |
|---|---|---|
| **Test chat** on a server card | Sent a prompt to the server through `POST /api/servers/<id>/chat` | Button hidden; the request answers 403 |
| **Open WebUI** panel on the Dashboard | Started a second Open WebUI on the node, bound to the network with no login | Panel hidden; `/api/openwebui/launch` and `/stop` answer 403 |

Two layers, both in the image:

- **The API** is what enforces it. The image starts through `files/vllm-launcher-entry.py` (`/opt/llm-launcher/overlay/stack_entry.py`), which replaces those routes before the launcher builds its app. The 403 names Open WebUI and API clients. It fails closed: if a newer launcher moved the routes, the build stops in `patch_ui.py` and the entry point refuses to start, each naming the fix.
- **The page** is tidied by the build: a style rule hides the Open WebUI panel, the page script hides the "Test chat" button by its label, and the three hints that said "run a test chat" point at Open WebUI.

To check a model after launching it, send it a message from Open WebUI, or `curl` it through LiteLLM. The Benchmark button reads timings from the server log, so it works after any request, whoever sent it.

`vllm_launcher_chat_enabled: true` brings both back. Changing it rebuilds the image and restarts the launcher, which stops the models it started. Note that the test chat sends no API key, so it also needs `vllm_launcher_enforce_api_key: false`.

### What the launcher does not do

- **It does not see the Ansible units.** It lists only servers it started. `vllm-serve`, `vllm-flux-*` and `vllm-aux-*` stay under `systemctl`.
- **Nothing restarts a GUI-started model.** They are child processes of the launcher container: a reboot, a `systemctl restart vllm-launcher`, or a playbook run that rebuilds the image or changes its script stops them, and they stay down until relaunched. A run that changes nothing leaves them alone.
- **It is not a chat client.** Test chat and the Open WebUI panel are off (see above); test through Open WebUI, or `curl` through LiteLLM.
- **One model per node through LiteLLM.** The follower keeps one LiteLLM entry per node in step with the model on `vllm_port`. With `vllm_launcher_swap: false`, a model under another name or on another port (`vllm_launcher_model_ports`, 8100-8109) is not registered: add it in the LiteLLM UI (`http://<node>:<port>/v1`, the node's API key).
- **Its servers' logs are not shipped to Loki.** The log shipper reads the `vllm-serve` unit's journal; a GUI-started server logs to a file under `vllm_launcher_data_dir`, shown in the GUI's Servers tab. Metrics are unaffected: the collector scrapes `/metrics` on `vllm_port` whoever started the server.
- **vLLM only.** The image carries no llama.cpp, SGLang or TGI, and the launcher's Docker-based modes are unavailable inside the container. `vllm_launcher_extra_build_steps` appends Dockerfile lines to add an engine (`llama-server` on `PATH` enables llama.cpp).
- **Ray mode is limited.** The form has no pipeline-parallel field and the memory advice counts only the head's GPU, so the sharded primary stays under Ansible (`vllm_launcher_owns_primary` is refused). The head's launcher is for downloads and extra single-node models.
- **Engine source updates are off.** The launcher only allows them from a direct loopback connection, which a request through the gate never is.

### Verification

```bash
# on the node
systemctl status vllm-launcher vllm-launcher-auth
curl -s http://127.0.0.1:8765/api/about                                 # launcher version
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4180/ping     # 200: gate up
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/api/openwebui/launch   # 403: chat is off

# on the stack host: an anonymous request is sent to Keycloak
curl -sk -o /dev/null -w '%{http_code} %{redirect_url}\n' https://launcher-vllm-node-01.<domain>/
# 302 https://auth.<domain>/realms/platform/protocol/openid-connect/auth?...

# on the stack host, in the deployed tree: what the follower reads and does
docker compose logs --tail 20 launcher-follower
docker compose exec launcher-follower python3 /opt/stack-scripts/launcher_follower.py --once --check
TOKEN=$(grep -m1 '^LAUNCHER_STATUS_TOKEN=' .env | cut -d= -f2-)
curl -s -H 'Host: launcher-vllm-node-01.<domain>' -H "Authorization: Bearer $TOKEN" \
  http://<node>:<gate port>/api/stack/slot
# {"version": 1, "state": "serving", "litellm": {"model_name": "gemma-4-26b-a4b", ...}}
```

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `inference.yml` stops at "has no stack domain or no OIDC client secret" | `site.yml` has not run with `launcher_enabled: true`, or the node play cannot read the deployed `.env` | Run `site.yml` first; or set `vllm_launcher_domain` and `vllm_launcher_oidc_client_secret` |
| Portal answers 503 / "no available server" | Traefik's `/ping` check on `<node>:<gate port>` (4180 by default) fails: gate down, port closed, or wrong address | `systemctl status vllm-launcher-auth` on the node; check `launcher_node_addresses` and the `vllm-cluster` zone source |
| Portal answers 503 although `curl http://127.0.0.1:<gate port>/ping` on the node answers 200, and `curl http://<node>:<gate port>/ping` from the stack host times out | A router between the stack host and the node drops the gate port (a packet capture on the node shows nothing arriving) | Let the router pass the port, or move the gate to a port it passes: `launcher_auth_port` (stack) and `vllm_launcher_auth_port` (node) to the same value, then rerun both playbooks |
| The browser cannot connect to `launcher-<node>.<domain>` at all | Its DNS record points at the node, where nothing serves HTTPS | Point it at the stack host, like `chatbot.<domain>` |
| `vllm_launcher_enabled: true` in `tests/vllm-remote/group_vars/vllm_compute/vars.local.yml` has no effect | Ansible reads that directory alphabetically, so `vars.local.yml` comes before `vars.yml` and loses to any variable `vars.yml` also sets | Keep the switch out of `vars.yml` (it is, since this was found); set it only in `vars.local.yml` |
| Keycloak shows "Invalid parameter: redirect_uri" | The portal's hostname is not on the `launcher` client: the stack inventory and the node inventory name the node differently | Make `launcher_nodes` match the node's inventory hostname, rerun `site.yml` |
| 403 after signing in | The user lacks `launcher_admin` | Add the user to `platform-admins` or `admin` in Keycloak |
| Gate log shows `x509: certificate signed by unknown authority` | `auth.<domain>` serves a certificate the proxy does not trust | `oauth2_proxy_ssl_insecure_skip_verify: true` on the stack (rendered as `LAUNCHER_SSL_INSECURE_SKIP_VERIFY`), rerun both playbooks |
| Every launch is rated red, no free VRAM | `vllm-serve` holds the GPU | `vllm_launcher_owns_primary: true` for that node |
| A launch answers "Port 8000 is in use by a program this launcher did not start" | `vllm-serve` (or something else) listens on the slot port | `systemctl status vllm-serve`; set `vllm_launcher_owns_primary: true` and rerun `inference.yml` |
| A launch answers "Another launch is replacing the model right now" | A swap is still stopping the old model or starting the new one | Wait a minute and launch again |
| Every chat with a swapped-in model fails with "auto tool choice requires --enable-auto-tool-choice and --tool-call-parser" | The model runs without a tool-calling format: its family is not one the launcher knows, and the form left the format empty | Launch it again with **Tool-calling format** set (Launch page, Advanced settings) |
| A swapped-in model stopped and the previous one came back | The new model exited before it answered (rollback) | Read the failed server's log on the Servers page; fix the settings and launch again |
| Open WebUI still lists the previous model's name | The new model is still loading, or LiteLLM's workers have not reloaded yet | Wait for the server to show Running, then reload Open WebUI; `docker compose logs launcher-follower` on the stack host |
| `launcher-follower` logs "the gate wants a sign-in for the slot status" | That node runs no slot (`vllm_launcher_swap` or `vllm_launcher_owns_primary` off), or its gate predates the slot | Rerun `inference.yml`; with the slot off on purpose, the follower only reports the node |
| `launcher-follower` logs "the slot status answered 401" | The node has another status token than the stack's `.env` | Rerun `inference.yml` after `site.yml` |
| `launcher-follower` logs "LiteLLM has no entry for http://<node>:8000/v1" | The node's entry was deleted or never created | Rerun `inference.yml`: it creates the entry, and the follower names it after the served model |
| `inference.yml` stops at "has no status token" | `site.yml` has not generated `launcher_status_token` yet | Run `site.yml` first |
| No "Test chat" button, no Open WebUI panel | Chat is off in the launcher by design | Chat through Open WebUI; `vllm_launcher_chat_enabled: true` brings them back |
| `vllm-launcher` exits with "cannot switch chat off" (or the image build stops in `patch_ui.py`) | `vllm_launcher_version` points at a launcher that moved its chat endpoints | Update the paths in `files/vllm-launcher-entry.py` and `files/vllm-launcher-patch-ui.py`, or set `vllm_launcher_chat_enabled: true` |
| Models started from the GUI are gone | The launcher container restarted (reboot, image rebuild, changed unit) | Relaunch from Favorites |

Every variable is documented in `roles/vllm_compute/defaults/main.yml` (`vllm_launcher_*`) and `roles/openwebui_stack/defaults/main.yml` (`launcher_*`).

---

## NVIDIA driver (the `nvidia_driver` role)

Both playbooks start with the `nvidia_driver` role. It looks at the host first and then does the least that makes `nvidia-smi` work:

| What it finds | What it does |
|---|---|
| No NVIDIA GPU | Nothing |
| `nvidia-smi` works | Nothing |
| A kernel module built for the running kernel but not loaded | Loads it |
| A driver installed with no kernel module for the running kernel: the state a kernel update leaves behind | Rebuilds the module for the running kernel, keeping the installed libraries and tools |
| No driver at all | Installs one |

How it installs or rebuilds depends on the system (`nvidia_driver_method: auto`):

- **Fedora and the RHEL family**: NVIDIA's own package repository, with the open kernel modules built by DKMS (`kmod-nvidia-open-dkms`, `nvidia-driver`, `nvidia-driver-cuda`). DKMS also rebuilds the module by itself when a new kernel is installed, as long as its headers are there.
- **VMware Photon OS**, and any host whose driver came from NVIDIA's `.run` installer: the `.run` installer of the installed version, with `--kernel-module-only` for a rebuild. Photon has no DKMS, so every kernel update needs this rebuild; the role installs the matching `linux-<flavor>-devel` headers first. A first install needs `nvidia_driver_version`. Installers are kept in `nvidia_driver_runfile_dir` (`/root/nvidia`), so a later rebuild downloads nothing.

After a repair it loads `nvidia` and `nvidia_uvm` and regenerates the container device file (`/var/run/cdi/nvidia.yaml`) when the NVIDIA container toolkit is installed.

It never reboots a host and never restarts a container. A first install while `nouveau` holds the GPU needs a reboot; the role says so and stops there. Containers that could not get the GPU start again when their owner runs: the stack's compose step, the vLLM units.

| | `inference.yml` (compute nodes) | `site.yml` (stack host) |
|---|---|---|
| A driver that cannot be made to work | Stops the play (`nvidia_driver_required: true`) | Reported; the deploy goes on and the `gpu` compose profile is left out of the run |

Run it alone with `--tags nvidia_driver`. A dry run (`--check`) reports what it would do and changes nothing:

```bash
# from ansible/ on the controller
ansible-playbook -i inventory/hosts.yml -i inventory/hosts.local.yml playbooks/site.yml --tags nvidia_driver
ansible-playbook -i tests/vllm-remote/inventory.yml -i tests/vllm-remote/inventory.local.yml playbooks/inference.yml --tags nvidia_driver --check
```

Variables in `roles/nvidia_driver/defaults/main.yml`: `nvidia_driver_enabled`, `nvidia_driver_install` (false: detect and report only), `nvidia_driver_required`, `nvidia_driver_method`, `nvidia_driver_version`, `nvidia_driver_runfile_*`, `nvidia_driver_dnf_*`.

## Rootless containers and tini

Two settings in `tasks/container_runtime.yml` apply to every container the role starts: the model servers, Ray, the launcher and its gate, the exporters and the log shipper.

### tini as the init process

Each container starts under an init process as PID 1 (`vllm_container_init: true`). It forwards the stop signal to the server and reaps the processes a server leaves behind. Without one, a server that ignores SIGTERM as PID 1 is only stopped by the kill after the timeout: on the test node a container stopped in 0.25 s with tini and in 5 s (killed) without.

| CLI | What `--init` runs | What the role does |
|---|---|---|
| docker | tini (`docker-init`) | Passes `--init` |
| podman | catatonit | Installs `tini-static` (`vllm_container_init_package`) and passes `--init --init-path /usr/bin/tini-static` (`vllm_container_init_path`) |

It has to be the static build: podman mounts the host's binary into the container, and a dynamically linked tini only starts in images that carry its libraries. Inside the container PID 1 shows as `podman-init`, which is the name podman mounts it under. Set `vllm_container_init_path: ""` to use catatonit, or `vllm_container_init: false` for none.

### Rootless podman

On a podman node the containers run as an unprivileged service account instead of root (`vllm_container_rootless`, on by default wherever `vllm_container_cli` is `podman`). Root inside a container is then that account on the host, and an image that runs as its own non-root user lands on one of the account's subordinate UIDs. A process that breaks out of a container holds no root and no sudo. Docker nodes stay as they are: the docker daemon itself runs as root.

What the role sets up:

| Piece | Detail |
|---|---|
| Service account | `vllm_container_user` (`vllm`): created when missing, no password, no login shell. An existing account is used unchanged. Do not point it at an account with sudo rights |
| Subordinate IDs | From `useradd`; added with `usermod` for an account that has none |
| Lingering | `loginctl enable-linger`: the account's user manager and `/run/user/<uid>` exist from boot, without a login. The containers' cgroup scopes live there, so without lingering they would stop when the account's last session ends |
| Units | Still system units with the same names, so `systemctl status vllm-serve`, `journalctl -u vllm-serve` and the log shipper's journal match are unchanged. They gain `User=`, `Group=`, `Environment=XDG_RUNTIME_DIR=/run/user/<uid>`, `Delegate=yes` and an ordering on `user@<uid>.service` |
| Logs | `Delegate=yes` keeps podman inside the unit's cgroup. Without it podman moves itself into a scope of the account's user manager and journald files the server's output under `user@<uid>.service`: `journalctl -u vllm-serve` shows only start and stop lines, and the log shipper sends nothing. The containers run with `--log-driver none`, because podman's own journald logging would then file every line a second time under the same unit (and `passthrough` files it under the account's scope again). So the log is in the journal only: `podman logs` shows nothing for these containers |
| Stop | Every unit has `SuccessExitStatus=143`: tini exits 143 when it stops the server on `systemctl stop`, which is a clean stop, not a failure |
| `NoNewPrivileges` | Off on the unit, passed to the container instead (`--security-opt no-new-privileges`). On the unit it blocks `newuidmap`, which podman needs to set up the user namespace: after a reboot every start fails with `newuidmap: write to uid_map failed` |
| GPU | CDI (`vllm_container_gpu_flag: "--device nvidia.com/gpu=all"`), which needs no root |
| Files | Launch scripts and the gate's `auth.env` stay root-owned and are group-readable by the account (it can run them, not rewrite them). `vllm_hf_cache_dir` and the launcher's data directory belong to the account |
| Image store | The account's own (`~/.local/share/containers/storage`; move it with `vllm_rootless_storage_root`). Tasks that call podman themselves (the launcher image build, the Ray check, a registry login) go through `vllm_container_argv`, which runs podman as the account |
| Exporters | System units from `templates/vllm-exporter.service.j2` in place of the root quadlets, under the same `<name>.service` names |
| Log shipper | Reads `/var/log/journal` through the `systemd-journal` group (`--group-add keep-groups`) |

**Moving an existing node.** The first run with rootless on does it in place:

1. Creates the account and enables lingering.
2. Stops the role's units that still run as root and removes their containers from root's store (other containers are not touched).
3. Hands `vllm_hf_cache_dir` to the account (one `chown -R`; the weights are not downloaded again).
4. Rewrites the scripts and units and starts them. The account's image store is empty, so this start pulls the images again, 25 GB for a vLLM image. The model is down for the length of that pull plus the normal load time.

Afterwards root's copies of the images are unused; remove them with `sudo podman rmi <image>` to get the space back. Check free space first: a pull needs room for the image in the account's store on top of root's copy.

To skip the download, copy the image from root's store into the account's before the run (the account must exist: create it with `useradd`, or let a first run stop at the pull). podman spools the whole image in its temp directory on both sides, `/var/tmp` by default, so point `TMPDIR` at a volume with room for twice the image; on the test node the default filled the root filesystem and the copy failed:

```bash
sudo install -d -m 1777 /home/podman-tmp
sudo env TMPDIR=/home/podman-tmp podman save docker.io/vllm/vllm-openai:latest \
  | sudo -u vllm env -C / TMPDIR=/home/podman-tmp XDG_RUNTIME_DIR=/run/user/$(id -u vllm) podman load
sudo rm -rf /home/podman-tmp
```

Set `vllm_container_rootless: false` to keep a node rootful.

**Checks on a rootless node:**

```bash
systemctl show -p User,Group vllm-serve                         # User=vllm
sudo -u vllm XDG_RUNTIME_DIR=/run/user/$(id -u vllm) podman ps  # the role's containers
sudo podman ps                                                  # none of them here
loginctl show-user vllm -p Linger                               # Linger=yes
```

| Symptom | Cause | Fix |
|---|---|---|
| Unit fails with `newuidmap: write to uid_map failed: Operation not permitted` | `NoNewPrivileges=true` on a rootless unit (a hand-edited unit or drop-in) | Remove it; the container already runs with `no-new-privileges` |
| `podman ps` as root shows nothing while the model answers | Expected: the containers are in the service account's store | Use the `sudo -u vllm ... podman ps` form above |
| `journalctl -u vllm-serve` shows only "Started"/"Stopped", and no vLLM logs reach Grafana | The unit lost `Delegate=yes`, so the output is filed under `user@<uid>.service` | Rerun the playbook; meanwhile `journalctl _SYSTEMD_UNIT=user@$(id -u vllm).service` has it |
| Every log line appears twice in Grafana | A container was started without `--log-driver none` | Rerun the playbook so the launch scripts are rewritten |
| `podman logs vllm-serve` is empty | Expected with `--log-driver none` | `journalctl -u vllm-serve` |
| Containers stop when nobody is logged in, or after a reboot the units fail with "no such file or directory: /run/user/<uid>" | Lingering is off for the account | `sudo loginctl enable-linger vllm`, or rerun the playbook |
| Model load fails with permission errors under the Hugging Face cache | Files in `vllm_hf_cache_dir` still belong to root | Rerun the playbook (it hands the tree over), or `sudo chown -R vllm: <dir>` |
| `Error: short-name ... did not resolve` or pulls the image again on every start | The image is in root's store, not the account's | Normal on the first rootless start; later starts use the account's copy |
| The log shipper logs "permission denied" on the journal | The account is not in `systemd-journal` | Rerun the playbook; it adds the group |

---

## Setup

### 1. Prepare each node (one-time, outside the role)

Per Fedora node — with the default `prerequisites_install_missing: false` the role checks these and fails fast with guidance; set `prerequisites_install_missing: true` in the node group's group_vars and the `prerequisites` role installs the container CLI, the NVIDIA container toolkit, and generates the CDI spec instead:

**Docker nodes:**

```bash
dnf install docker docker-compose nvidia-container-toolkit akmod-nvidia xorg-x11-drv-nvidia-cuda
systemctl enable --now docker
nvidia-ctk runtime configure --runtime=docker && systemctl restart docker
```

**Podman nodes** (daemonless; GPU injection uses CDI, so only the toolkit + a generated spec are needed):

```bash
dnf install podman nvidia-container-toolkit
nvidia-ctk cdi generate --output=/var/run/cdi/nvidia.yaml
```

- `docker-compose` is the compose plugin package on Fedora (Photon: `tdnf install docker docker-compose`).
- The NVIDIA driver comes from the `nvidia_driver` role, the first role `inference.yml` runs: it installs NVIDIA's packages when a GPU has no driver (see [NVIDIA driver](#nvidia-driver-the-nvidia_driver-role)). Do not mix in RPM Fusion's `akmod-nvidia`: two driver sources on one node conflict.
- Docker nodes run `nvidia-ctk runtime configure` to register the `nvidia` runtime `--gpus all` needs; podman nodes use `--device nvidia.com/gpu=all` (set `vllm_container_gpu_flag` accordingly), which resolves through the CDI spec.
- Rootful podman defaults its image store to `/var/lib/containers/storage`; when `/var` is small, set `vllm_podman_storage_root` (the role rewrites `/etc/containers/storage.conf`).
- Put a large data volume where `vllm_hf_cache_dir` points — pre-download big models with `huggingface-cli download <repo>` so first serve does not download on every node.

### 2. Inventory

`ansible/inventory/hosts.yml` — mark **exactly one** host `vllm_ray_head: true` (ray mode):

```yaml
vllm_compute:
  hosts:
    vllm-node-01:
      ansible_host: COMPUTE_NODE_01_IP
      ansible_user: root
      vllm_ray_head: true
    vllm-node-02:
      ansible_host: COMPUTE_NODE_02_IP
      ansible_user: root
```

### 3. Configure

`ansible/group_vars/vllm_compute/vars.yml` — model, topology, GPU counts, firewall CIDR, and the `vllm_flux_models` list. Every variable is documented in `roles/vllm_compute/defaults/main.yml`. Supply `VLLM_API_KEY` and `HF_TOKEN` (gated models) in the controller environment (they are in `.env.example`).

### 4. Deploy

From the controller (a machine with SSH to the nodes and the collections installed):

```bash
cd ansible
ansible-galaxy collection install -r requirements.yaml
ansible-playbook playbooks/inference.yml
```

What a run does, per node: validates the config → detects GPUs → opens the `vllm-cluster` firewalld zone (Ray ports 6379, 8265, 13377–13379, 14000–15000 + API 8000 + flux ports + aux ports + monitoring 9100/9400) restricted to the compute CIDR → deploys the launch scripts and systemd units → starts `vllm-ray`, waits for every node to register → starts `vllm-serve`, waits for `/health` → deploys/starts each flux server, waits for each `/health` → deploys/starts each aux server placed on this node, waits for each `/health` → deploys the monitoring exporters (node-exporter, dcgm-exporter) as quadlets and renders the remote-nodes Grafana dashboard. Firewall rules go through the `ansible.posix.firewalld` module (idempotent; needs `python3-firewall`/`python3-dbus`, installed by `prerequisites`), and health waits use `ansible.builtin.uri`; the Ray registration check uses `community.docker.docker_container_exec` or `containers.podman.podman_container_exec` per `vllm_container_cli`.

Services after a successful run:

```bash
systemctl status vllm-ray vllm-serve vllm-flux-flux vllm-flux-flux-schnell vllm-aux-gatekeeper
curl http://127.0.0.1:8000/health
```

### 5. Register with LiteLLM

The run renders `litellm/vllm-models.yaml` in the repo: one `model_list` entry for the primary model (single cluster endpoint in ray mode; one per node in standalone — LiteLLM load balances), one `mode: image_generation` entry per flux model, and one entry per aux (node, entry) placement.

With `store_model_in_db` enabled on the proxy (the default in this stack), the role **also registers the model directly**: it resolves the litellm container's API from the controller, diffs against `GET /v1/models`, and POSTs each missing endpoint to `/model/new` — no restart needed. `model_info` is POSTed with `max_input_tokens`/`max_tokens` as JSON **numbers**: clients that gate on `Number.isFinite` (MAGIE Coder's discovery plugin) reject string limits, so a string `"262144"` made the client fall back to a default context window and refuse to send large prompts even though vLLM accepted them. Requires `LITELLM_MASTER_KEY` exported on the controller (the same secret in `.env`). Missing key, or litellm not running → registration is skipped with the file snippet as fallback.

The same run **prunes stale vLLM models** from the gateway's model db: any registered model whose `api_base` points at a `vllm_compute` node (`http://<ip>:<port>/v1`) but which this run no longer serves — a retired `vllm_model`, a removed aux entry, a changed `max_model_len`, a **non-number stored limit** (legacy string-typed registration), changed `supports_reasoning` or `repetition_detection`, or (primary only) a changed `vllm_adaptive_quality_tier`/`vllm_adaptive_strengths` — is deleted via `POST /model/delete`. Without the prune, removed models linger in every client's model list forever (that is how `Qwen2.5-7B-Instruct` stuck around after the primary was switched). Scope is strict: only db models with a vLLM `api_base` are touched; config-declared models (gemini, bedrock, `auto`/`adaptive`, `voice-assistant`) have no `api_base` and are never deleted.

**Adaptive-router registration.** When `vllm_adaptive_quality_tier` is set (per-node group_vars; see `ansible/roles/vllm_compute/defaults/main.yml`), the primary model's registration carries `adaptive_router_preferences` (`quality_tier`, `strengths`) in its `model_info`, and the rendered snippet includes the same block. List the model under an `auto_router/adaptive_router` deployment's `available_models` in `litellm/config.yaml` to let the bandit route to it; the preferences are its cold-start prior. `vllm_adaptive_input_cost_per_token` is recorded for humans but `/model/new` does not persist cost fields in `model_info` — the adaptive router defaults a missing cost to 0, which is what a self-hosted node costs.

Merge the snippet into `model_list` in `litellm/config.yaml` on the stack host for file-based deployments and reload:

```bash
# on the photon host, in the deployed tree
docker compose restart litellm
```

The models then appear in Open WebUI's model dropdown.

### Operations

| Change | How |
|---|---|
| Switch the primary model | Edit `vllm_model` → re-run the playbook → merge the regenerated snippet |
| Change adaptive-router prefs | Edit `vllm_adaptive_quality_tier`/`vllm_adaptive_strengths` in group_vars → re-run (prune + re-register is automatic) |
| Turn reasoning on/off | Set/clear `vllm_reasoning_parser` (+ `vllm_chat_template_kwargs`) or an aux entry's `reasoning_parser` → re-run (prune + re-register is automatic) |
| Change the default thinking level | Set `vllm_reasoning_effort` (or an aux entry's `reasoning_effort`) → re-run; per-request effort from Open WebUI / MAGIE still overrides it |
| Tune repetition-loop detection | Set `vllm_repetition_detection` (or an aux entry's `repetition_detection`) → re-run; `{}` disables it |
| Change topology | Set `vllm_deployment_mode` / `vllm_cross_node_tensor_parallel` → re-run |
| Add/remove an image model | Edit `vllm_flux_models` → re-run; removal cleans up automatically |
| Add/remove/placement an aux model | Edit `vllm_aux_models` (add/remove hosts in its `hosts` key) → re-run; cleanup is automatic |
| Restart everything on a node | `systemctl restart vllm-ray vllm-serve 'vllm-flux-*' 'vllm-aux-*' 'vllm-*-exporter-container'` |
| Logs | `journalctl -u vllm-serve -f` (or the flux/aux unit); Ray dashboard on the head: `http://<head>:8265` |
| Tear flux down | `vllm_flux_enabled: false` → re-run |
| Tear aux down | `vllm_aux_enabled: false` → re-run |
| Monitoring off | `vllm_monitoring_enabled: false` → re-run (exporter quadlets stay until removed by hand) |
| Tune a node's model from the browser | `vllm_launcher_enabled: true` (+ `vllm_launcher_owns_primary: true` to free the GPU) → re-run → `https://launcher-<node>.<domain>` |
| Swap the node's model | Launcher: Models (search, Download) → Launch… → Launch. The running model stops; LiteLLM and Open WebUI follow within a minute of the new one answering |
| Change the running model's settings | Launcher: Tune settings on its server card (or the Launch page, preset **Running now**) → change → Launch |
| Give the primary model back to Ansible | `vllm_launcher_owns_primary: false` → re-run (stop the GUI-started server first; it holds the port) |
| Update the launcher | Set `vllm_launcher_version` to a newer commit → re-run (rebuilds the image and restarts the launcher, which stops GUI-started models) |
| Tear the launcher down | `vllm_launcher_enabled: false` → re-run (units, scripts and ports removed; data directory and image stay) |

### Security notes

- Ray traffic is unauthenticated and can execute arbitrary code: keep `vllm_firewall_zone_source` limited to the compute segment and always set `VLLM_API_KEY`.
- The zone source binding is greedy: firewalld routes every packet from that subnet into `vllm-cluster`, so the role admits `ssh` there. If you remove that task or swap the zone's source, management hosts in the subnet lose SSH to the node on the next reload — recover from the node's console.
- A first run pulls the model from Hugging Face before `/health` passes; the wait is `vllm_health_wait_retries` × 10s (default 60). Raise it in group_vars for slow links or large models, or pre-download with `huggingface-cli download`.
- Model weights must exist at the same path or HF id on every node; pin the same vLLM image version cluster-wide.
- The LLM Launcher has no login of its own. It listens on the node's loopback only and is reachable solely through its oauth2-proxy gate, which admits the `launcher_admin` role. The hop from Traefik on the stack host to the gate (port 4180) is plain HTTP on the compute segment, like the vLLM API. Servers started from the GUI bind `0.0.0.0` (`vllm_launcher_lan_access`) and carry the node's API key (`vllm_launcher_enforce_api_key`); turning the key off leaves the firewall zone as their only protection.

### Monitoring (exporters + Grafana dashboard)

Each node gets up to two Prometheus exporters when `vllm_monitoring_enabled` is true (default):

- **node-exporter** (`prom/node-exporter`, host port 9100): CPU/memory/disk/network of the node itself, read-only mounts of `/`, `/proc`, `/sys`.
- **The flavor-correct GPU exporter**, resolved by `tasks/gpu_flavor.yml` (`vllm_gpu_exporter_flavor: auto` probes `nvidia-smi`): **dcgm-exporter** (port 9400) for datacenter GPUs (Tesla, A/H/L/B series), or the **nvidia-smi exporter** (`utkuozdemir/nvidia_gpu_exporter:latest-nvml`, port 9835) for GeForce cards — DCGM does not support consumer GPUs. Set `vllm_gpu_exporter_flavor: none` to skip the GPU exporter (the stub rig does).

Ports already served by a foreign exporter (e.g. the `prometheus-node-exporter` rpm on 9100, the `nvidia-gpu-exporter` rpm on 9835) are detected and **reused** instead of double-deployed — the collector scrapes whatever serves the port.

Deployment: podman nodes get a systemd **quadlet** per exporter (`containers.podman.podman_container` `state: quadlet` → `/etc/containers/systemd/<name>.container`, units `<name>.service`); docker nodes use `community.docker.docker_container` with `restart_policy: unless-stopped` (runtime dep: `python3-requests`, installed by the `prerequisites` role on RedHat-family hosts). The exporter ports open in the `vllm-cluster` firewalld zone regardless of deploy/reuse.

The role also templates a **`vLLM Remote Nodes` Grafana dashboard** (`vllm-remote-nodes`) into `grafana/provisioning/dashboards/json/vllm-nodes-dashboard.json` on the controller — the same repo-render pattern as the LiteLLM snippet. GPU panels query both metric families (DCGM and nvidia_smi); only the present one returns data.

Consumption on the stack host: the role injects one scrape job per node/exporter into `otel/config.yaml` between `vllm_compute` markers (targets rendered from the live inventory — no hardcoded IPs; adding a node and re-running extends the scrape set). The OTel collector scrapes them over the management subnet; it needs the non-internal `frontend` network (`backend` is `internal: true`), which `docker-compose.yml` attaches. After a run, restart the monitoring containers:

```bash
docker compose --profile monitoring restart otel-collector lgtm
```

FIPS note: the exporters are Go binaries and run unmasked on a `fips=1` host. For the vLLM image itself, two options: upstream `vllm/vllm-openai` (Ubuntu; needs `vllm_mask_fips_flag: true` on a FIPS host) or **Red Hat's RHEL9 vLLM images** (`registry.redhat.io/rhoai/odh-vllm-cuda-rhel9`, `registry.redhat.io/rhaii-fast/vllm-omni-cuda-rhel9`, `registry.redhat.io/rhaiis/vllm-cuda-rhel9`) — RHEL9 images ship the FIPS-capable OpenSSL, so `vllm_mask_fips_flag: false` with them. They need Customer Portal credentials: export `REGISTRY_REDHAT_IO_USER`/`REGISTRY_REDHAT_IO_PASS` on the controller and the role logs the node in before the pull (an existing `podman login` on the node also works).

---

## Model evaluation (the `vllm_eval` role)

`ansible/playbooks/eval.yml` runs the reproducible test suite used for the Qwen3-Coder-30B vs Gemma-4-26B-A4B comparison (2026-11-03), so future models are measured the same way — through the LiteLLM gateway, so the full Open WebUI path is what's tested:

1. **KV probe** — greps the vLLM engine's own startup line (`GPU KV cache size: N tokens, Maximum concurrency ...`) for the *measured* capacity, not the config-math estimate.
2. **Long prompt** — `vllm_eval_long_prompt_tokens` of filler context plus a question (default 30K; the original production failure was 16,385 tokens against a 16,384 window).
3. **Coding** — short code-gen prompt, scored on producing a `def` reading JSON.
4. **Vision** — a generated red/white split PNG, "which color is the left half". Auto-skips (not fails) when the model rejects image content.
5. **Concurrency** — `vllm_eval_concurrency` parallel requests, verifying the KV capacity line holds in practice.

Results land in `ansible/tests/eval-results/<model>-<timestamp>.json` (gitignored) so runs are comparable across models over time. The play fails if any test fails.

```bash
# from ansible/tests/vllm-remote/ (deployed node)
./run.sh eval
# or target any registered gateway model:
./run.sh eval -e vllm_eval_model=gemini-3.8-flash
```

---

## Testing the role locally

`ansible/tests/vllm-rig/` runs the real role against a disposable OrbStack Fedora VM (no NVIDIA hardware — GPU pieces are stubbed). From `ansible/`:

```bash
bash tests/vllm-rig/run.sh setup      # stubs + CPU stand-in image
bash tests/vllm-rig/run.sh inference  # full play, two fake nodes
bash tests/vllm-rig/run.sh check     # rerun; must report zero changes
```

The rig exercises: prerequisites fail-fast, Ray cluster formation, `/health` waits, firewall zone idempotency, multi-model flux deploy, aux placement (list-form and `all` host keys; both rig hosts alias one VM, so the rig only exercises placements that resolve to an identical slice on every host), model-removal cleanup, and idempotency.

`ansible/tests/vllm-remote/` runs the role against a **real remote GPU node** (Fedora 44, RTX 4090, podman), reached only from the photon bastion, so **ansible runs on photon**. It exercises the podman path end to end: `prerequisites_install_missing: true` installs the NVIDIA container toolkit and generates the CDI spec, rootful podman storage moves under the node user's home (`vllm_podman_storage_root`), and the node serves the deployed model standalone on port 8000. The node address, user, node-local paths, firewall CIDR, and controller address are site-specific: they live in `tests/vllm-remote/inventory.local.yml` and `tests/vllm-remote/group_vars/vllm_compute/vars.local.yml` (both gitignored; the committed `inventory.yml` / `vars.yml` ship placeholders and document the keys). From the repo root (on the Mac):

```bash
bash ansible/tests/vllm-remote/run.sh inference  # sync to photon, run the playbook there
bash ansible/tests/vllm-remote/run.sh check     # rerun; must report zero changes
```

The script itself runs on the Mac and shells into photon via an OrbStack VM (which holds the bastion key); photon then SSHes to the node user with passwordless sudo. Connection values for the script live in `run.local.env` (gitignored; see `run.local.env.example`).
