---
name: sso-troubleshooting
description: >-
  Diagnose and fix Keycloak SSO login failures across Open WebUI, LiteLLM,
  and Grafana: decode error symptoms to root cause (missing client secret,
  role-claim mismatch, group mapper, redirect URI) and apply the exact fix
  in the openwebui-stack Ansible provisioning. Use whenever SSO, OIDC,
  Keycloak login, or "email or password incorrect" errors come up.
license: MIT
---

# SSO Troubleshooting (Keycloak OIDC)

Everything below applies to the `platform` realm provisioned by
`ansible/roles/openwebui_stack/tasks/keycloak_app.yml`. The heal for
almost every entry is a playbook rerun — the table tells you what to
verify afterward.

## Symptom → cause → fix

| App | Symptom | Cause | Fix |
|---|---|---|---|
| Open WebUI | "email or password incorrect" on SSO login | Missing `OPENWEBUI_OIDC_CLIENT_SECRET`, or OAuth failed and it fell back to local login | Rerun playbook; ensure `ENABLE_OAUTH_SIGNUP: "true"`, `ENABLE_OAUTH: "true"`; secrets come from vault.local.yml |
| Open WebUI | 403 after login | Roles in token don't literally match `OAUTH_ALLOWED_ROLES` (role management is on) | User needs the `openwebui_user` client role via group membership (`OAUTH_ALLOWED_ROLES: "openwebui_user"`, admin role `openwebui_admin`) |
| Open WebUI | Login works but user lands as `pending`/`user`, no admin | Group→role mapping missing | Assign the user's Keycloak group with `openwebui_admin` role mapping (`keycloak_group_role_mappings`) |
| LiteLLM | "authentication database unreachable" | OIDC config incomplete / missing client secret / wrong `DATABASE_URL` | Rerun playbook; verify `GENERIC_CLIENT_ID: litellm`, secret from vault, `PROXY_BASE_URL: https://litellm.<domain>` |
| Grafana | "Login failed - Failed to get token" | Missing `GF_OIDC_CLIENT_SECRET` or user not in a mapped group | Rerun playbook; assign group in Keycloak |
| Any | Redirect URI mismatch on Keycloak error page | Client's allowed redirect differs from real URL | Redirects are declared in `keycloak_apps` (vars.yml); rerun and compare with the error page |
| Keycloak admin | 403 on `/admin` | IP allowlist (`KEYCLOAK_ADMIN_IP_RANGE`) | Set the correct range in `.env` |

## Verify (after a heal)

```bash
# Discovery endpoint answers
curl -sk "https://auth.localhost/realms/platform/.well-known/openid-configuration"

# Clients exist (example uses the master admin CLI on the stack host)
docker compose exec keycloak /opt/keycloak/bin/kc.sh list-clients --realm platform

# Token carries the groups claim: log in once, then decode the JWT
# (groups → OAUTH_GROUP_CLAIM; roles → OAUTH_ROLES_CLAIM) at jwt.io or
# with base64 -d on the payload segment.
```

## Non-negotiables

1. **`auth.localhost` (or `auth.<domain>`) must resolve** on the browser
   machine — check `/etc/hosts` first before blaming Keycloak.
2. **Users must be assigned to Keycloak groups** for role mapping to work
   (`ENABLE_OAUTH_GROUP_MANAGEMENT: "true"`, claim `groups`).
3. **Client scopes need the group mapper.** It is added by
   `keycloak_app.yml` dedicated scope mappers; a hand-created client in
   the console will not have it.
4. **Passkey logins**: the realm has passkeys enabled
   (`keycloak_passkeys_enabled`); passwordless credentials register from
   the account console. A user with only a passkey cannot complete a
   client_credentials flow — not an SSO bug.
5. Open WebUI uses **internal container URLs** for discovery
   (`http://keycloak:8080/realms/...`); LiteLLM and Grafana use public
   URLs for browser redirects. Both are correct; do not "normalize" them.
