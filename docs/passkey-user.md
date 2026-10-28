# Set up a new user with a passkey

Passkeys are enabled on the `platform` realm by `ansible/roles/openwebui_stack/tasks/keycloak_passkeys.yml`. A new user still needs a password the first time, because the passkey is registered after login. After that, the user can sign in with the passkey and skip the password. This follows the [Keycloak 26.8 account console](https://www.keycloak.org/docs/latest/server_admin/#passwordless-authentication-with-webauthn).

Use the realm `platform`, not `master`. Replace `auth.example.com` with `auth.$DOMAIN`.

## 1. Create the user

1. Open `https://auth.example.com/admin` and select the `platform` realm.
2. Go to **Users** and click **Add user**.
3. Set the username, email, first name, and last name. Turn on **Email verified** if you do not want Keycloak to ask the user to verify it.
4. Save.
5. Open **Credentials**, click **Set password**, enter a password, and turn off **Temporary**.
6. Open **Groups** and join one group from the role table in `README.md`. A user with no group can sign in but does not get an application role.

To add the user through Ansible instead, add an entry to `keycloak_users` in `ansible/group_vars/all/vars.yml` and rerun `ansible-playbook playbooks/site.yml` from `ansible/`.

## 2. Register the passkey

The user does this in a browser that can use a passkey, such as Chrome, Safari, or Edge with a platform authenticator, phone, or security key.

1. Open `https://auth.example.com/realms/platform/account`.
2. Sign in with the username and password from step 1.
3. Click **Account security**, then **Signing in**.
4. In the **Passwordless** section, click **Set up a Passkey**.
5. Click **Register** and complete the browser prompt.
6. Sign out.

The two-factor **Set up a Passkey** button is a second factor after the password. Use the button in the **Passwordless** section for passwordless login.

## 3. Sign in with the passkey

1. Open `https://chatbot.example.com`, `https://grafana.example.com`, `https://litellm.example.com`, or `https://voice.example.com`.
2. On the Keycloak username form, click **Sign in with Passkey**, or focus the username field and pick the passkey from the browser autofill list.
3. Complete the browser prompt. Keycloak skips the password.

The passkey is stored by the browser or device for `auth.example.com`. A different browser or device needs its own passkey, registered again from the account console.
