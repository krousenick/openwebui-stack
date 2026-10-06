#!/usr/bin/env python3
"""
Provision Keycloak SSO for Grafana, Open WebUI and LiteLLM.

Design goals:
- OIDC Authorization Code flow only
- No password/direct-access grants for application clients
- No service accounts on user-facing clients
- Application-specific client roles
- Application-specific dedicated client scopes
- Group -> client-role mappings
- Explicit role scope mappings
- Full Scope Allowed disabled
- Groups exposed as a "groups" claim
- Application roles exposed as a "roles" claim
- TLS verification enabled by default
- No curl/subprocess dependency
- Idempotent provisioning
- Secrets are not written to .env automatically

Tested conceptually against the current Keycloak Admin REST API model.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppConfig:
    client_id: str
    name: str
    base_url: str
    redirect_uris: tuple[str, ...]
    roles: tuple[str, ...]


DOMAIN = os.getenv("DOMAIN", "example.com")

APPS = (
    AppConfig(
        client_id="grafana",
        name="Grafana",
        base_url=f"https://grafana.{DOMAIN}",
        redirect_uris=(
            f"https://grafana.{DOMAIN}/login/generic_oauth",
        ),
        roles=("admin", "editor", "viewer"),
    ),
    AppConfig(
        client_id="open-webui",
        name="Open WebUI",
        base_url=f"https://chatbot.{DOMAIN}",
        redirect_uris=(
            f"https://chatbot.{DOMAIN}/oauth/oidc/callback",
        ),
        roles=("admin", "user"),
    ),
    AppConfig(
        client_id="litellm",
        name="LiteLLM",
        base_url=f"https://litellm.{DOMAIN}",
        redirect_uris=(
            f"https://litellm.{DOMAIN}/callback",
        ),
        roles=(
            "proxy_admin",
            "proxy_admin_viewer",
            "internal_user",
            "internal_user_viewer",
        ),
    ),
)


GROUP_ROLE_MAP: dict[str, tuple[str, str]] = {
    # group -> (client_id, client_role)
    "kc-grafana-admin": ("grafana", "admin"),
    "kc-grafana-editor": ("grafana", "editor"),
    "kc-grafana-viewer": ("grafana", "viewer"),

    "kc-openwebui-admin": ("open-webui", "admin"),
    "kc-openwebui-user": ("open-webui", "user"),

    "kc-litellm-admin": ("litellm", "proxy_admin"),
    "kc-litellm-viewer": ("litellm", "proxy_admin_viewer"),
    "kc-litellm-user": ("litellm", "internal_user"),
    "kc-litellm-readonly": ("litellm", "internal_user_viewer"),
}


# ---------------------------------------------------------------------------
# Keycloak API client
# ---------------------------------------------------------------------------

class KeycloakError(RuntimeError):
    pass


class Keycloak:
    def __init__(
        self,
        base_url: str,
        realm: str,
        token: str,
        verify_tls: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.realm = realm
        self.token = token
        self.verify_tls = verify_tls

        self.session = requests.Session()
        self.session.trust_env = True

        retry = Retry(
            total=4,
            connect=4,
            read=4,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(
                {"GET", "POST", "PUT", "DELETE"}
            ),
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=10,
            pool_maxsize=10,
        )

        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    @property
    def api(self) -> str:
        return (
            f"{self.base_url}/admin/realms/"
            f"{self.realm}"
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        expected: Iterable[int] = (200,),
        **kwargs: Any,
    ) -> requests.Response:
        url = f"{self.api}/{path.lstrip('/')}"

        headers = kwargs.pop("headers", {})
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            **headers,
        }

        response = self.session.request(
            method,
            url,
            headers=headers,
            verify=self.verify_tls,
            timeout=30,
            **kwargs,
        )

        if response.status_code not in expected:
            raise KeycloakError(
                f"{method} {url} failed: "
                f"HTTP {response.status_code}: "
                f"{response.text[:1000]}"
            )

        return response

    def json(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        response = self.request(method, path, **kwargs)

        if not response.content:
            return None

        return response.json()

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    def groups(self) -> list[dict]:
        return self.json("GET", "/groups", expected=(200,))

    def group(self, name: str) -> dict | None:
        for group in self.groups():
            if group.get("name") == name:
                return group
        return None

    def ensure_group(self, name: str) -> dict:
        existing = self.group(name)

        if existing:
            return existing

        self.request(
            "POST",
            "/groups",
            json={"name": name},
            expected=(201,),
        )

        created = self.group(name)

        if not created:
            raise KeycloakError(
                f"Group '{name}' was created but could not be found"
            )

        print(f"  + group {name}")
        return created

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    def clients(self, client_id: str | None = None) -> list[dict]:
        params = {}

        if client_id:
            params["clientId"] = client_id

        return self.json(
            "GET",
            "/clients",
            params=params,
            expected=(200,),
        )

    def client(self, client_id: str) -> dict | None:
        clients = self.clients(client_id)

        for client in clients:
            if client.get("clientId") == client_id:
                return client

        return None

    def ensure_client(self, config: AppConfig) -> dict:
        existing = self.client(config.client_id)

        payload = {
            "clientId": config.client_id,
            "name": config.name,
            "enabled": True,

            # Confidential OIDC client.
            "publicClient": False,

            # Browser SSO.
            "standardFlowEnabled": True,

            # Do not use OAuth password grant.
            "directAccessGrantsEnabled": False,

            # Not required for browser SSO.
            "serviceAccountsEnabled": False,

            # Explicitly disable implicit flow.
            "implicitFlowEnabled": False,

            "protocol": "openid-connect",

            "rootUrl": config.base_url,
            "baseUrl": config.base_url,

            "redirectUris": list(config.redirect_uris),

            # Exact origin only.
            "webOrigins": [config.base_url],

            # Security boundary.
            "fullScopeAllowed": False,

            # Don't require consent for internal SSO apps.
            "consentRequired": False,

            # Standard OIDC settings.
            "attributes": {
                "pkce.code.challenge.method": "S256",
            },
        }

        if existing:
            self.request(
                "PUT",
                f"/clients/{existing['id']}",
                json=payload,
                expected=(204,),
            )

            print(f"  ~ client {config.client_id}")

            return self.client(config.client_id) or existing

        self.request(
            "POST",
            "/clients",
            json=payload,
            expected=(201,),
        )

        created = self.client(config.client_id)

        if not created:
            raise KeycloakError(
                f"Client '{config.client_id}' created but not found"
            )

        print(f"  + client {config.client_id}")
        return created

    # ------------------------------------------------------------------
    # Client roles
    # ------------------------------------------------------------------

    def client_roles(self, client_uuid: str) -> list[dict]:
        return self.json(
            "GET",
            f"/clients/{client_uuid}/roles",
            expected=(200,),
        )

    def client_role(
        self,
        client_uuid: str,
        role_name: str,
    ) -> dict | None:
        for role in self.client_roles(client_uuid):
            if role.get("name") == role_name:
                return role
        return None

    def ensure_client_role(
        self,
        client_uuid: str,
        role_name: str,
    ) -> dict:
        existing = self.client_role(client_uuid, role_name)

        if existing:
            return existing

        self.request(
            "POST",
            f"/clients/{client_uuid}/roles",
            json={
                "name": role_name,
                "description": (
                    f"Application role '{role_name}'"
                ),
            },
            expected=(201,),
        )

        created = self.client_role(
            client_uuid,
            role_name,
        )

        if not created:
            raise KeycloakError(
                f"Role '{role_name}' was not created"
            )

        print(
            f"    + role "
            f"{role_name}"
        )

        return created

    # ------------------------------------------------------------------
    # Client scopes
    # ------------------------------------------------------------------

    def client_scopes(self) -> list[dict]:
        return self.json(
            "GET",
            "/client-scopes",
            expected=(200,),
        )

    def client_scope(self, name: str) -> dict | None:
        for scope in self.client_scopes():
            if scope.get("name") == name:
                return scope
        return None

    def ensure_client_scope(
        self,
        name: str,
        description: str,
    ) -> dict:
        existing = self.client_scope(name)

        if existing:
            return existing

        self.request(
            "POST",
            "/client-scopes",
            json={
                "name": name,
                "description": description,
                "protocol": "openid-connect",
                "attributes": {
                    "include.in.token.scope": "false",
                    "display.on.consent.screen": "false",
                },
            },
            expected=(201,),
        )

        created = self.client_scope(name)

        if not created:
            raise KeycloakError(
                f"Client scope '{name}' was not created"
            )

        print(f"  + scope {name}")
        return created

    def client_scope_mappers(
        self,
        scope_uuid: str,
    ) -> list[dict]:
        return self.json(
            "GET",
            f"/client-scopes/{scope_uuid}/protocol-mappers/models",
            expected=(200,),
        )

    def ensure_scope_mapper(
        self,
        scope_uuid: str,
        mapper: dict,
    ) -> None:
        existing = self.client_scope_mappers(scope_uuid)

        for current in existing:
            if current.get("name") == mapper["name"]:
                return

        self.request(
            "POST",
            f"/client-scopes/{scope_uuid}/protocol-mappers/models",
            json=mapper,
            expected=(201, 200),
        )

        print(f"    + mapper {mapper['name']}")

    # ------------------------------------------------------------------
    # Client scope assignment
    # ------------------------------------------------------------------

    def default_scopes(
        self,
        client_uuid: str,
    ) -> list[dict]:
        return self.json(
            "GET",
            f"/clients/{client_uuid}/default-client-scopes",
            expected=(200,),
        )

    def ensure_default_scope(
        self,
        client_uuid: str,
        scope_uuid: str,
    ) -> None:
        existing = self.default_scopes(client_uuid)

        if any(
            scope.get("id") == scope_uuid
            for scope in existing
        ):
            return

        self.request(
            "PUT",
            f"/clients/{client_uuid}/default-client-scopes/"
            f"{scope_uuid}",
            expected=(204,),
        )

    # ------------------------------------------------------------------
    # Role scope mappings
    # ------------------------------------------------------------------

    def client_scope_role_mappings(
        self,
        scope_uuid: str,
        role_client_uuid: str,
    ) -> list[dict]:
        return self.json(
            "GET",
            f"/client-scopes/{scope_uuid}/scope-mappings/"
            f"clients/{role_client_uuid}",
            expected=(200,),
        )

    def ensure_scope_role(
        self,
        scope_uuid: str,
        role_client_uuid: str,
        role: dict,
    ) -> None:
        current = self.client_scope_role_mappings(
            scope_uuid,
            role_client_uuid,
        )

        if any(
            item.get("id") == role["id"]
            for item in current
        ):
            return

        self.request(
            "POST",
            f"/client-scopes/{scope_uuid}/scope-mappings/"
            f"clients/{role_client_uuid}",
            json=[
                {
                    "id": role["id"],
                    "name": role["name"],
                }
            ],
            expected=(204,),
        )

    # ------------------------------------------------------------------
    # Group -> client role mapping
    # ------------------------------------------------------------------

    def group_client_roles(
        self,
        group_uuid: str,
        client_uuid: str,
    ) -> list[dict]:
        return self.json(
            "GET",
            f"/groups/{group_uuid}/role-mappings/"
            f"clients/{client_uuid}",
            expected=(200,),
        )

    def ensure_group_client_role(
        self,
        group_uuid: str,
        client_uuid: str,
        role: dict,
    ) -> None:
        existing = self.group_client_roles(
            group_uuid,
            client_uuid,
        )

        if any(
            item.get("id") == role["id"]
            for item in existing
        ):
            return

        self.request(
            "POST",
            f"/groups/{group_uuid}/role-mappings/"
            f"clients/{client_uuid}",
            json=[
                {
                    "id": role["id"],
                    "name": role["name"],
                }
            ],
            expected=(204,),
        )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def get_admin_token(
    keycloak_url: str,
    realm: str,
    client_id: str,
    client_secret: str,
    verify_tls: bool,
) -> str:
    """
    Preferred authentication method:
    confidential client + client credentials.

    The provisioning client must have appropriate realm-management
    permissions in the target realm.
    """

    token_url = (
        f"{keycloak_url.rstrip('/')}/realms/{realm}"
        "/protocol/openid-connect/token"
    )

    response = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        verify=verify_tls,
        timeout=30,
    )

    response.raise_for_status()

    token = response.json().get("access_token")

    if not token:
        raise KeycloakError(
            "Token endpoint returned no access_token"
        )

    return token


# ---------------------------------------------------------------------------
# Application configuration
# ---------------------------------------------------------------------------

def configure_application(
    kc: Keycloak,
    app: AppConfig,
) -> None:
    print(f"\nConfiguring {app.client_id}")

    client = kc.ensure_client(app)
    client_uuid = client["id"]

    # Dedicated scope prevents application claims from leaking
    # into other clients.
    scope_name = f"{app.client_id}-dedicated"

    scope = kc.ensure_client_scope(
        scope_name,
        f"Dedicated OIDC scope for {app.name}",
    )

    scope_uuid = scope["id"]

    # ---------------------------------------------------------------
    # Groups mapper
    # ---------------------------------------------------------------

    kc.ensure_scope_mapper(
        scope_uuid,
        {
            "name": "groups",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "consentRequired": False,
            "config": {
                "full.path": "false",
                "claim.name": "groups",
                "id.token.claim": "true",
                "access.token.claim": "true",
                "userinfo.token.claim": "true",
            },
        },
    )

    # ---------------------------------------------------------------
    # Application role mapper
    #
    # This produces:
    #
    # "roles": ["admin", "viewer"]
    #
    # rather than making applications understand Keycloak's
    # resource_access structure.
    # ---------------------------------------------------------------

    kc.ensure_scope_mapper(
        scope_uuid,
        {
            "name": "application-roles",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-client-role-mapper",
            "consentRequired": False,
            "config": {
                "client.id": app.client_id,
                "user.attribute": "",
                "claim.name": "roles",
                "jsonType.label": "String",
                "multivalued": "true",
                "id.token.claim": "true",
                "access.token.claim": "true",
                "userinfo.token.claim": "true",
            },
        },
    )

    # ---------------------------------------------------------------
    # Role scope mappings
    # ---------------------------------------------------------------

    for role_name in app.roles:
        role = kc.client_role(
            client_uuid,
            role_name,
        )

        if not role:
            raise KeycloakError(
                f"Expected role {app.client_id}:{role_name}"
                " does not exist"
            )

        kc.ensure_scope_role(
            scope_uuid,
            client_uuid,
            role,
        )

    # Make the dedicated scope part of this client.
    kc.ensure_default_scope(
        client_uuid,
        scope_uuid,
    )

    print(
        f"  ✓ {app.client_id}: "
        f"dedicated scope + mappers + roles configured"
    )


def configure_groups_and_roles(kc: Keycloak) -> None:
    print("\nConfiguring groups")

    groups = {}

    for group_name in sorted(
        set(GROUP_ROLE_MAP)
    ):
        groups[group_name] = kc.ensure_group(group_name)

    print("\nConfiguring group -> client role mappings")

    for group_name, (
        client_id,
        role_name,
    ) in GROUP_ROLE_MAP.items():

        group = groups[group_name]
        client = kc.client(client_id)

        if not client:
            raise KeycloakError(
                f"Client '{client_id}' not found"
            )

        role = kc.client_role(
            client["id"],
            role_name,
        )

        if not role:
            raise KeycloakError(
                f"Role '{client_id}:{role_name}' not found"
            )

        kc.ensure_group_client_role(
            group["id"],
            client["id"],
            role,
        )

        print(
            f"  ✓ {group_name} -> "
            f"{client_id}:{role_name}"
        )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(
    kc: Keycloak,
) -> bool:
    print("\nVerifying configuration")

    success = True

    for app in APPS:
        client = kc.client(app.client_id)

        if not client:
            print(
                f"  ✗ missing client: {app.client_id}"
            )
            success = False
            continue

        if client.get("fullScopeAllowed") is not False:
            print(
                f"  ✗ {app.client_id}: "
                "fullScopeAllowed is not disabled"
            )
            success = False

        if client.get("directAccessGrantsEnabled"):
            print(
                f"  ✗ {app.client_id}: "
                "direct access grants enabled"
            )
            success = False

        if client.get("serviceAccountsEnabled"):
            print(
                f"  ✗ {app.client_id}: "
                "service account enabled"
            )
            success = False

        if not client.get("standardFlowEnabled"):
            print(
                f"  ✗ {app.client_id}: "
                "standard flow disabled"
            )
            success = False

        for uri in app.redirect_uris:
            if uri not in client.get(
                "redirectUris",
                [],
            ):
                print(
                    f"  ✗ {app.client_id}: "
                    f"missing redirect URI {uri}"
                )
                success = False

        print(
            f"  ✓ {app.client_id}"
        )

    for group_name, (
        client_id,
        role_name,
    ) in GROUP_ROLE_MAP.items():

        group = kc.group(group_name)
        client = kc.client(client_id)

        if not group or not client:
            success = False
            continue

        roles = kc.group_client_roles(
            group["id"],
            client["id"],
        )

        if not any(
            role.get("name") == role_name
            for role in roles
        ):
            print(
                f"  ✗ {group_name} -> "
                f"{client_id}:{role_name}"
            )
            success = False
        else:
            print(
                f"  ✓ {group_name} -> "
                f"{client_id}:{role_name}"
            )

    return success


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Provision Keycloak OIDC SSO for "
            "Grafana, Open WebUI and LiteLLM"
        )
    )

    parser.add_argument(
        "--keycloak-url",
        default=os.getenv(
            "KEYCLOAK_URL",
            f"https://auth.{DOMAIN}",
        ),
    )

    parser.add_argument(
        "--realm",
        default=os.getenv(
            "KEYCLOAK_REALM",
            "ai-platform",
        ),
    )

    parser.add_argument(
        "--provisioner-client-id",
        default=os.getenv(
            "KEYCLOAK_PROVISIONER_CLIENT_ID",
        ),
    )

    parser.add_argument(
        "--provisioner-client-secret",
        default=os.getenv(
            "KEYCLOAK_PROVISIONER_CLIENT_SECRET",
        ),
    )

    parser.add_argument(
        "--insecure",
        action="store_true",
        help=(
            "Disable TLS verification. "
            "Use only for development."
        ),
    )

    args = parser.parse_args()

    if not args.provisioner_client_id:
        print(
            "ERROR: "
            "KEYCLOAK_PROVISIONER_CLIENT_ID is required",
            file=sys.stderr,
        )
        return 1

    if not args.provisioner_client_secret:
        print(
            "ERROR: "
            "KEYCLOAK_PROVISIONER_CLIENT_SECRET is required",
            file=sys.stderr,
        )
        return 1

    verify_tls = not args.insecure

    print("=" * 72)
    print("Keycloak SSO Provisioning")
    print("=" * 72)

    print(
        f"Keycloak: {args.keycloak_url}"
    )
    print(
        f"Realm:    {args.realm}"
    )

    # ---------------------------------------------------------------
    # Obtain provisioning token.
    # ---------------------------------------------------------------

    print("\nObtaining provisioning token...")

    token = get_admin_token(
        keycloak_url=args.keycloak_url,
        realm=args.realm,
        client_id=args.provisioner_client_id,
        client_secret=args.provisioner_client_secret,
        verify_tls=verify_tls,
    )

    kc = Keycloak(
        base_url=args.keycloak_url,
        realm=args.realm,
        token=token,
        verify_tls=verify_tls,
    )

    # ---------------------------------------------------------------
    # Applications
    # ---------------------------------------------------------------

    for app in APPS:
        configure_application(kc, app)

    # ---------------------------------------------------------------
    # Groups / roles
    # ---------------------------------------------------------------

    configure_groups_and_roles(kc)

    # ---------------------------------------------------------------
    # Verify
    # ---------------------------------------------------------------

    print()

    if not verify(kc):
        print("\nCONFIGURATION FAILED")
        return 2

    print("\n" + "=" * 72)
    print("KEYCLOAK CONFIGURATION PASSED")
    print("=" * 72)

    print(
        """
Next steps:

1. Configure Grafana Generic OAuth.
2. Configure Open WebUI OIDC.
3. Configure LiteLLM SSO/OIDC.
4. Put users into the appropriate Keycloak groups.
5. Test with a non-administrator test account.
6. Inspect the resulting ID/access token claims.
"""
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)
    except requests.RequestException as exc:
        print(
            f"\nHTTP ERROR: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except KeycloakError as exc:
        print(
            f"\nKEYCLOAK ERROR: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
