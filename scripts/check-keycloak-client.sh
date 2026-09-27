#!/bin/bash
set -euo pipefail

KEYCLOAK_URL="http://localhost:8080"
REALM="master"
ADMIN_USER="admin"
ADMIN_PASS="K3ycl0akAdm1nP@ssw0rd!"

# Get admin token
TOKEN=$(curl -sk -X POST "$KEYCLOAK_URL/realms/master/protocol/openid-connect/token" \
	-H "Content-Type: application/x-www-form-urlencoded" \
	-d "username=$ADMIN_USER" \
	-d "password=$ADMIN_PASS" \
	-d "grant_type=password" \
	-d "client_id=admin-cli" | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))")

# Get clients
curl -sk "$KEYCLOAK_URL/admin/realms/master/clients" \
	-H "Authorization: Bearer $TOKEN" | python3 -m json.tool | grep -A10 '"clientId": "litellm"'
