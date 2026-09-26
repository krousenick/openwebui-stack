#!/bin/bash
# =============================================================================
# Keycloak Client Configuration Script
# =============================================================================
# Automatically creates OIDC clients for Grafana, Open WebUI, and LiteLLM
# Usage: ./scripts/configure-keycloak-clients.sh [realm]

set -eo pipefail

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

# Get admin password - try .env first, then container
KC_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-}"
if [ -z "$KC_ADMIN_PASSWORD" ]; then
	KC_ADMIN_PASSWORD=$(docker exec openwebui-stack_keycloak printenv KC_BOOTSTRAP_ADMIN_PASSWORD 2>/dev/null || echo "")
fi

if [ -z "$KC_ADMIN_PASSWORD" ]; then
	echo "ERROR: Cannot find Keycloak admin password"
	exit 1
fi
CLIENTS=(
	"grafana:Grafana:https://grafana.${DOMAIN:-localhost}:/login/generic_oauth"
	"open-webui:Open WebUI:https://chat.${DOMAIN:-localhost}:/oauth/oidc/callback"
	"litellm:LiteLLM:https://litellm.${DOMAIN:-localhost}:/callback"
)

echo "=============================================="
echo "  Keycloak Client Configuration"
echo "=============================================="
echo ""

# Wait for Keycloak to be ready (check via docker exec to bypass Traefik)
echo "[1/4] Waiting for Keycloak to be ready..."
max_attempts=60
attempt=0
while [ $attempt -lt $max_attempts ]; do
	# Check Keycloak health directly via container or via metrics endpoint
	if curl -skf "$KC_URL/realms/master" >/dev/null 2>&1; then
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
echo "  URL: $KC_URL/realms/master/protocol/openid-connect/token"
echo "  User: $KC_ADMIN_USER"
echo "  Pass length: ${#KC_ADMIN_PASSWORD}"

# Debug: Try directly first
TOKEN_RESPONSE=$(curl -sf -X POST "$KC_URL/realms/master/protocol/openid-connect/token" \
	-H "Content-Type: application/x-www-form-urlencoded" \
	-d "username=$KC_ADMIN_USER" \
	-d "password=$KC_ADMIN_PASSWORD" \
	-d "grant_type=password" \
	-d "client_id=admin-cli" 2>&1)

echo "  Token response: ${TOKEN_RESPONSE:0:100}..."

ADMIN_TOKEN=$(echo "$TOKEN_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null)
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

# Create clients and groups
echo "[3/5] Creating groups and roles..."

# Create admin group if it doesn't exist
ADMIN_GROUP=$(curl -sf -X GET "$KC_URL/admin/realms/$REALM/groups?search=admin" \
    -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -c "import sys, json; groups = json.load(sys.stdin); print(groups[0]['id'] if groups else '')" 2>/dev/null || echo "")

if [ -z "$ADMIN_GROUP" ]; then
    echo "  Creating admin group..."
    curl -sf -X POST "$KC_URL/admin/realms/$REALM/groups" \
        -H "Authorization: Bearer $ADMIN_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"name": "admin"}' >/dev/null
    ADMIN_GROUP=$(curl -sf -X GET "$KC_URL/admin/realms/$REALM/groups?search=admin" \
        -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -c "import sys, json; print(json.load(sys.stdin)[0]['id'])")
else
    echo "  Admin group already exists"
fi

# Create user group if it doesn't exist
USER_GROUP=$(curl -sf -X GET "$KC_URL/admin/realms/$REALM/groups?search=user" \
    -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -c "import sys, json; groups = json.load(sys.stdin); print(groups[0]['id'] if groups else '')" 2>/dev/null || echo "")

if [ -z "$USER_GROUP" ]; then
    echo "  Creating user group..."
    curl -sf -X POST "$KC_URL/admin/realms/$REALM/groups" \
        -H "Authorization: Bearer $ADMIN_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"name": "user"}' >/dev/null
    USER_GROUP=$(curl -sf -X GET "$KC_URL/admin/realms/$REALM/groups?search=user" \
        -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -c "import sys, json; print(json.load(sys.stdin)[0]['id'])")
else
    echo "  User group already exists"
fi

echo "  Groups created: admin ($ADMIN_GROUP), user ($USER_GROUP)"

# Create clients
echo "[4/5] Creating OIDC clients..."

for client_spec in "${CLIENTS[@]}"; do
	IFS=':' read -r client_id client_name redirect_base redirect_path <<<"$client_spec"

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

	# Build redirect URIs based on client type
	redirect_uris="\"$redirect_base/*\", \"$redirect_base$redirect_path\""
	if [ "$client_id" = "open-webui" ]; then
		# Open WebUI uses /oauth/oidc/callback
		redirect_uris="\"$redirect_base/oauth/oidc/callback\""
	elif [ "$client_id" = "grafana" ]; then
		# Grafana uses /login/generic_oauth
		redirect_uris="\"$redirect_base/login/generic_oauth\", \"$redirect_base/*\""
	elif [ "$client_id" = "litellm" ]; then
		# LiteLLM uses /callback
		redirect_uris="\"$redirect_base/callback\", \"$redirect_base/*\""
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
    "rootUrl": "$redirect_base",
    "redirectUris": [$redirect_uris],
    "webOrigins": ["$redirect_base"],
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

	# Add group membership protocol mapper for Open WebUI
	if [ "$client_id" = "open-webui" ]; then
		echo "    Adding groups mapper for Open WebUI..."
		# Get the client scope UUID
		client_scope_uuid=$(curl -sf -X GET "$KC_URL/admin/realms/$REALM/client-scopes" \
			-H "Authorization: Bearer $ADMIN_TOKEN" | \
			python3 -c "import sys, json; scopes = json.load(sys.stdin); print([s['id'] for s in scopes if s['name'] == 'profile'][0] if any(s['name'] == 'profile' for s in scopes) else '')")
		
		if [ -n "$client_scope_uuid" ]; then
			# Create/Update groups mapper
			mapper_payload='{"name":"groups","protocol":"openid-connect","protocolMapper":"oidc-group-membership-mapper","consentRequired":false,"config":{"full.path":"false","introspection.token.claim":"true","userinfo.token.claim":"true","id.token.claim":"true","access.token.claim":"true","claim.name":"groups","jsonType.label":"String"}}'
			
			curl -sf -X POST "$KC_URL/admin/realms/$REALM/client-scopes/$client_scope_uuid/protocol-mappers/models" \
				-H "Authorization: Bearer $ADMIN_TOKEN" \
				-H "Content-Type: application/json" \
				-d "$mapper_payload" >/dev/null 2>&1 || echo "    (Mapper may already exist)"
		fi
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

echo "[5/5] Summary"
echo ""
echo "Keycloak realm configured:"
echo "  Realm: $REALM"
echo "  Groups: admin, user"
echo ""
echo "OIDC clients created:"
echo "  - grafana: $redirect_base/login/generic_oauth"
echo "  - open-webui: https://chat.${DOMAIN:-localhost}/oauth/oidc/callback"
echo "  - litellm: https://litellm.${DOMAIN:-localhost}/callback"
echo ""
echo "Client secrets saved to .env"
echo ""
echo "Next steps:"
echo "  1. Restart services: docker compose restart grafana open-webui litellm"
echo "  2. Or restart the entire stack: docker compose down && docker compose up -d"
echo ""
echo "Done!"
