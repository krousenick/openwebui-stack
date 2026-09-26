#!/bin/bash
# =============================================================================
# Setup llama.cpp Edge Nodes for Inference
# =============================================================================
# This script helps configure local llama.cpp inference nodes running on
# NVIDIA GPUs with Fedora Linux. It generates models and configuration for
# integrating with the Open WebUI stack via LiteLLM.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Cross-platform sed in-place editing (macOS BSD sed vs GNU sed)
sed_inplace() {
	if [[ $OSTYPE == "darwin"* ]]; then
		sed -i '' "$@"
	else
		sed -i "$@"
	fi
}

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}  llama.cpp Edge Node Setup${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Check prerequisites
echo -e "${YELLOW}Checking prerequisites...${NC}"

# Check if we're on Fedora Linux (for edge nodes)
if [[ -f /etc/os-release ]]; then
	source /etc/os-release
	if [[ "$ID" != "fedora" ]]; then
		echo -e "${YELLOW}Note: This script is designed for Fedora Linux edge nodes.${NC}"
		echo -e "${YELLOW}You can still configure remote nodes from this machine.${NC}"
	fi
fi

# Check for NVIDIA GPU
if command -v nvidia-smi &>/dev/null; then
	echo -e "${GREEN}✓ NVIDIA GPU detected:${NC}"
	nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1
else
	echo -e "${YELLOW}⚠ nvidia-smi not found. If this is an edge node, ensure NVIDIA drivers are installed.${NC}"
fi

echo ""
echo -e "${YELLOW}Step 1: Edge Node Configuration${NC}"
echo "This script will configure edge nodes for llama.cpp inference."
echo ""

# Prompt for number of edge nodes
read -rp "Number of edge nodes to configure (default: 1): " NUM_NODES
NUM_NODES=${NUM_NODES:-1}

EDGE_NODES=()
declare -A NODE_MODELS
declare -A NODE_PORTS

for ((i = 1; i <= NUM_NODES; i++)); do
	echo ""
	echo -e "${BLUE}Edge Node $i Configuration:${NC}"

	read -rp "  Hostname or IP address: " NODE_HOST
	NODE_HOST=${NODE_HOST:-localhost}

	read -rp "  Port (default: 8080): " NODE_PORT
	NODE_PORT=${NODE_PORT:-8080}

	read -rp "  Model name (e.g., llama-3.1-8b-instruct): " MODEL_NAME
	MODEL_NAME=${MODEL_NAME:-llama-3.1-8b-instruct}

	EDGE_NODES+=("$NODE_HOST:$NODE_PORT")
	NODE_MODELS["$NODE_HOST:$NODE_PORT"]="$MODEL_NAME"
	NODE_PORTS["$NODE_HOST"]="$NODE_PORT"

	echo -e "${GREEN}  ✓ Added edge node: $NODE_HOST:$NODE_PORT ($MODEL_NAME)${NC}"
done

echo ""
echo -e "${YELLOW}Step 2: Model Configuration${NC}"
echo "Select models available on edge nodes:"
echo "  1) Llama 3.1 8B Instruct"
echo "  2) Llama 3.1 70B Instruct"
echo "  3) Llama 3.2 1B Instruct"
echo "  4) Mistral 7B Instruct"
echo "  5) Qwen 2.5 7B Instruct"
echo "  6) Custom model"
echo ""

DEFAULT_MODELS=""
for ((i = 1; i <= NUM_NODES; i++)); do
	NODE="${EDGE_NODES[$((i - 1))]}"
	MODEL="${NODE_MODELS[$NODE]}"
	DEFAULT_MODELS="$DEFAULT_MODELS$MODEL,"
done
DEFAULT_MODELS="${DEFAULT_MODELS%,}"

read -rp "Enter model names (comma-separated, default from above): " MODELS_INPUT
MODELS=${MODELS_INPUT:-$DEFAULT_MODELS}

# Convert to array
IFS=',' read -ra MODEL_ARRAY <<<"$MODELS"

echo ""
echo -e "${YELLOW}Step 3: GPU Configuration${NC}"
echo "Configure GPU layers for inference:"
echo ""

read -rp "GPU layers to offload (default: 35, use -1 for all): " GPU_LAYERS
GPU_LAYERS=${GPU_LAYERS:--1}

read -rp "Context window size (default: 4096): " CTX_SIZE
CTX_SIZE=${CTX_SIZE:-4096}

read -rp "Number of parallel slots (default: 8): " N_SLOTS
N_SLOTS=${N_SLOTS:-8}

echo ""
echo -e "${YELLOW}Step 4: Systemd Service Configuration${NC}"
echo "Generate systemd service files for edge nodes:"
echo ""

read -rp "Model directory (default: /opt/models): " MODEL_DIR
MODEL_DIR=${MODEL_DIR:-/opt/models}

read -rp "llama.cpp binary path (default: /usr/local/bin/llama-server): " LLAMA_BIN
LLAMA_BIN=${LLAMA_BIN:-/usr/local/bin/llama-server}

echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "Generating configuration files..."
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Create systemd service file template
SERVICE_FILE="$PROJECT_ROOT/edge-nodes/llama-server.service"
mkdir -p "$PROJECT_ROOT/edge-nodes"

cat >"$SERVICE_FILE" <<'EOF'
[Unit]
Description=llama.cpp Inference Server
After=network.target nvidia-driver.service
Requires=nvidia-driver.service

[Service]
Type=simple
User=nobody
Group=nobody
WorkingDirectory=/opt/models
ExecStart=/usr/local/bin/llama-server \
    --host 0.0.0.0 \
    --port 8080 \
    --model /opt/models/MODEL_FILE \
    --gpu-layers GPU_LAYERS \
    --ctx-size CTX_SIZE \
    --slots N_SLOTS \
    --cont-batching \
    --metrics
Restart=always
RestartSec=10
TimeoutStopSec=30

# Resource limits
LimitNOFILE=65535
CPUQuota=100%
MemoryMax=90%

# Security
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

echo -e "${GREEN}✓ Created systemd service template: $SERVICE_FILE${NC}"

# Create edge node specific service files
for NODE in "${EDGE_NODES[@]}"; do
	NODE_HOST="${NODE%%:*}"
	NODE_PORT="${NODE##*:}"
	MODEL="${NODE_MODELS[$NODE]}"

	SERVICE_NAME="llama-server-${NODE_HOST}.service"
	SERVICE_FILE="$PROJECT_ROOT/edge-nodes/$SERVICE_NAME"

	sed "s|MODEL_FILE|$MODEL.gguf|g; s|GPU_LAYERS|$GPU_LAYERS|g; s|CTX_SIZE|$CTX_SIZE|g; s|N_SLOTS|$N_SLOTS|g; s|8080|$NODE_PORT|g" \
		"$PROJECT_ROOT/edge-nodes/llama-server.service" >"$SERVICE_FILE"

	echo -e "${GREEN}  ✓ Created: $SERVICE_NAME${NC}"
done

# Create setup script for edge nodes
SETUP_SCRIPT="$PROJECT_ROOT/edge-nodes/setup-edge-node.sh"

cat >"$SETUP_SCRIPT" <<'EOF'
#!/bin/bash
# =============================================================================
# Edge Node Installation Script
# =============================================================================
# Run this script on each edge node (Fedora Linux with NVIDIA GPU)
# =============================================================================

set -euo pipefail

echo "Installing llama.cpp on edge node..."

# Install dependencies
sudo dnf install -y git cmake gcc-c++ cuda nvidia-driver

# Clone llama.cpp
if [[ ! -d /opt/llama.cpp ]]; then
    cd /opt
    sudo git clone https://github.com/ggml-org/llama.cpp.git
    cd llama.cpp
    sudo cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=native
    sudo cmake --build build --config Release -j$(nproc)
    sudo cmake --install build --prefix=/usr/local
fi

# Create model directory
sudo mkdir -p /opt/models
sudo chown -R nobody:nobody /opt/models

# Install systemd service
sudo cp llama-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable llama-server
sudo systemctl start llama-server

echo "Edge node setup complete!"
echo "Download models to /opt/models/"
EOF

chmod +x "$SETUP_SCRIPT"
echo -e "${GREEN}✓ Created edge node setup script: $SETUP_SCRIPT${NC}"

# Generate model environment variable for LiteLLM
EDGE_NODES_VAR=$(
	IFS=','
	echo "${EDGE_NODES[*]}"
)
echo ""
echo -e "${YELLOW}Step 5: Update Open WebUI Stack Configuration${NC}"

# Update .env file with edge nodes
if [ -f "$PROJECT_ROOT/.env" ]; then
	echo -e "${BLUE}Updating .env with edge nodes...${NC}"

	# Add LLAMA_CPP_EDGE_NODES if it doesn't exist
	if ! grep -q "^LLAMA_CPP_EDGE_NODES=" "$PROJECT_ROOT/.env" 2>/dev/null; then
		echo -e "\n# -----------------------------------------------------------------------------\n# llama.cpp Edge Nodes Configuration\n# -----------------------------------------------------------------------------\nLLAMA_CPP_EDGE_NODES=$EDGE_NODES_VAR" >>"$PROJECT_ROOT/.env"
		echo -e "${GREEN}✓ Added LLAMA_CPP_EDGE_NODES to .env${NC}"
	else
		sed_inplace "s|^LLAMA_CPP_EDGE_NODES=.*|LLAMA_CPP_EDGE_NODES=$EDGE_NODES_VAR|" "$PROJECT_ROOT/.env"
		echo -e "${GREEN}✓ Updated LLAMA_CPP_EDGE_NODES in .env${NC}"
	fi
else
	echo -e "${YELLOW}.env file not found. Add this to your .env file:${NC}"
	echo ""
	echo "LLAMA_CPP_EDGE_NODES=$EDGE_NODES_VAR"
	echo ""
fi

# Generate LiteLLM model config snippet
echo ""
echo -e "${YELLOW}LiteLLM Model Configuration:${NC}"
echo "Add the following to litellm/config.yaml under model_list:"
echo ""
echo ""

for NODE in "${EDGE_NODES[@]}"; do
	NODE_HOST="${NODE%%:*}"
	NODE_PORT="${NODE##*:}"
	MODEL="${NODE_MODELS[$NODE]}"

	cat <<MODELEOF
  - model_name: ${MODEL}
    litellm_params:
      model: openai/${MODEL}
      api_base: http://${NODE_HOST}:${NODE_PORT}/v1
    model_info:
      description: "${MODEL} on ${NODE_HOST}"

MODELEOF
done

echo ""

# Create Prometheus monitoring config
MONITORING_SCRIPT="$PROJECT_ROOT/edge-nodes/monitor-edge-nodes.sh"

cat >"$MONITORING_SCRIPT" <<'EOF'
#!/bin/bash
# =============================================================================
# Monitor llama.cpp Edge Nodes Health
# =============================================================================

EDGE_NODES="${LLAMA_CPP_EDGE_NODES:-}"
IFS=',' read -ra NODES <<< "$EDGE_NODES"

for NODE in "${NODES[@]}"; do
    HOST="${NODE%%:*}"
    PORT="${NODE##*:}"
    
    if curl -sf "http://${HOST}:${PORT}/health" > /dev/null 2>&1; then
        echo "✓ ${HOST}:${PORT} - healthy"
    else
        echo "✗ ${HOST}:${PORT} - unhealthy"
    fi
done
EOF

chmod +x "$MONITORING_SCRIPT"
echo -e "${GREEN}✓ Created monitoring script: $MONITORING_SCRIPT${NC}"

# Summary
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Edge Node Setup Complete!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "Summary:"
echo "  • Edge nodes configured: ${#EDGE_NODES[@]}"
for NODE in "${EDGE_NODES[@]}"; do
	echo "    - $NODE (${NODE_MODELS[$NODE]})"
done
echo ""
echo "Next steps:"
echo "  1. Copy edge-nodes/setup-edge-node.sh to each edge node"
echo "  2. Run setup-edge-node.sh on each edge node (requires sudo)"
echo "  3. Download GGUF models to /opt/models/ on each edge node"
echo "  4. Add the model config snippet above to litellm/config.yaml"
echo "  5. Restart the stack: docker compose restart litellm"
echo ""
echo "For monitoring, run: ./edge-nodes/monitor-edge-nodes.sh"
echo ""
