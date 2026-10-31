# vLLM Inference

How all vLLM inferencing is configured and deployed in this stack: the architecture, the parallelism modes, the variables that control them, and a complete setup walkthrough.

Everything is deployed by the **`vllm_compute` Ansible role** (`ansible/roles/vllm_compute/`) via `ansible-playbook playbooks/inference.yml`. It runs on the `vllm_compute` inventory group after the global `prerequisites` check role (Docker, `docker compose`, and — for this group — the NVIDIA driver via `prerequisites_check_nvidia: true`).

There are two independent serving layers in the role:

1. **Primary model** — one chat/completion model, optionally sharded across the cluster (Ray pipeline or tensor parallelism, or standalone replicas).
2. **Flux image-generation servers** — a list of independent single-model `vllm serve` instances, one per configured model, running on every node.

Both render into `litellm/vllm-models.yaml` (see [Register with LiteLLM](#register-with-litellm)).

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
   │ shared HF cache: /mnt/data1/huggingface                     │
   └──────────────────────────────────────────────────────────────┘
```

Key points:

- **The compute nodes are separate hosts from the Open WebUI stack.** Containers run under **systemd** (`vllm-ray`, `vllm-serve`, `vllm-flux-<name>`), not docker compose. Each unit runs a generated launch script from `vllm_install_dir` (default `/opt/vllm`) that does `docker run` with the rendered flags.
- **All servers share one Hugging Face cache** (`vllm_hf_cache_dir`, default `/mnt/data1/huggingface`), so each node downloads a model once even when the primary model and several flux servers need weights.
- **The unit files handle container lifecycle**: `--rm` does not fire when systemd stops the `docker run` client, so every unit has `ExecStop`/`ExecStopPost` that removes the container explicitly, plus `KillMode=control-group`. This makes `systemctl restart` clean (no leftover container holding the name).
- **Requests come through LiteLLM**, which authenticates with `VLLM_API_KEY`. Ray traffic itself is unauthenticated and can execute arbitrary code — the `vllm-cluster` firewalld zone restricts it to the compute CIDR (`vllm_firewall_zone_source`).

### Files in the role

| File | Purpose |
|---|---|
| `defaults/main.yml` | Every variable with a default + full explanations. The reference for what each knob does. |
| `tasks/main.yml` | Task flow: validate config → detect GPUs → resolve topology → firewall → services → flux → LiteLLM render (in that order). |
| `tasks/services.yml` | Primary-model scripts, units, startup, health waits. |
| `tasks/flux.yml` | Flux list merge/validation, per-model scripts and units, cleanup of removed models, health waits. |
| `tasks/firewall.yml` | Creates the `vllm-cluster` firewalld zone, restricts to `vllm_firewall_zone_source`, opens the pinned Ray + API + flux ports. |
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

## Setup

### 1. Prepare each node (one-time, outside the role)

Per Fedora node — the role checks these but deliberately does not install them (see the fail messages in `roles/prerequisites/tasks/main.yml`):

```bash
dnf install docker docker-compose nvidia-container-toolkit akmod-nvidia xorg-x11-drv-nvidia-cuda
systemctl enable --now docker
nvidia-ctk runtime configure --runtime=docker && systemctl restart docker
```

- `docker-compose` is the compose plugin package on Fedora (Photon: `tdnf install docker docker-compose`).
- The NVIDIA driver (`akmod-nvidia` builds the kmod on next boot; `xorg-x11-drv-nvidia-cuda` adds CUDA).
- `nvidia-ctk runtime configure` registers the `nvidia` runtime Docker uses for `--gpus all`.
- Put a large data volume at `/mnt/data1` — the HF cache (`vllm_hf_cache_dir`) lives there. Pre-download big models with `huggingface-cli download <repo>` so first serve does not download on every node.

### 2. Inventory

`ansible/inventory/hosts.yml` — mark **exactly one** host `vllm_ray_head: true` (ray mode):

```yaml
vllm_compute:
  hosts:
    vllm-node-01:
      ansible_host: 10.0.105.21
      ansible_user: root
      vllm_ray_head: true
    vllm-node-02:
      ansible_host: 10.0.105.22
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

What a run does, per node: validates the config → detects GPUs → opens the `vllm-cluster` firewalld zone (Ray ports 6379, 8265, 13377–13379, 14000–15000 + API 8000 + flux ports) restricted to the compute CIDR → deploys the launch scripts and systemd units → starts `vllm-ray`, waits for every node to register → starts `vllm-serve`, waits for `/health` → deploys/starts each flux server, waits for each `/health`.

Services after a successful run:

```bash
systemctl status vllm-ray vllm-serve vllm-flux-flux vllm-flux-flux-schnell
curl http://127.0.0.1:8000/health
```

### 5. Register with LiteLLM

The run renders `litellm/vllm-models.yaml` in the repo: one `model_list` entry for the primary model (single cluster endpoint in ray mode; one per node in standalone — LiteLLM load balances) plus one `mode: image_generation` entry per flux model.

Merge the snippet into `model_list` in `litellm/config.yaml` on the stack host and reload:

```bash
# on the photon host, in the deployed tree
docker compose restart litellm
```

The models then appear in Open WebUI's model dropdown.

### Operations

| Change | How |
|---|---|
| Switch the primary model | Edit `vllm_model` → re-run the playbook → merge the regenerated snippet |
| Change topology | Set `vllm_deployment_mode` / `vllm_cross_node_tensor_parallel` → re-run |
| Add/remove an image model | Edit `vllm_flux_models` → re-run; removal cleans up automatically |
| Restart everything on a node | `systemctl restart vllm-ray vllm-serve 'vllm-flux-*'` |
| Logs | `journalctl -u vllm-serve -f` (or the flux unit); Ray dashboard on the head: `http://<head>:8265` |
| Tear flux down | `vllm_flux_enabled: false` → re-run |

### Security notes

- Ray traffic is unauthenticated and can execute arbitrary code: keep `vllm_firewall_zone_source` limited to the compute segment and always set `VLLM_API_KEY`.
- Model weights must exist at the same path or HF id on every node; pin the same vLLM image version cluster-wide.

---

## Testing the role locally

`ansible/tests/vllm-rig/` runs the real role against a disposable OrbStack Fedora VM (no NVIDIA hardware — GPU pieces are stubbed). From `ansible/`:

```bash
bash tests/vllm-rig/run.sh setup      # stubs + CPU stand-in image
bash tests/vllm-rig/run.sh inference  # full play, two fake nodes
bash tests/vllm-rig/run.sh check     # rerun; must report zero changes
```

The rig exercises: prerequisites fail-fast, Ray cluster formation, `/health` waits, firewall zone idempotency, multi-model flux deploy, model-removal cleanup, and idempotency.
