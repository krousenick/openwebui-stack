#!/bin/bash
# =============================================================================
# Open WebUI Stack - Setup Script
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

# Error handling
error_exit() {
	echo -e "${RED}ERROR: $1${NC}" >&2
	exit 1
}

# Cross-platform sed in-place editing (macOS BSD sed vs GNU sed)
sed_inplace() {
	if [[ $OSTYPE == "darwin"* ]]; then
		sed -i '' "$@"
	else
		sed -i "$@"
	fi
}

# Check directory exists and has correct permissions
check_directory() {
	local dir="$1"
	local desc="$2"
	local required_perms="${3:-755}"

	if [[ ! -d "$dir" ]]; then
		echo -e "${YELLOW}Creating directory: $dir${NC}"
		mkdir -p "$dir" || error_exit "Failed to create $desc directory: $dir"
	fi

	# Ensure directory is executable (can be traversed)
	chmod "$required_perms" "$dir" 2>/dev/null || true

	if [[ ! -r "$dir" ]]; then
		error_exit "$desc directory not readable: $dir"
	fi

	if [[ ! -x "$dir" ]]; then
		error_exit "$desc directory not executable (traversable): $dir"
	fi

	return 0
}

# Check file has correct permissions
check_script_permissions() {
	local file="$1"

	if [[ ! -f "$file" ]]; then
		return 1
	fi

	# Make sure script is executable
	if [[ ! -x "$file" ]]; then
		chmod +x "$file" || echo -e "${YELLOW}Warning: Could not make $file executable${NC}"
	fi

	return 0
}

# Verify required directories and permissions
verify_directories() {
	echo -e "${YELLOW}Verifying directory structure and permissions...${NC}"

	# Required directories to check
	local dirs=(
		"$PROJECT_ROOT/traefik"
		"$PROJECT_ROOT/traefik/dynamic"
		"$PROJECT_ROOT/init-db"
		"$PROJECT_ROOT/certs"
		"$PROJECT_ROOT/grafana"
		"$PROJECT_ROOT/litellm"
		"$PROJECT_ROOT/keycloak"
		"$PROJECT_ROOT/scripts"
	)

	local errors=0
	for dir in "${dirs[@]}"; do
		if [[ -d "$dir" ]]; then
			check_directory "$dir" "$(basename "$dir")" 755
		else
			echo -e "${YELLOW}  Directory will be created: $dir${NC}"
		fi
	done

	# Check init-db scripts are executable
	if [[ -d "$PROJECT_ROOT/init-db" ]]; then
		for script in "$PROJECT_ROOT/init-db"/*.sh; do
			if [[ -f "$script" ]]; then
				check_script_permissions "$script"
				echo -e "${GREEN}  ✓ $(basename "$script") is executable${NC}"
			fi
		done
	fi

	# Check scripts are executable
	for script in "$PROJECT_ROOT/scripts"/*.sh "$PROJECT_ROOT/scripts"/*.py; do
		if [[ -f "$script" ]]; then
			check_script_permissions "$script"
		fi
	done

	# Ensure init-db directory permissions (postgres needs to read)
	if [[ -d "$PROJECT_ROOT/init-db" ]]; then
		chmod 755 "$PROJECT_ROOT/init-db"
		chmod 644 "$PROJECT_ROOT/init-db"/*.sh 2>/dev/null || true
		chmod 755 "$PROJECT_ROOT/init-db"/*.sh 2>/dev/null || true
		echo -e "${GREEN}  ✓ init-db scripts have correct permissions${NC}"
	fi

	return 0
}

# Helper function to generate htpasswd hash (apr1 format)
generate_htpasswd() {
	local password="$1"
	openssl passwd -apr1 "$password"
}

# Helper function to generate password
generate_password() {
	openssl rand -base64 32 | tr -dc 'a-zA-Z0-9' | head -c 32
}

# Helper function to update /etc/hosts with required FQDNs
update_hosts_file() {
	local domain="$1"

	# Skip if domain is localhost
	[[ "$domain" == "localhost" ]] && return

	local hosts_to_add=(
		"127.0.0.1 traefik.${domain}"
		"127.0.0.1 chat.${domain}"
		"127.0.0.1 litellm.${domain}"
		"127.0.0.1 auth.${domain}"
		"127.0.0.1 grafana.${domain}"
	)

	# Check if we can write to /etc/hosts
	local hosts_file="/etc/hosts"
	local needs_sudo=false

	for entry in "${hosts_to_add[@]}"; do
		local hostname="${entry##* }"
		if ! grep -q "$hostname" "$hosts_file" 2>/dev/null; then
			if [[ ! -w "$hosts_file" ]]; then
				needs_sudo=true
			fi
			break
		fi
	done

	# Add entries
	for entry in "${hosts_to_add[@]}"; do
		local hostname="${entry##* }"
		if ! grep -q "$hostname" "$hosts_file" 2>/dev/null; then
			if [[ "$needs_sudo" == "true" ]]; then
				echo "$entry" | sudo tee -a "$hosts_file" >/dev/null 2>&1
			else
				echo "$entry" >>"$hosts_file"
			fi
			echo -e "  ${GREEN}Added${NC} $hostname to /etc/hosts"
		fi
	done
}

# Helper function for yes/no prompt (defaults to yes)
prompt_yes_no() {
	local prompt="$1"
	local response
	read -rp "$prompt (Y/n): " response
	response=${response:-y}
	case "$response" in
	[yY][eE][sS] | [yY]) return 0 ;;
	[nN][oO] | [nN]) return 1 ;;
	*) return 0 ;; # Default to yes on invalid input
	esac
}

# Helper function for yes/no prompt (defaults to no)
prompt_yes_no_default_no() {
	local prompt="$1"
	local response
	read -rp "$prompt (y/N): " response
	response=${response:-n}
	case "$response" in
	[yY][eE][sS] | [yY]) return 0 ;;
	[nN][oO] | [nN]) return 1 ;;
	*) return 1 ;; # Default to no on invalid input
	esac
}

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}  Open WebUI Stack - Setup${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Verify directories and permissions
verify_directories
echo ""

# 1. Initialize .env file
echo -e "${YELLOW}Step 1: Environment File${NC}"
if [ -f "$PROJECT_ROOT/.env" ]; then
	echo -e "${GREEN}✓ .env file already exists${NC}"
	if prompt_yes_no_default_no "Override existing .env file?"; then
		INIT_ENV=true
	else
		INIT_ENV=false
	fi
else
	if prompt_yes_no "Initialize .env file?"; then
		INIT_ENV=true
	else
		INIT_ENV=false
	fi
fi

echo ""

# 2. Domain
echo -e "${YELLOW}Step 2: Domain Configuration${NC}"

# Check if DOMAIN is already set in .env
EXISTING_DOMAIN=""
if [[ -f "$PROJECT_ROOT/.env" ]]; then
	EXISTING_DOMAIN=$(grep "^DOMAIN=" "$PROJECT_ROOT/.env" 2>/dev/null | cut -d'=' -f2 | tr -d ' "' || echo "")
fi

if [[ -n "$EXISTING_DOMAIN" && "$EXISTING_DOMAIN" != "localhost" ]]; then
	echo -e "${GREEN}Found existing domain: $EXISTING_DOMAIN${NC}"
	read -rp "Keep existing domain? (Y/n): " KEEP_DOMAIN
	KEEP_DOMAIN=${KEEP_DOMAIN:-y}
	case "$KEEP_DOMAIN" in
	[yY][eE][sS] | [yY])
		DOMAIN="$EXISTING_DOMAIN"
		echo -e "${GREEN}✓ Using existing domain: $DOMAIN${NC}"
		;;
	*)
		read -rp "Enter new domain (default: localhost): " DOMAIN
		DOMAIN=${DOMAIN:-localhost}
		;;
	esac
else
	read -rp "Enter your domain (default: localhost): " DOMAIN
	DOMAIN=${DOMAIN:-localhost}
	echo -e "${GREEN}✓ Domain: $DOMAIN${NC}"
fi

# Update /etc/hosts with required FQDNs
if [[ "$DOMAIN" != "localhost" ]]; then
	echo -e "\n${YELLOW}Updating /etc/hosts...${NC}"
	update_hosts_file "$DOMAIN"
fi
echo ""

# 3. Generate passwords
GENERATE_PASSWORDS=false
if [ "$INIT_ENV" = true ]; then
	echo -e "${YELLOW}Step 3: Generate Passwords${NC}"
	if prompt_yes_no "Generate secure passwords for .env?"; then
		GENERATE_PASSWORDS=true
	fi
	echo ""
fi

# 4. SSL Certificates (optional)
echo -e "${YELLOW}Step 4: SSL Certificates${NC}"
echo "  Select SSL certificate option:"
echo "  1) Use existing wildcard certificates (cert/key files)"
echo "  2) Generate persistent self-signed certificates"
echo "  3) Use Traefik default (not recommended - changes on restart)"
echo ""
echo -n "Enter choice [1]: "
read -r CERT_OPTION
CERT_OPTION=${CERT_OPTION:-1}

GENERATE_CERTS=false
USE_EXISTING_CERTS=false

case "$CERT_OPTION" in
1)
	# Check if certs directory exists with files
	if [ -d "$PROJECT_ROOT/certs" ]; then
		# Find the first .crt and .key file
		CERT_FILE=$(ls -1 "$PROJECT_ROOT/certs/"*.crt 2>/dev/null | head -1 | xargs basename 2>/dev/null || echo "")
		KEY_FILE=$(ls -1 "$PROJECT_ROOT/certs/"*.key 2>/dev/null | head -1 | xargs basename 2>/dev/null || echo "")

		if [ -n "$CERT_FILE" ] && [ -n "$KEY_FILE" ]; then
			echo -e "${GREEN}✓ Found existing certificates in certs/${NC}"
			echo "  Certificate: $CERT_FILE"
			echo "  Private Key: $KEY_FILE"

			# Update .env with the actual file paths
			sed_inplace "s|^SSL_CERT_FILE=.*|SSL_CERT_FILE=certs/$CERT_FILE|" "$PROJECT_ROOT/.env"
			sed_inplace "s|^SSL_KEY_FILE=.*|SSL_KEY_FILE=certs/$KEY_FILE|" "$PROJECT_ROOT/.env"

			# Generate Traefik config for existing certs
			DYNAMIC_CERTS_FILE="$PROJECT_ROOT/traefik/dynamic/certs.yml"
			cat >"$DYNAMIC_CERTS_FILE" <<EOF
# =============================================================================
# Traefik Dynamic Configuration - Custom Certificates
# =============================================================================

tls:
  certificates:
    - certFile: /etc/traefik/certs/$CERT_FILE
      keyFile: /etc/traefik/certs/$KEY_FILE

  stores:
    default:
      defaultCertificate:
        certFile: /etc/traefik/certs/$CERT_FILE
        keyFile: /etc/traefik/certs/$KEY_FILE
EOF
			echo -e "${GREEN}✓ Created Traefik certs config${NC}"
		else
			echo -e "${YELLOW}No certificate files found in certs/${NC}"
			echo "  Please place your wildcard certificates in the certs/ directory:"
			echo "    - certs/wildcard.crt (or fullchain.pem)"
			echo "    - certs/wildcard.key (private key)"
			echo ""
			if prompt_yes_no_default_no "Generate self-signed certificates instead?"; then
				GENERATE_CERTS=true
			fi
		fi
	else
		echo -e "${YELLOW}No certs directory found${NC}"
		if prompt_yes_no_default_no "Create certs directory and generate self-signed certificates?"; then
			mkdir -p "$PROJECT_ROOT/certs"
			GENERATE_CERTS=true
		else
			echo -e "${YELLOW}Using Traefik default certificates${NC}"
		fi
	fi
	;;
2)
	if prompt_yes_no "Generate persistent self-signed certificates?"; then
		GENERATE_CERTS=true
	fi
	;;
3)
	echo -e "${YELLOW}Using Traefik default certificates (not persistent)${NC}"
	;;
*)
	echo -e "${RED}Invalid option, using Traefik default${NC}"
	;;
esac

# Update .env with certificate configuration
if [ -f "$PROJECT_ROOT/.env" ]; then
	if [ "$CERT_OPTION" -eq 1 ] && [ -n "$CERT_FILE" ] && [ -n "$KEY_FILE" ]; then
		sed_inplace "s|^SSL_CERT_TYPE=.*|SSL_CERT_TYPE=existing|" "$PROJECT_ROOT/.env"
		USE_EXISTING_CERTS=true
	elif [ "$CERT_OPTION" -eq 2 ]; then
		sed_inplace "s|^SSL_CERT_TYPE=.*|SSL_CERT_TYPE=selfsigned|" "$PROJECT_ROOT/.env"
	else
		sed_inplace "s|^SSL_CERT_TYPE=.*|SSL_CERT_TYPE=traefik|" "$PROJECT_ROOT/.env"
	fi
fi
echo ""

# Execute steps
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "Executing setup..."
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Initialize .env
if [ "$INIT_ENV" = true ]; then
	if [ -f "$PROJECT_ROOT/.env.example" ]; then
		cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
		echo -e "${GREEN}✓ Created .env file${NC}"
	else
		echo -e "${RED}✗ .env.example not found${NC}"
		exit 1
	fi
fi

# Set domain in .env
if [ -f "$PROJECT_ROOT/.env" ]; then
	sed_inplace "s/DOMAIN=.*/DOMAIN=$DOMAIN/g" "$PROJECT_ROOT/.env"
	echo -e "${GREEN}✓ Set domain to: $DOMAIN${NC}"
fi

# Generate passwords
if [ "$GENERATE_PASSWORDS" = true ]; then
	# Generate all plain text passwords first
	POSTGRES_PASS=$(generate_password)
	OPENWEBUI_DB_PASS=$(generate_password)
	LITELLM_DB_PASS=$(generate_password)
	OPENWEBUI_SECRET=$(generate_password)
	LITELLM_MASTER=sk-$(generate_password)
	LITELLM_SALT=$(generate_password)
	GRAFANA_PASS=$(generate_password)
	QDRANT_KEY=$(generate_password)
	VALKEY_PASS=$(generate_password)
	TRAEFIK_PASS=$(generate_password)
	KEYCLOAK_DB_PASS=$(generate_password)
	KEYCLOAK_ADMIN_PASS=$(generate_password)
	OPENWEBUI_OIDC_SECRET=$(generate_password)
	LITELLM_OIDC_SECRET=$(generate_password)
	GF_OIDC_SECRET=$(generate_password)

	# Replace all passwords with specific line patterns to avoid substring matches
	sed_inplace "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$POSTGRES_PASS|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^OPENWEBUI_DB_PASSWORD=.*|OPENWEBUI_DB_PASSWORD=$OPENWEBUI_DB_PASS|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^LITELLM_DB_PASSWORD=.*|LITELLM_DB_PASSWORD=$LITELLM_DB_PASS|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^OPENWEBUI_SECRET_KEY=.*|OPENWEBUI_SECRET_KEY=$OPENWEBUI_SECRET|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^LITELLM_MASTER_KEY=.*|LITELLM_MASTER_KEY=$LITELLM_MASTER|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^LITELLM_SALT_KEY=.*|LITELLM_SALT_KEY=$LITELLM_SALT|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^GF_SECURITY_ADMIN_PASSWORD=.*|GF_SECURITY_ADMIN_PASSWORD=$GRAFANA_PASS|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^QDRANT_API_KEY=.*|QDRANT_API_KEY=$QDRANT_KEY|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^VALKEY_PASSWORD=.*|VALKEY_PASSWORD=$VALKEY_PASS|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^TRAEFIK_DASHBOARD_PASSWORD=.*|TRAEFIK_DASHBOARD_PASSWORD=$TRAEFIK_PASS|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^KEYCLOAK_DB_PASSWORD=.*|KEYCLOAK_DB_PASSWORD=$KEYCLOAK_DB_PASS|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^KEYCLOAK_ADMIN_PASSWORD=.*|KEYCLOAK_ADMIN_PASSWORD=$KEYCLOAK_ADMIN_PASS|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^OPENWEBUI_OIDC_CLIENT_SECRET=.*|OPENWEBUI_OIDC_CLIENT_SECRET=$OPENWEBUI_OIDC_SECRET|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^LITELLM_OIDC_CLIENT_SECRET=.*|LITELLM_OIDC_CLIENT_SECRET=$LITELLM_OIDC_SECRET|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^GF_OIDC_CLIENT_SECRET=.*|GF_OIDC_CLIENT_SECRET=$GF_OIDC_SECRET|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^KEYCLOAK_ADMIN_IP_RANGE=.*|KEYCLOAK_ADMIN_IP_RANGE=0.0.0.0/0|" "$PROJECT_ROOT/.env"
	sed_inplace "s|^BACKUP_RETENTION_DAYS=.*|BACKUP_RETENTION_DAYS=7|" "$PROJECT_ROOT/.env"

	# Generate Traefik dashboard password hash from the plain password we just set
	TRAEFIK_HASH=$(generate_htpasswd "$TRAEFIK_PASS")
	# Escape $ signs for Docker Compose variable interpolation (apr1 hashes use $ in format)
	# Also escape special characters for sed
	ESCAPED_HASH=$(echo "$TRAEFIK_HASH" | sed 's/\$/$$/g' | sed 's/[\/&]/\\&/g')
	sed_inplace "s|^TRAEFIK_DASHBOARD_PASSWORD_HASH=.*|TRAEFIK_DASHBOARD_PASSWORD_HASH=$ESCAPED_HASH|" "$PROJECT_ROOT/.env"

	echo -e "${GREEN}✓ Generated secure passwords${NC}"
fi

# Generate SSL certificates
if [ "$GENERATE_CERTS" = true ]; then
	export DOMAIN
	"$SCRIPT_DIR/generate-certs.sh"
fi

# Start Docker stack and configure Keycloak clients
echo -e "${YELLOW}Step 5: Start Docker Stack${NC}"
if prompt_yes_no_default_no "Start Docker stack and configure Keycloak clients?"; then
	echo -e "${BLUE}Starting Docker stack...${NC}"
	cd "$PROJECT_ROOT"

	# Pull images first
	echo "Pulling latest images..."
	if ! docker compose pull 2>&1; then
		echo -e "${YELLOW}Warning: Some images failed to pull, continuing with existing images${NC}"
	fi

	# Start services
	echo "Starting services..."
	if ! docker compose up -d 2>&1; then
		echo -e "${RED}Failed to start Docker stack!${NC}"
		echo "Check logs with: docker compose logs"
		error_exit "Docker compose failed"
	fi

	echo ""
	echo -e "${YELLOW}Waiting for services to be ready...${NC}"
	echo "This may take a few minutes..."

	# Wait for Keycloak to be ready
	echo "Waiting for Keycloak..."
	max_attempts=60
	attempt=0
	KC_URL="https://auth.${DOMAIN:-localhost}"
	while [ $attempt -lt $max_attempts ]; do
		if curl -skf --resolve "auth.${DOMAIN:-localhost}:443:127.0.0.1" "$KC_URL/realms/master" >/dev/null 2>&1 ||
			curl -skf "$KC_URL/realms/master" >/dev/null 2>&1; then
			echo -e "${GREEN}Keycloak is ready!${NC}"
			break
		fi
		attempt=$((attempt + 1))
		echo "  Waiting... ($attempt/$max_attempts)"
		sleep 3
	done

	if [ $attempt -eq $max_attempts ]; then
		echo -e "${YELLOW}Keycloak did not respond yet. You can run the configuration later:${NC}"
		echo "  ./scripts/configure-keycloak-clients.py"
	else
		echo -e "${BLUE}Configuring Keycloak clients...${NC}"
		python3 "$SCRIPT_DIR/configure-keycloak-clients.py"

		echo ""
		echo -e "${YELLOW}Restarting services to apply OIDC changes...${NC}"
		docker compose restart grafana open-webui litellm
		echo -e "${GREEN}Services restarted!${NC}"
	fi
fi

# 5. Additional Providers (Optional)
echo -e "${YELLOW}Step 6: Additional LLM Providers (Optional)${NC}"
echo ""

# Discord Webhook for Grafana alerts
DISCORD_WEBHOOK=""
if prompt_yes_no "Configure Discord webhook for Grafana alerts?"; then
	read -rp "Enter Discord webhook URL: " DISCORD_WEBHOOK
	if [[ -n "$DISCORD_WEBHOOK" ]]; then
		if [ -f "$PROJECT_ROOT/.env" ]; then
			if grep -q "^GRAFANA_DISCORD_WEBHOOK_URL=" "$PROJECT_ROOT/.env" 2>/dev/null; then
				sed_inplace "s|^GRAFANA_DISCORD_WEBHOOK_URL=.*|GRAFANA_DISCORD_WEBHOOK_URL=$DISCORD_WEBHOOK|" "$PROJECT_ROOT/.env"
			else
				echo "GRAFANA_DISCORD_WEBHOOK_URL=$DISCORD_WEBHOOK" >>"$PROJECT_ROOT/.env"
			fi
			echo -e "${GREEN}✓ Added Discord webhook to .env${NC}"
		else
			echo -e "${RED}.env file not found. Add manually: GRAFANA_DISCORD_WEBHOOK_URL=$DISCORD_WEBHOOK${NC}"
		fi
	fi
fi

# Gemini API Key
GEMINI_KEY=""
if prompt_yes_no "Configure Google Gemini provider?"; then
	read -rp "Enter Gemini API key: " GEMINI_KEY
	if [[ -n "$GEMINI_KEY" ]]; then
		if [ -f "$PROJECT_ROOT/.env" ]; then
			if grep -q "^GEMINI_API_KEY=" "$PROJECT_ROOT/.env" 2>/dev/null; then
				sed_inplace "s|^GEMINI_API_KEY=.*|GEMINI_API_KEY=$GEMINI_KEY|" "$PROJECT_ROOT/.env"
			else
				echo "GEMINI_API_KEY=$GEMINI_KEY" >>"$PROJECT_ROOT/.env"
			fi
			echo -e "${GREEN}✓ Added Gemini API key to .env${NC}"
		else
			echo -e "${RED}.env file not found. Add manually: GEMINI_API_KEY=$GEMINI_KEY${NC}"
		fi
	fi
fi

# AWS Bedrock (OSS Models)
if prompt_yes_no "Configure AWS Bedrock provider for OSS models (MiniMax, Qwen, Kimi)?"; then
	read -rp "Enter AWS Access Key ID: " AWS_ACCESS_KEY_ID
	read -rp "Enter AWS Secret Access Key: " AWS_SECRET_ACCESS_KEY

	if [[ -n "$AWS_ACCESS_KEY_ID" ]] && [[ -n "$AWS_SECRET_ACCESS_KEY" ]]; then
		if [ -f "$PROJECT_ROOT/.env" ]; then
			for KEY in "AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID" "AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY" "AWS_REGION=us-east-1"; do
				VAR_NAME="${KEY%%=*}"
				VAR_VALUE="${KEY##*=}"
				if grep -q "^${VAR_NAME}=" "$PROJECT_ROOT/.env" 2>/dev/null; then
					sed_inplace "s|^${VAR_NAME}=.*|${VAR_NAME}=${VAR_VALUE}|" "$PROJECT_ROOT/.env"
				else
					echo "${VAR_NAME}=${VAR_VALUE}" >>"$PROJECT_ROOT/.env"
				fi
			done
			echo -e "${GREEN}✓ Added AWS Bedrock credentials to .env${NC}"
		else
			echo -e "${RED}.env file not found. Add manually:${NC}"
			echo "  AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID"
			echo "  AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY"
			echo "  AWS_REGION=us-east-1"
		fi
	else
		echo -e "${YELLOW}Skipped - both Access Key ID and Secret are required${NC}"
	fi
fi

# llama.cpp Edge Nodes
if prompt_yes_no "Configure local llama.cpp edge nodes?"; then
	"$SCRIPT_DIR/setup-edge-nodes.sh"
fi

echo ""

# Make scripts executable
# chmod +x "$SCRIPT_DIR"/*.sh

echo ""
echo -e "${GREEN}✓ Setup complete!${NC}"
echo ""
