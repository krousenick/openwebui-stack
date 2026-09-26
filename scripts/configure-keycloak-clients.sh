#!/bin/bash
# =============================================================================
# Keycloak Client Configuration Script
# =============================================================================
# Automatically creates OIDC clients for Grafana, Open WebUI, and LiteLLM
# Usage: ./scripts/configure-keycloak-clients.sh [realm]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Load env variables
if [ -f "$PROJECT_ROOT/.env" ]; then
	set -a
	source "$PROJECT_ROOT/.env"
	set +a
fi

REALM="${1:-master}"
KC_URL="https://auth.${DOMAIN:-localhost}"
KC_ADMIN_USER="${KEYCLOAK_ADMIN_USER:-admin}"
KC_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD}"
CLIENTS=(
	"grafana:Grafana:https://grafana.${DOMAIN:-localhost}"
	"open-webui:Open WebUI:https://chat.${DOMAIN:-localhost}"
	"litellm:LiteLLM:https://litellm.${DOMAIN:-localhost}"
)

echo "=============================================="
echo "  Keycloak Client Configuration"
echo "=============================================="
echo ""

# Wait for Keycloak to be ready
echo "[1/4] Waiting for Keycloak to be ready..."
max_attempts=60
attempt=0
while [ $attempt -lt $max_attempts ]; do
	if curl -sf "$KC_URL/health/ready" >/dev/null 2>&1; then
		echo "Keycloak is ready!"
		break
	fi
	attempt=$((attempt + 1))
	echo "  Waiting... ($attempt/$max_attempts)"
	sleep 2
done

if [ $attempt -eq $max_attempts ]; then
	echo "ERROR: Keycloak did not become ready in time"
	exit 1
fi

# Get admin token
echo "[2/4] Getting admin access token..."
ADMIN_TOKEN=$(curl -sf -X POST "$KC_URL/realms/master/protocol/openid-connect/token" \
	-H "Content-Type: application/x-www-form-urlencoded" \
	-d "username=$KC_ADMIN_USER" \
	-d "password=$KC_ADMIN_PASSWORD" \
	-d "grant_type=password" \
	-d "client_id=admin-cli" | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null)

if [ -z "$ADMIN_TOKEN" ]; then
	echo "ERROR: Failed to get admin token"
	exit 1
fi

echo "Admin token obtained!"

# Create clients
echo "[3/4] Creating OIDC clients..."

for client_spec in "${CLIENTS[@]}"; do
	IFS=':' read -r client_id client_name redirect_uri <<<"$client_spec"

	echo "  Configuring $client_name ($client_id)..."

	# Check if client exists
	existing=$(curl -sf -X GET "$KC_URL/admin/realms/$REALM/clients?clientId=$client_id" \
		-H "Authorization: Bearer $ADMIN_TOKEN")

	if echo "$existing" | python3 -c "import sys, json; exit(0 if len(json.load(sys.stdin)) > 0 else 1)" 2>/dev/null; then
		client_uuid=$(echo "$existing" | python3 -c "import sys, json; print(json.load(sys.stdin)[0]['id'])")
		echo "    Client already exists, updating..."
	else
		echo "    Creating new client..."
	fi

	# Create or update client
	client_payload=$(
		cat <<EOF
{
    "clientId": "$client_id",
    "name": "$client_name",
    "enabled": true,
    "publicClient": false,
    "serviceAccountsEnabled": true,
    "authorizationServicesEnabled": false,
    "standardFlowEnabled": true,
    "implicitFlowEnabled": false,
    "directAccessGrantsEnabled": true,
    "rootUrl": "$redirect_uri",
    "redirectUris": [
        "$redirect_uri/*",
        "$redirect_uri/callback"
    ],
    "webOrigins": ["$redirect_uri"],
    "protocol": "openid-connect",
    "attributes": {
        "access.token.lifespan": "300",
        "client.secret.creation.time": "$(date +%s)",
        "oauth2.device.authorization.grant.enabled": "false",
        "display.on.consent.screen": "false",
        "client_id.frontend": "true"
    }
}
EOF
	)

	if [ -n "${client_uuid:-}" ]; then
		# Update existing client
		curl -sf -X PUT "$KC_URL/admin/realms/$REALM/clients/$client_uuid" \
			-H "Authorization: Bearer $ADMIN_TOKEN" \
			-H "Content-Type: application/json" \
			-d "$client_payload" >/dev/null
	else
		# Create new client
		curl -sf -X POST "$KC_URL/admin/realms/$REALM/clients" \
			-H "Authorization: Bearer $ADMIN_TOKEN" \
			-H "Content-Type: application/json" \
			-d "$client_payload" >/dev/null

		# Get the new client UUID
		client_uuid=$(curl -sf -X GET "$KC_URL/admin/realms/$REALM/clients?clientId=$client_id" \
			-H "Authorization: Bearer $ADMIN_TOKEN" | python3 -c "import sys, json; print(json.load(sys.stdin)[0]['id'])")
	fi

	# Get client secret
	secret_payload=$(curl -sf -X GET "$KC_URL/admin/realms/$REALM/clients/$client_uuid/client-secret" \
		-H "Authorization: Bearer $ADMIN_TOKEN")

	client_secret=$(echo "$secret_payload" | python3 -c "import sys, json; print(json.load(sys.stdin).get('value', ''))" 2>/dev/null || echo "")

	if [ -n "$client_secret" ]; then
		echo "    Client secret obtained"

		# Update .env file with client secret
		case "$client_id" in
		grafana)
			sed_inplace() {
				if [[ $OSTYPE == "darwin"* ]]; then sed -i '' "$@"; else sed -i "$@"; fi
			}
			if [ -f "$PROJECT_ROOT/.env" ]; then
				sed_inplace "s|^GF_OIDC_CLIENT_SECRET=.*|GF_OIDC_CLIENT_SECRET=$client_secret|" "$PROJECT_ROOT/.env"
				echo "    Updated GF_OIDC_CLIENT_SECRET in .env"
			fi
			;;
		open-webui)
			sed_inplace() {
				if [[ $OSTYPE == "darwin"* ]]; then sed -i '' "$@"; else sed -i "$@"; fi
			}
			if [ -f "$PROJECT_ROOT/.env" ]; then
				sed_inplace "s|^OPENWEBUI_OIDC_CLIENT_SECRET=.*|OPENWEBUI_OIDC_CLIENT_SECRET=$client_secret|" "$PROJECT_ROOT/.env"
				echo "    Updated OPENWEBUI_OIDC_CLIENT_SECRET in .env"
			fi
			;;
		litellm)
			sed_inplace() {
				if [[ $OSTYPE == "darwin"* ]]; then sed -i '' "$@"; else sed -i "$@"; fi
			}
			if [ -f "$PROJECT_ROOT/.env" ]; then
				sed_inplace "s|^LITELLM_OIDC_CLIENT_SECRET=.*|LITELLM_OIDC_CLIENT_SECRET=$client_secret|" "$PROJECT_ROOT/.env"
				echo "    Updated LITELLM_OIDC_CLIENT_SECRET in .env"
			fi
			;;
		esac
	else
		echo "    WARNING: Could not get client secret"
	fi

	echo "    $client_name configured successfully!"
done

# Create/update Grafana user if needed (optional)
echo "[4/4] Summary"
echo ""
echo "Keycloak clients have been configured:"
echo "  - grafana: OIDC client for Grafana"
echo "  - open-webui: OIDC client for Open WebUI"
echo "  - litellm: OIDC client for LiteLLM"
echo ""
echo "Client secrets have been saved to .env"
echo ""
echo "Next steps:"
echo "  1. Restart services to pick up new client secrets: docker compose restart grafana open-webui litellm"
echo "  2. Or restart the entire stack: docker compose down && docker compose up -d"
echo ""
echo "Done!"
