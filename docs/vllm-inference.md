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
| Docker Engine + compose plugin **or podman**, NVIDIA driver + container toolkit | container runtime; checked by `prerequisites` (installed per-node by hand, or by the role with `prerequisites_install_missing: true`) |

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
| `tasks/services.yml` | Primary-model scripts, units, startup, health waits. |
| `tasks/flux.yml` | Flux list merge/validation, per-model scripts and units, cleanup of removed models, health waits. |
| `tasks/aux.yml` | Aux list merge/validation with per-node placement, per-model scripts and units, cleanup of removed or re-hosted models, health waits. |
| `tasks/firewall.yml` | Creates the `vllm-cluster` firewalld zone via the `ansible.posix.firewalld` module, restricts to `vllm_firewall_zone_source`, admits the `ssh` service (source binding routes ALL subnet traffic into the zone — without it management SSH is cut off), opens the pinned Ray + API + flux + aux ports. |
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

Behavior:

- **Placement** is evaluated per node: an entry deploys only on hosts its `hosts` key names. Removing a host from the list (or the whole entry) on the next run stops + disables that node's unit and deletes the unit file and launch script. `vllm_aux_enabled: false` tears every aux server down everywhere.
- **Ports must be unique** across the aux, flux, and primary API ports; the role asserts this.
- **GPU budget**: same rule as flux — the role validates each entry's TP against the node's GPUs but not the total; plan co-running servers (aux + flux + primary) to fit the node.
- **LiteLLM**: each (node, entry) placement renders one `model_list` entry in `litellm/vllm-models.yaml`; a model placed on several nodes appears several times under the same `model_name`, and LiteLLM load balances those replicas.

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
dnf install podman nvidia-container-toolkit akmod-nvidia xorg-x11-drv-nvidia-cuda
nvidia-ctk cdi generate --output=/var/run/cdi/nvidia.yaml
```

- `docker-compose` is the compose plugin package on Fedora (Photon: `tdnf install docker docker-compose`).
- The NVIDIA driver (`akmod-nvidia` builds the kmod on next boot; `xorg-x11-drv-nvidia-cuda` adds CUDA).
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

The same run **prunes stale vLLM models** from the gateway's model db: any registered model whose `api_base` points at a `vllm_compute` node (`http://<ip>:<port>/v1`) but which this run no longer serves — a retired `vllm_model`, a removed aux entry, a changed `max_model_len`, a **non-number stored limit** (legacy string-typed registration), or (primary only) a changed `vllm_adaptive_quality_tier`/`vllm_adaptive_strengths` — is deleted via `POST /model/delete`. Without the prune, removed models linger in every client's model list forever (that is how `Qwen2.5-7B-Instruct` stuck around after the primary was switched). Scope is strict: only db models with a vLLM `api_base` are touched; config-declared models (gemini, bedrock, `auto`/`adaptive`, `voice-assistant`) have no `api_base` and are never deleted.

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
| Change topology | Set `vllm_deployment_mode` / `vllm_cross_node_tensor_parallel` → re-run |
| Add/remove an image model | Edit `vllm_flux_models` → re-run; removal cleans up automatically |
| Add/remove/placement an aux model | Edit `vllm_aux_models` (add/remove hosts in its `hosts` key) → re-run; cleanup is automatic |
| Restart everything on a node | `systemctl restart vllm-ray vllm-serve 'vllm-flux-*' 'vllm-aux-*' 'vllm-*-exporter-container'` |
| Logs | `journalctl -u vllm-serve -f` (or the flux/aux unit); Ray dashboard on the head: `http://<head>:8265` |
| Tear flux down | `vllm_flux_enabled: false` → re-run |
| Tear aux down | `vllm_aux_enabled: false` → re-run |
| Monitoring off | `vllm_monitoring_enabled: false` → re-run (exporter quadlets stay until removed by hand) |

### Security notes

- Ray traffic is unauthenticated and can execute arbitrary code: keep `vllm_firewall_zone_source` limited to the compute segment and always set `VLLM_API_KEY`.
- The zone source binding is greedy: firewalld routes every packet from that subnet into `vllm-cluster`, so the role admits `ssh` there. If you remove that task or swap the zone's source, management hosts in the subnet lose SSH to the node on the next reload — recover from the node's console.
- A first run pulls the model from Hugging Face before `/health` passes; the wait is `vllm_health_wait_retries` × 10s (default 60). Raise it in group_vars for slow links or large models, or pre-download with `huggingface-cli download`.
- Model weights must exist at the same path or HF id on every node; pin the same vLLM image version cluster-wide.

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
