#!/usr/bin/env python3
"""
Keycloak Client Configuration Script
Creates OIDC clients for Grafana, Open WebUI, and LiteLLM with proper groups, roles, and associations.
"""

import os
import sys
import json
import secrets
import argparse
from pathlib import Path
from typing import Optional, Dict, List, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def load_env(project_root: Path) -> dict:
    """Load environment variables from .env file."""
    env_file = project_root / ".env"
    env = {}
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env[key] = value
    return env


def get_admin_token(kc_url: str, username: str, password: str, verify: bool = True) -> str:
    """Get admin access token from Keycloak using curl."""
    import subprocess
    
    token_url = f"{kc_url}/realms/master/protocol/openid-connect/token"
    
    curl_cmd = [
        "curl", "-sk", "-X", "POST", token_url,
        "-H", "Content-Type: application/x-www-form-urlencoded",
        "-d", f"username={username}",
        "-d", f"password={password}",
        "-d", "grant_type=password",
        "-d", "client_id=admin-cli"
    ]
    
    result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=30)
    
    import json
    try:
        data = json.loads(result.stdout)
        if "access_token" in data:
            return data["access_token"]
        raise Exception(f"No access_token in response: {data}")
    except json.JSONDecodeError:
        raise Exception(f"Failed to parse response: {result.stdout[:200]}")


class KeycloakClient:
    """Wrapper for Keycloak Admin API."""

    def __init__(self, kc_url: str, realm: str, token: str, verify: bool = True):
        self.kc_url = kc_url
        self.realm = realm
        self.token = token
        self.verify = verify
        self.base_url = f"{kc_url}/admin/realms/{realm}"
        self.session = requests.Session()
        
        # Disable proxy to avoid issues
        self.session.trust_env = False
        
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

    def get_groups(self, search: Optional[str] = None) -> list:
        """Get all groups or search by name."""
        params = {"search": search} if search else {}
        response = self.session.get(
            f"{self.base_url}/groups",
            params=params,
            headers=self._headers(),
            verify=self.verify
        )
        response.raise_for_status()
        return response.json()

    def get_group_by_name(self, name: str) -> Optional[dict]:
        """Get a group by its name."""
        groups = self.get_groups(search=name)
        for group in groups:
            if group.get("name") == name:
                return group
        return None

    def get_group_id(self, name: str) -> Optional[str]:
        """Get group ID by name."""
        group = self.get_group_by_name(name)
        return group.get("id") if group else None

    def create_group(self, name: str) -> dict:
        """Create a new group."""
        existing = self.get_group_by_name(name)
        if existing:
            print(f"  Group '{name}' already exists")
            return existing

        response = self.session.post(
            f"{self.base_url}/groups",
            json={"name": name},
            headers=self._headers(),
            verify=self.verify
        )
        response.raise_for_status()
        print(f"  Created group: {name}")
        return self.get_group_by_name(name)

    def assign_group_role(self, group_id: str, role: dict) -> None:
        """Assign a realm role to a group."""
        response = self.session.post(
            f"{self.base_url}/groups/{group_id}/role-mappings/realm",
            json=[role],
            headers=self._headers(),
            verify=self.verify
        )
        if response.status_code == 204:
            print(f"    Assigned role '{role.get('name')}' to group")

    def get_group_assigned_roles(self, group_id: str) -> list:
        """Get roles assigned to a group."""
        response = self.session.get(
            f"{self.base_url}/groups/{group_id}/role-mappings/realm",
            headers=self._headers(),
            verify=self.verify
        )
        response.raise_for_status()
        return response.json()

    def get_roles(self) -> list:
        """Get all realm roles."""
        response = self.session.get(
            f"{self.base_url}/roles",
            headers=self._headers(),
            verify=self.verify
        )
        response.raise_for_status()
        return response.json()

    def get_role_by_name(self, name: str) -> Optional[dict]:
        """Get a role by name."""
        roles = self.get_roles()
        for role in roles:
            if role.get("name") == name:
                return role
        return None

    def create_role(self, name: str, description: str = "") -> dict:
        """Create a new realm role."""
        existing = self.get_role_by_name(name)
        if existing:
            print(f"  Role '{name}' already exists")
            return existing

        response = self.session.post(
            f"{self.base_url}/roles",
            json={"name": name, "description": description},
            headers=self._headers(),
            verify=self.verify
        )
        if response.status_code == 409:
            print(f"  Role '{name}' already exists (409)")
            return self.get_role_by_name(name)
        response.raise_for_status()
        print(f"  Created role: {name}")
        return {"name": name}

    def get_clients(self, client_id: Optional[str] = None) -> list:
        """Get all clients or filter by client_id."""
        params = {"clientId": client_id} if client_id else {}
        response = self.session.get(
            f"{self.base_url}/clients",
            params=params,
            headers=self._headers(),
            verify=self.verify
        )
        response.raise_for_status()
        return response.json()

    def get_client_by_client_id(self, client_id: str) -> Optional[dict]:
        """Get a client by its clientId."""
        clients = self.get_clients(client_id=client_id)
        for client in clients:
            if client.get("clientId") == client_id:
                return client
        return None

    def get_client_uuid(self, client_id: str) -> Optional[str]:
        """Get client UUID by clientId."""
        client = self.get_client_by_client_id(client_id)
        return client.get("id") if client else None

    def create_client(self, client_data: dict) -> dict:
        """Create a new client."""
        response = self.session.post(
            f"{self.base_url}/clients",
            json=client_data,
            headers=self._headers(),
            verify=self.verify
        )
        if response.status_code == 409:
            print(f"  Client '{client_data.get('clientId')}' already exists (409)")
            return self.get_client_by_client_id(client_data.get("clientId"))
        response.raise_for_status()
        print(f"  Created client: {client_data.get('clientId')}")
        try:
            return response.json() if response.text else {}
        except Exception:
            return {}

    def update_client(self, client_uuid: str, client_data: dict) -> None:
        """Update an existing client."""
        response = self.session.put(
            f"{self.base_url}/clients/{client_uuid}",
            json=client_data,
            headers=self._headers(),
            verify=self.verify
        )
        response.raise_for_status()
        print(f"  Updated client")

    def get_client_secret(self, client_uuid: str) -> str:
        """Get or generate client secret."""
        response = self.session.get(
            f"{self.base_url}/clients/{client_uuid}/client-secret",
            headers=self._headers(),
            verify=self.verify
        )
        if response.status_code == 404:
            secret = secrets.token_hex(32)
            response = self.session.post(
                f"{self.base_url}/clients/{client_uuid}/client-secret",
                json={},
                headers=self._headers(),
                verify=self.verify
            )
            response.raise_for_status()
            return response.json().get("value", secret)
        response.raise_for_status()
        return response.json().get("value", "")

    def get_client_scopes(self) -> list:
        """Get all client scopes."""
        response = self.session.get(
            f"{self.base_url}/client-scopes",
            headers=self._headers(),
            verify=self.verify
        )
        response.raise_for_status()
        return response.json()

    def get_client_scope_by_name(self, name: str) -> Optional[dict]:
        """Get a client scope by name."""
        scopes = self.get_client_scopes()
        for scope in scopes:
            if scope.get("name") == name:
                return scope
        return None

    def get_client_scope_id(self, name: str) -> Optional[str]:
        """Get client scope ID by name."""
        scope = self.get_client_scope_by_name(name)
        return scope.get("id") if scope else None

    def get_client_scope_mappers(self, scope_name: str) -> list:
        """Get protocol mappers for a client scope."""
        scope_id = self.get_client_scope_id(scope_name)
        if not scope_id:
            return []
        response = self.session.get(
            f"{self.base_url}/client-scopes/{scope_id}/protocol-mappers/models",
            headers=self._headers(),
            verify=self.verify
        )
        response.raise_for_status()
        return response.json()

    def has_mapper(self, scope_name: str, mapper_name: str) -> bool:
        """Check if a mapper exists in a client scope."""
        mappers = self.get_client_scope_mappers(scope_name)
        return any(m.get("name") == mapper_name for m in mappers)

    def add_client_scope_mapper(self, scope_name: str, mapper_config: dict) -> None:
        """Add a protocol mapper to a client scope."""
        scope = self.get_client_scope_by_name(scope_name)
        if not scope:
            print(f"  Client scope '{scope_name}' not found, skipping mapper")
            return

        if self.has_mapper(scope_name, mapper_config.get("name")):
            print(f"  Mapper '{mapper_config.get('name')}' already exists in {scope_name}")
            return

        scope_id = scope["id"]
        response = self.session.post(
            f"{self.base_url}/client-scopes/{scope_id}/protocol-mappers/models",
            json=mapper_config,
            headers=self._headers(),
            verify=self.verify
        )
        if response.status_code == 409:
            print(f"  Mapper '{mapper_config.get('name')}' already exists in {scope_name}")
        else:
            response.raise_for_status()
            print(f"  Added mapper '{mapper_config.get('name')}' to {scope_name}")

    def get_default_client_scopes(self, client_uuid: str) -> list:
        """Get default client scopes assigned to a client."""
        response = self.session.get(
            f"{self.base_url}/clients/{client_uuid}/default-client-scopes",
            headers=self._headers(),
            verify=self.verify
        )
        response.raise_for_status()
        return response.json()

    def has_default_client_scope(self, client_uuid: str, scope_name: str) -> bool:
        """Check if client has a default client scope."""
        scopes = self.get_default_client_scopes(client_uuid)
        return any(s.get("name") == scope_name for s in scopes)

    def add_default_client_scope(self, client_uuid: str, scope_id: str) -> None:
        """Add a default client scope to a client."""
        response = self.session.put(
            f"{self.base_url}/clients/{client_uuid}/default-client-scopes/{scope_id}",
            headers=self._headers(),
            verify=self.verify
        )
        if response.status_code not in (204, 200):
            print(f"  Warning: Could not add default client scope {scope_id}")

    def get_optional_client_scopes(self, client_uuid: str) -> list:
        """Get optional client scopes assigned to a client."""
        response = self.session.get(
            f"{self.base_url}/clients/{client_uuid}/optional-client-scopes",
            headers=self._headers(),
            verify=self.verify
        )
        response.raise_for_status()
        return response.json()

    def has_optional_client_scope(self, client_uuid: str, scope_name: str) -> bool:
        """Check if client has an optional client scope."""
        scopes = self.get_optional_client_scopes(client_uuid)
        return any(s.get("name") == scope_name for s in scopes)

    def add_optional_client_scope(self, client_uuid: str, scope_id: str) -> None:
        """Add an optional client scope to a client."""
        response = self.session.put(
            f"{self.base_url}/clients/{client_uuid}/optional-client-scopes/{scope_id}",
            headers=self._headers(),
            verify=self.verify
        )
        if response.status_code not in (204, 200):
            print(f"  Warning: Could not add optional client scope {scope_id}")

    def get_client_role_mappers(self, client_uuid: str) -> list:
        """Get client role mappers for a client."""
        response = self.session.get(
            f"{self.base_url}/clients/{client_uuid}/protocol-mappers/models",
            headers=self._headers(),
            verify=self.verify
        )
        response.raise_for_status()
        return response.json()


def update_hosts_file(domain: str) -> None:
    """Add required hostnames to /etc/hosts if not already present."""
    import subprocess
    
    hosts_to_add = [
        f"127.0.0.1 auth.{domain}",
        f"127.0.0.1 chat.{domain}",
        f"127.0.0.1 grafana.{domain}",
        f"127.0.0.1 litellm.{domain}",
    ]
    
    # Check current hosts file
    try:
        result = subprocess.run(
            ["cat", "/etc/hosts"],
            capture_output=True,
            text=True,
            timeout=5
        )
        current_hosts = result.stdout
    except Exception:
        return
    
    modified = False
    for host_entry in hosts_to_add:
        hostname = host_entry.split()[1]
        if hostname not in current_hosts:
            try:
                with open("/etc/hosts", "a") as f:
                    f.write(f"{host_entry}\n")
                print(f"  Added {hostname} to /etc/hosts")
                modified = True
            except PermissionError:
                # Try with sudo
                try:
                    result = subprocess.run(
                        ["sudo", "sh", "-c", f"echo '{host_entry}' >> /etc/hosts"],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    if result.returncode == 0:
                        print(f"  Added {hostname} to /etc/hosts (sudo)")
                        modified = True
                except Exception:
                    pass
    
    if modified:
        print()


def wait_for_keycloak(kc_url: str, max_attempts: int = 60, verify: bool = True) -> bool:
    """Wait for Keycloak to be ready."""
    import subprocess
    
    print(f"Waiting for Keycloak at {kc_url}...")
    curl_opts = "-sk"
    if not verify:
        curl_opts = "-sk"
    
    for attempt in range(max_attempts):
        try:
            result = subprocess.run(
                ["curl", curl_opts, "--max-time", "5", f"{kc_url}/realms/master"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0 and '"realm"' in result.stdout:
                print("Keycloak is ready!")
                return True
            if result.returncode == 0:
                import json
                try:
                    data = json.loads(result.stdout)
                    if data.get("realm"):
                        print("Keycloak is ready!")
                        return True
                except:
                    pass
        except Exception:
            pass
        print(f"  Attempt {attempt + 1}/{max_attempts}...")
        import time
        time.sleep(3)
    return False


def verify_client_config(kc: KeycloakClient, client_id: str, expected_config: dict) -> bool:
    """Verify client has expected configuration."""
    client = kc.get_client_by_client_id(client_id)
    if not client:
        return False
    
    issues = []
    for key, expected in expected_config.items():
        actual = client.get(key)
        if actual != expected:
            issues.append(f"{key}: expected {expected}, got {actual}")
    
    return len(issues) == 0


def main():
    parser = argparse.ArgumentParser(description="Configure Keycloak OIDC clients")
    parser.add_argument("--realm", default="master", help="Keycloak realm")
    parser.add_argument("--kc-url", default=None, help="Keycloak URL")
    parser.add_argument("--domain", default="localhost", help="Domain for URLs")
    parser.add_argument("--verify-ssl", action="store_true", default=False, help="Verify SSL certificates")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    env = load_env(project_root)

    domain = env.get("DOMAIN", args.domain)
    kc_url = args.kc_url or env.get("KEYCLOAK_URL", f"https://auth.{domain}")
    kc_admin_user = env.get("KEYCLOAK_ADMIN_USER", "admin")
    kc_admin_password = env.get("KEYCLOAK_ADMIN_PASSWORD", "")

    if not kc_admin_password:
        print("ERROR: KEYCLOAK_ADMIN_PASSWORD not found in .env")
        sys.exit(1)

    verify = args.verify_ssl

    print("=" * 60)
    print("Keycloak Client Configuration")
    print("=" * 60)

    if args.kc_url:
        kc_urls_to_try = [args.kc_url]
    elif env.get("KEYCLOAK_URL"):
        kc_urls_to_try = [env.get("KEYCLOAK_URL")]
    else:
        print(f"\nUpdating /etc/hosts for domain '{domain}'...")
        update_hosts_file(domain)
        
        kc_external_url = f"https://auth.{domain}"
        kc_urls_to_try = [kc_external_url]

    kc_url = None
    for url in kc_urls_to_try:
        print(f"\nTrying Keycloak at {url}...")
        if wait_for_keycloak(url, max_attempts=10, verify=verify):
            kc_url = url
            break
        print(f"  Failed to connect to {url}")

    if not kc_url:
        print("\nERROR: Could not connect to Keycloak at any URL:")
        for url in kc_urls_to_try:
            print(f"  - {url}")
        sys.exit(1)

    print(f"\nUsing Keycloak URL: {kc_url}")

    print("\n[1/8] Getting admin token...")
    try:
        token = get_admin_token(kc_url, kc_admin_user, kc_admin_password, verify=verify)
        print("  Admin token obtained")
    except Exception as e:
        print(f"ERROR: Failed to get admin token: {e}")
        sys.exit(1)

    kc = KeycloakClient(kc_url, args.realm, token, verify=verify)

    print("\n[2/8] Creating groups...")
    groups_to_create = [
        "admin",
        "user",
        "editor",
        "viewer",
        "litellm-admin",
        "litellm-viewer",
    ]
    created_groups = {}
    for group_name in groups_to_create:
        group = kc.create_group(group_name)
        if group:
            created_groups[group_name] = group

    print("\n[3/8] Creating realm roles...")
    roles_to_create = [
        ("admin", "Admin role"),
        ("user", "User role"),
        ("editor", "Editor role"),
        ("viewer", "Viewer role"),
        ("proxy_admin", "LiteLLM Proxy Admin"),
        ("proxy_admin_viewer", "LiteLLM Proxy Admin Viewer"),
        ("internal_user", "LiteLLM Internal User"),
        ("internal_user_viewer", "LiteLLM Internal User Viewer"),
    ]
    created_roles = {}
    for role_name, description in roles_to_create:
        role = kc.create_role(role_name, description)
        if role:
            created_roles[role_name] = role

    print("\n[4/8] Creating OIDC clients...")

    grafana_base = f"https://grafana.{domain}"
    openwebui_base = f"https://chat.{domain}"
    litellm_base = f"https://litellm.{domain}"

    clients_config = [
        {
            "clientId": "grafana",
            "name": "Grafana",
            "enabled": True,
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "authorizationServicesEnabled": False,
            "standardFlowEnabled": True,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": True,
            "rootUrl": grafana_base,
            "redirectUris": [f"{grafana_base}/login/generic_oauth", f"{grafana_base}/*"],
            "webOrigins": [grafana_base],
            "protocol": "openid-connect",
            "attributes": {
                "access.token.lifespan": "300",
                "client.secret.creation.time": "0",
                "oauth2.device.authorization.grant.enabled": "false",
                "display.on.consent.screen": "false",
                "client_id.frontend": "true"
            }
        },
        {
            "clientId": "open-webui",
            "name": "Open WebUI",
            "enabled": True,
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "authorizationServicesEnabled": False,
            "standardFlowEnabled": True,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": True,
            "rootUrl": openwebui_base,
            "redirectUris": [f"{openwebui_base}/oauth/oidc/callback"],
            "webOrigins": [openwebui_base],
            "protocol": "openid-connect",
            "attributes": {
                "access.token.lifespan": "300",
                "client.secret.creation.time": "0",
                "oauth2.device.authorization.grant.enabled": "false",
                "display.on.consent.screen": "false",
                "client_id.frontend": "true"
            }
        },
        {
            "clientId": "litellm",
            "name": "LiteLLM",
            "enabled": True,
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "authorizationServicesEnabled": False,
            "standardFlowEnabled": True,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": True,
            "rootUrl": litellm_base,
            "redirectUris": [f"{litellm_base}/callback", f"{litellm_base}/*"],
            "webOrigins": [litellm_base],
            "protocol": "openid-connect",
            "attributes": {
                "access.token.lifespan": "300",
                "client.secret.creation.time": "0",
                "oauth2.device.authorization.grant.enabled": "false",
                "display.on.consent.screen": "false",
                "client_id.frontend": "true"
            }
        },
    ]

    client_secrets = {}
    client_uuids = {}
    for client_config in clients_config:
        client_id = client_config["clientId"]
        print(f"\n  Configuring {client_id}...")

        existing_client = kc.get_client_by_client_id(client_id)
        if existing_client:
            print(f"    Client exists, updating...")
            kc.update_client(existing_client["id"], client_config)
            client_uuid = existing_client["id"]
        else:
            print(f"    Creating new client...")
            kc.create_client(client_config)
            new_client = kc.get_client_by_client_id(client_id)
            client_uuid = new_client["id"] if new_client else ""
        
        client_uuids[client_id] = client_uuid

        secret = kc.get_client_secret(client_uuid)
        client_secrets[client_id] = secret
        print(f"    Client secret obtained: {secret[:16]}...")

        print(f"    Adding groups mapper to 'profile' scope...")
        groups_mapper = {
            "name": f"{client_id}-groups",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "consentRequired": False,
            "config": {
                "full.path": "false",
                "introspection.token.claim": "true",
                "userinfo.token.claim": "true",
                "id.token.claim": "true",
                "access.token.claim": "true",
                "claim.name": "groups",
                "jsonType.label": "String"
            }
        }
        kc.add_client_scope_mapper("profile", groups_mapper)

        print(f"    Adding roles mapper to 'profile' scope...")
        roles_mapper = {
            "name": f"{client_id}-roles",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-client-role-mapper",
            "consentRequired": False,
            "config": {
                "user.model": "client",
                "client.id": client_id,
                "introspection.token.claim": "true",
                "userinfo.token.claim": "true",
                "id.token.claim": "true",
                "access.token.claim": "true",
                "claim.name": "roles",
                "jsonType.label": "String",
                "multivalued": "true"
            }
        }
        kc.add_client_scope_mapper("profile", roles_mapper)

    print("\n[5/8] Assigning group-role mappings...")
    
    group_role_mappings = {
        "admin": ["admin"],
        "user": ["user"],
        "editor": ["editor"],
        "viewer": ["viewer"],
        "litellm-admin": ["proxy_admin"],
        "litellm-viewer": ["proxy_admin_viewer"],
    }
    
    for group_name, role_names in group_role_mappings.items():
        group_id = kc.get_group_id(group_name)
        if not group_id:
            print(f"  Warning: Group '{group_name}' not found, skipping role assignment")
            continue
        
        for role_name in role_names:
            role = created_roles.get(role_name)
            if role:
                kc.assign_group_role(group_id, role)
            else:
                print(f"  Warning: Role '{role_name}' not found for group '{group_name}'")

    print("\n[6/8] Assigning client scopes to clients...")
    
    required_scopes = ["profile", "roles", "email", "offline_access"]
    
    for client_id, client_uuid in client_uuids.items():
        print(f"  {client_id}:")
        for scope_name in required_scopes:
            scope_id = kc.get_client_scope_id(scope_name)
            if not scope_id:
                print(f"    Warning: Client scope '{scope_name}' not found")
                continue
            
            if not kc.has_default_client_scope(client_uuid, scope_name):
                kc.add_default_client_scope(client_uuid, scope_id)
                print(f"    Added default scope: {scope_name}")
            else:
                print(f"    Default scope already assigned: {scope_name}")

    print("\n[7/8] Updating .env file...")
    env_file = project_root / ".env"
    env_content = env_file.read_text() if env_file.exists() else ""

    secret_mappings = {
        "GF_OIDC_CLIENT_SECRET": "grafana",
        "OPENWEBUI_OIDC_CLIENT_SECRET": "open-webui",
        "LITELLM_OIDC_CLIENT_SECRET": "litellm",
    }

    for env_var, client_id in secret_mappings.items():
        if client_id in client_secrets:
            secret = client_secrets[client_id]
            if env_var in env_content:
                import re
                pattern = f"^{re.escape(env_var)}=.*$"
                env_content = re.sub(pattern, f"{env_var}={secret}", env_content, flags=re.MULTILINE)
                print(f"  Updated {env_var}")
            else:
                env_content += f"\n{env_var}={secret}"
                print(f"  Added {env_var}")

    env_file.write_text(env_content)

    print("\n[8/8] Verification...")
    print("\n  Clients:")
    all_clients_ok = True
    for client_id in ["grafana", "open-webui", "litellm"]:
        client = kc.get_client_by_client_id(client_id)
        if client:
            print(f"    ✓ {client_id}: {client['id']}")
            
            has_secret = kc.get_client_secret(client['id'])
            if has_secret:
                print(f"      ✓ Client secret exists")
            else:
                print(f"      ✗ Client secret missing")
                all_clients_ok = False
        else:
            print(f"    ✗ {client_id}: NOT FOUND")
            all_clients_ok = False

    print("\n  Groups:")
    all_groups_ok = True
    for group_name in groups_to_create:
        group = kc.get_group_by_name(group_name)
        if group:
            print(f"    ✓ {group_name}: {group['id']}")
        else:
            print(f"    ✗ {group_name}: NOT FOUND")
            all_groups_ok = False

    print("\n  Roles:")
    all_roles_ok = True
    for role_name, _ in roles_to_create:
        role = kc.get_role_by_name(role_name)
        if role:
            print(f"    ✓ {role_name}")
        else:
            print(f"    ✗ {role_name}: NOT FOUND")
            all_roles_ok = False

    print("\n  Group-Role Mappings:")
    all_mappings_ok = True
    for group_name, role_names in group_role_mappings.items():
        group_id = kc.get_group_id(group_name)
        if not group_id:
            print(f"    ✗ {group_name}: group not found")
            all_mappings_ok = False
            continue
        
        assigned_roles = kc.get_group_assigned_roles(group_id)
        assigned_role_names = [r.get("name") for r in assigned_roles]
        
        for expected_role in role_names:
            if expected_role in assigned_role_names:
                print(f"    ✓ {group_name} -> {expected_role}")
            else:
                print(f"    ✗ {group_name} -> {expected_role} (NOT ASSIGNED)")
                all_mappings_ok = False

    print("\n  Client Scopes (assigned to clients):")
    all_scopes_ok = True
    for client_id, client_uuid in client_uuids.items():
        print(f"    {client_id}:")
        for scope_name in required_scopes:
            is_default = kc.has_default_client_scope(client_uuid, scope_name)
            is_optional = kc.has_optional_client_scope(client_uuid, scope_name)
            if is_default:
                print(f"      ✓ {scope_name} (default)")
            elif is_optional:
                print(f"      ✓ {scope_name} (optional)")
            else:
                print(f"      ✗ {scope_name} (NOT ASSIGNED)")
                all_scopes_ok = False

    print("\n  Protocol Mappers (profile scope):")
    all_mappers_ok = True
    for client_id in ["grafana", "open-webui", "litellm"]:
        print(f"    {client_id}:")
        for mapper_name in [f"{client_id}-groups", f"{client_id}-roles"]:
            if kc.has_mapper("profile", mapper_name):
                print(f"      ✓ {mapper_name}")
            else:
                print(f"      ✗ {mapper_name} (NOT FOUND)")
                all_mappers_ok = False

    print("\n" + "=" * 60)
    if all_clients_ok and all_groups_ok and all_roles_ok and all_mappings_ok and all_scopes_ok and all_mappers_ok:
        print("✓ ALL VERIFICATIONS PASSED")
    else:
        print("⚠ SOME VERIFICATIONS FAILED - Review output above")
    print("=" * 60)
    print("\nRestart services to apply changes:")
    print("  docker compose restart grafana open-webui litellm")
    print("\nOr restart the entire stack:")
    print("  docker compose down && docker compose up -d")


if __name__ == "__main__":
    main()