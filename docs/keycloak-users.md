# Users, sign-in and account self-service

Keycloak is the one identity provider for Open WebUI, LiteLLM, Grafana, the voice UI and the LLM Launcher portals. Every person has one account in the `platform` realm, and group membership decides what each application lets them do.

Replace `example.com` with your `DOMAIN` throughout. Use the realm `platform`, not `master`: `master` only holds the bootstrap `admin` account.

The values below (password policy, session lengths, what users may edit) are what `ansible/roles/openwebui_stack/tasks/keycloak_bootstrap.yml` and `ansible/group_vars/all/vars.yml` configure. If a custom login theme is installed under `keycloak/themes/`, the wording of the links on the login page follows the theme; the URLs stay the same.

## URLs

### For everyone

| What | URL |
|------|-----|
| Chat (Open WebUI) | `https://chatbot.example.com` |
| Voice UI | `https://voice.example.com` |
| Account console: profile, password, passkeys, sessions | `https://auth.example.com/realms/platform/account/` |
| Request a password reset | `https://auth.example.com/realms/platform/login-actions/reset-credentials?client_id=account-console` |
| Register a new account | `https://auth.example.com/realms/platform/protocol/openid-connect/registrations?client_id=account&response_type=code&scope=openid&redirect_uri=https://auth.example.com/realms/platform/account/` |
| Sign out of Keycloak | `https://auth.example.com/realms/platform/protocol/openid-connect/logout` |

The account console is the plain "log in to the platform realm" address: it sends an anonymous visitor to the realm's login page and back. The login page itself carries the **Forgot Password?** and **Register** links, so most people never need the two long URLs.

The password reset page only works once Keycloak can send mail. See [Password reset](#password-reset).

### For administrators

| What | URL | Who can sign in |
|------|-----|-----------------|
| Keycloak admin console, `platform` realm | `https://auth.example.com/admin/platform/console/` | Members of `platform-admins` |
| Keycloak admin console, `master` realm | `https://auth.example.com/admin/` | The bootstrap `admin` account |
| LiteLLM admin UI | `https://litellm.example.com/ui` | LiteLLM roles, see the table below |
| Grafana | `https://grafana.example.com` | Grafana roles, see the table below |
| OIDC discovery document | `https://auth.example.com/realms/platform/.well-known/openid-configuration` | Anyone |

The bootstrap `admin` password is `keycloak_admin_password` in `ansible/group_vars/all/vault.local.yml` on the stack host (`KEYCLOAK_ADMIN_PASSWORD` in the deployed `.env`). When you sign in at `/admin/`, switch the realm selector to `platform` before touching users.

## Groups decide access

A user gets application roles only through groups. Role changes take effect the next time the user signs in to that application.

| Group | Open WebUI | LiteLLM | Grafana | Also |
|-------|------------|---------|---------|------|
| `platform-admins` | Admin | `proxy_admin` | GrafanaAdmin | Administers the `platform` realm in Keycloak; LLM Launcher portals |
| `admin` | Admin | `proxy_admin` | Admin | LLM Launcher portals |
| `user` | User | `internal_user` | — | |
| `editor` | — | — | Editor | |
| `viewer` | — | — | Viewer | |
| `litellm-admin` | — | `proxy_admin` | — | |
| `litellm-viewer` | — | `proxy_admin_viewer` | — | |

A dash means the group grants no role in that application.

- **`user` is the default group.** Every new account lands in it, whether it was self-registered or created by an administrator (`keycloak_default_groups` in `vars.yml`).
- **Open WebUI refuses an account without one of its roles.** The login ends in a 403 (`OAUTH_ALLOWED_ROLES`).
- **Grafana maps an account without one of its roles to `None`**, which carries no permissions.
- **The voice UI admits any signed-in realm user.** Its gate does not check a role.
- **LiteLLM's free tier allows five SSO users in its admin UI.** The sixth person to sign in there gets a 403 until a license is set (`litellm_license` in the vault). Open WebUI and Grafana have no such limit.

The mappings live in `keycloak_group_role_mappings` in `ansible/group_vars/all/vars.yml`. To add a group or change what one grants, edit that list and run `./scripts/setup.sh --tags keycloak` from the repository root.

## Create a user

There are three ways. People should use the first or second; the third is for shared and test accounts.

### 1. The user registers

Self-registration is on (`keycloak_registration_enabled: true`).

1. The user opens `https://chatbot.example.com` or the account console and clicks **Register** on the login page.
2. They fill in username, password, email, first name and last name. All are required.
3. The account is active at once and sits in `user`: chat in Open WebUI and a personal LiteLLM login. No verification mail is sent (`keycloak_verify_email: false`, because no mail server is configured).

For more than `user` access, an administrator adds the account to another group afterwards (Users → the user → **Groups** → **Join group**).

To turn self-registration off, set `keycloak_registration_enabled: false` in `vars.yml` and run `./scripts/setup.sh --tags keycloak`.

### 2. An administrator creates the account

1. Open `https://auth.example.com/admin/platform/console/` as a member of `platform-admins`.
2. Go to **Users** and click **Add user**.
3. Enter **Username**, **Email**, **First name** and **Last name**. The realm requires all four. Turn on **Email verified**.
4. Optional, **Required user actions**: choose **Update Password** to make the user pick their own password at first sign-in, and **Configure OTP** to make them set up an authenticator app.
5. Under **Groups**, click **Join groups** and pick the groups from the table above. `user` is added automatically.
6. Click **Create**.
7. Open the **Credentials** tab, click **Set password**, and enter a password that meets the [policy](#password-rules). Leave **Temporary** on to force a change at first sign-in.
8. Give the user their username, the temporary password, and the account console URL.

**Step 7 is not optional.** An account without a password cannot sign in, and a required action such as Update Password only runs after a successful sign-in. Keycloak cannot mail a set-password link either while no mail server is configured.

### 3. The playbook creates the account

Add an entry to `keycloak_users` in `ansible/group_vars/all/vars.yml`:

```yaml
  - username: jdoe
    email: "jdoe@{{ domain }}"
    first_name: Jane
    last_name: Doe
    password: "{{ keycloak_user_jdoe_password }}"
    group_memberships:
      - name: user
```

Put the password in `ansible/group_vars/all/vault.local.yml` on the stack host (`keycloak_user_jdoe_password: ...`). It has to meet the [policy](#password-rules). Then run the Keycloak part of the playbook from the repository root:

```bash
./scripts/setup.sh --tags keycloak
```

The playbook sets the password of every account in `keycloak_users` again on each run. A password the user changed themselves is overwritten, so keep people out of this list.

## What users can do themselves

Everything is in the account console, `https://auth.example.com/realms/platform/account/`.

| Task | Where |
|------|-------|
| Change email, first name, last name | **Personal info** |
| Change password | **Account security** → **Signing in** → **Password** → **Update** |
| Register a passkey | **Account security** → **Signing in** → **Passwordless**; see [passkey-user.md](passkey-user.md) |
| Add an authenticator app (one-time codes) | **Account security** → **Signing in** → **Two-factor authentication** |
| See active sessions, sign out everywhere | **Account security** → **Device activity** |
| See which applications they have used | **Applications** |

Two pages can also be opened directly: `.../account/account-security/signing-in` and `.../account/account-security/device-activity`.

Users cannot change their username or delete their own account. An administrator does both from the admin console.

### Password rules

At least 12 characters, with at least one upper-case letter and one digit. The password may not equal the username or the email address, and may not repeat any of the last five passwords.

After five failed attempts the account is locked for a minute. The wait grows with further failures, up to 15 minutes, and then clears by itself.

## Password reset

### While no mail server is configured (the default)

The **Forgot Password?** page is shown, but Keycloak has no mail server to send the reset link through, so the user never receives one. An administrator resets the password instead:

1. Admin console → **Users** → the user → **Credentials**.
2. Click **Reset password**, enter a new password, leave **Temporary** on.
3. Pass the temporary password to the user. Keycloak makes them choose a new one at the next sign-in.

A user who still knows their password changes it themselves in the account console.

### With a mail server

Set it once in the admin console: **Realm settings** → **Email**. Fill in the sender address and the SMTP host, port and credentials, then use **Test connection**. The playbook does not manage these settings.

After that:

- **Forgot Password?** sends the user a link. It is valid for 5 minutes.
- An administrator can send a link instead of setting a password: **Users** → the user → **Credentials** → **Credential reset**, pick **Update Password**, and send. That link is valid for 12 hours.
- Email verification for new accounts can be turned on: set `keycloak_verify_email: true` and run `./scripts/setup.sh --tags keycloak`.

## Sessions and signing out

- A Keycloak session ends after 30 minutes without activity and after 10 hours at most. Until then, opening another application signs the user in without asking again.
- **Log out** in Open WebUI, Grafana, the LiteLLM admin UI or the voice UI also ends the Keycloak session, which signs the user out of the other applications.
- A user ends all their sessions from **Device activity** in the account console. An administrator does it from **Users** → the user → **Sessions** → **Sign out**.

## Disable or remove a user

- **Disable:** admin console → **Users** → the user → turn **Enabled** off, then sign out their sessions. The account can no longer sign in anywhere.
- **Delete:** **Users** → select the user → **Delete**. This removes the Keycloak account only. Their chats in Open WebUI and their user record in LiteLLM stay until removed in those applications.
- **API keys are separate.** A LiteLLM virtual key or an Open WebUI API key keeps working after the Keycloak account is disabled or deleted. Revoke the keys in the LiteLLM admin UI (**Virtual Keys**) and in Open WebUI.

## Checking from the shell

The Keycloak admin CLI is in the container. This signs it in with the bootstrap admin from the container's own environment, so no password appears on the command line, and lists the realm's users:

```bash
docker compose exec keycloak sh -c '
  export KCADM_CONFIG=/tmp/kcadm.config
  /opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 --realm master \
    --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" --config "$KCADM_CONFIG"
  /opt/keycloak/bin/kcadm.sh get users -r platform --fields username,enabled,email --config "$KCADM_CONFIG"
  rm -f "$KCADM_CONFIG"
'
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| A new account cannot sign in at all | It has no password: it was created in the admin console and **Credentials** → **Set password** was skipped | Set a password there |
| Open WebUI answers 403 after the Keycloak login, or Grafana shows nothing | The account holds no role for that application | Add it to a group from the table above; the user signs in again |
| The user never gets a reset mail | No mail server is configured in the realm | Reset the password as an administrator, or configure **Realm settings** → **Email** |
| "Invalid username or password" although it is right | The account is locked after five failures | Wait; the lock clears by itself within 15 minutes |
| A sixth person gets a 403 in the LiteLLM admin UI | LiteLLM's free tier allows five SSO users | Set `litellm_license` in the vault and rerun the playbook |
| A playbook-managed account's password keeps changing back | The playbook sets it on every run | Change it in `vault.local.yml`, or take the account out of `keycloak_users` |

For login errors between Keycloak and the applications (redirect URI mismatches, missing client secrets), see "Troubleshooting SSO Issues" in [AGENTS.md](../AGENTS.md).
