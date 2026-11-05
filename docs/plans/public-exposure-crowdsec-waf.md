# Plan: Public exposure with CrowdSec WAF + US-only geo allowlist

Status: **Approved plan, not yet implemented.** This document is self-contained:
a future session can execute it without the original conversation context.

Written 2026-11-05. Research citations inline; re-verify versions before executing.

---

## 1. Goal

Expose this stack (chatbot/litellm/grafana/auth at `*.krouse.io`) to the public
internet, restricted to **US source IPs only**, with request-level WAF
protection (SQLi/XSS/RCE/virtual patching) and per-IP ban capability.

Current state: Traefik publishes 80/443 on all interfaces but the Photon VM's
iptables firewall (policy DROP, only 22/SSH open) blocks all external traffic —
nothing is publicly reachable yet. The owner also wants the eventual exposure to
not undo the internal-only conveniences (Loki log pipeline, admin access).

## 2. Stack context (for a session with no prior context)

- Repo: `openwebui-stack`, Docker Compose on VMware Photon OS. Deployed tree:
  `/mnt/data1/openwebui-stack` on host `10.0.105.14` (SSH as root with key
  `~/.ssh/id_rsa_10.0.105.14`). All work happens through the OrbStack Fedora VM
  (`orb -m fedora ...`), never on the Mac and never inside the Photon VM's OS
  directly except via SSH.
- Compose/env/Traefik config are **Ansible-templated**: edit
  `ansible/roles/openwebui_stack/templates/docker-compose.yml.j2`,
  `templates/env.j2`, `traefik/traefik.yml` (static), `traefik/dynamic/*.yml`
  — then run `ansible-playbook -i inventory/hosts.yml -i inventory/hosts.local.yml
  playbooks/site.yml` from `ansible/`. Never hand-edit the deployed files.
- Secret tiering: vault (`ansible/group_vars/all/vault.yml`, and the gitignored
  `vault.local.yml` which overrides for this deployment) → environment variable
  → deployed `.env` → generated once + persisted back to vault.
- Traefik v3.7 (image `traefik:v3.7`, pinned minor). Entrypoints: `web` (:80,
  redirects to websecure), `websecure` (:443, TLS), `mcp` (:8082, internal-only
  HTTP), `traefik` (:8080, metrics/API). Access logs go to **stdout** in JSON
  (deliberate: Grafana Alloy ships them to Loki — do NOT set `accessLog.filePath`).
- All routers use the `websecure` entrypoint via compose labels
  (`traefik.http.routers.<name>.entrypoints=websecure`). Traefik dashboard is on
  websecure with basicauth (`dashboard-auth` middleware). Keycloak admin console
  is reachable at `https://auth.krouse.io/admin` through the public keycloak
  router. A commented-out `ipwhitelist` middleware scaffold for
  `keycloak-admin` exists in the compose template around line 520
  (`KEYCLOAK_ADMIN_IP_RANGE` in `.env`).
- Keycloak realm hardening already done (2026-11-05): brute-force detection,
  password policy, refresh token rotation, login/admin event logging, passkeys
  enabled, self-registration enabled with `verify_email: false` (**no SMTP
  server exists** — verify_email must stay false or signups are stranded).
- LiteLLM free tier: UI SSO limited to 5 users (6th gets 403; requires
  enterprise license — `litellm_license` vault knob already wired).
- Firewall: Photon `iptables -L INPUT` is policy DROP with an accept-all rule
  (rule 1), established/related, and SSH. The Photon host is behind NAT on
  `10.0.105.0/24` — public exposure additionally requires port-forward
  80+443 on the upstream router, OR Cloudflare Tunnel (see decisions).

## 3. Architecture decision (already made, with rationale)

**CrowdSec + AppSec component + Traefik bouncer plugin** — single container:

- CrowdSec's **AppSec** component listens on `:7422` and inspects full requests
  (headers/URI/args/body) **before** they reach the apps. The Traefik bouncer
  plugin forwards requests to it in `live` mode and blocks on verdicts.
- Hub collections do the injection defense:
  `crowdsecurity/appsec-virtual-patching` (CVE exploit patterns) and
  `crowdsecurity/appsec-generic-rules` (generic SQLi/XSS/path traversal/RCE).
  `crowdsecurity/appsec-crs` is the fuller OWASP CRS if generic rules prove
  insufficient.
- **US-only geo enforcement is an AppSec `pre_eval` hook rule** (officially
  documented pattern — https://docs.crowdsec.net/docs/next/appsec/rules_examples/
  § "GeoBlocking"):
  ```yaml
  inband:
    pre_eval:
      - filter: GeoIPEnrich(req.RemoteAddr)?.Country.IsoCode not in ["US", ""]
        apply:
          - DropRequest("Forbidden Country")
  ```
  The `""` entry passes IPs with unknown geo (and the `?` prevents nil-crash on
  lookup failure — both intentional per the docs). `GeoIPEnrich` uses the
  GeoLite2 databases that the `crowdsecurity/geoip-enrich` hub item downloads
  automatically (hub item declares source_url → GeoLite2-City.mmdb; see
  https://github.com/crowdsecurity/hub/blob/master/parsers/s02-enrich/crowdsecurity/geoip-enrich.yaml).
  So: install `geoip-enrich`, and the helper has its data. GeoLite updates are
  handled by CrowdSec's hub data refresh.
- Log-scenario protection (existing traefik log scenarios for scanners/brute
  force on top of AppSec) uses the **docker source** in `acquis.yaml` reading
  Traefik's stdout JSON — verified compatible with the no-filePath design:
  ```yaml
  source: docker
  container_name:
    - openwebui-stack_traefik
  labels:
    type: traefik
  ```
- Bouncer plugin: `github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin`
  (the maintained one; the older `traefik-crowdsec-bouncer` is deprecated and
  lacks AppSec). Latest release at research time: **v1.7.1**
  (https://github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin/releases —
  re-check). It supports `crowdsecMode: live|stream` and AppSec forwarding
  (`crowdsecAppsecEnabled: true`, `crowdsecAppsecHost: crowdsec:7422`).
- Reference examples reviewed and partially borrowed:
  https://github.com/FoxxMD/traefik-homelab (entrypoint-level middleware
  pattern, bouncer key via env, escalating ban durations in profiles.yaml,
  `clientTrustedIPs` LAN bypass; their two-container LAPI/ingest split is
  unnecessary on a single host).

## 4. Open decisions (blockers — ask the owner before implementing)

| # | Decision | Options | Impact |
|---|---|---|---|
| D1 | DNS/edge | (a) Direct: A records `*.krouse.io` → public IP, port-forward 80/443 on router. (b) Cloudflare-proxied: CF in front, origin behind CF. | **(b) changes the whole design**: CrowdSec sees CF IPs — must set `forwardedHeadersTrustedIPs` to CF ranges and geo must come from CF's `CF-IPCountry` header (or use CF WAF geo rule directly, simpler); TLS becomes CF edge certs + origin cert. **(a) keeps this plan as-is.** |
| D2 | Keycloak admin console (`/admin`) | (a) IP-whitelist via the commented `ipwhitelist` middleware. (b) `KC_HOSTNAME_ADMIN` on a separate unpublished hostname. | Public `/admin` with only basicauth-less Keycloak login is the highest-value target. |
| D3 | Self-registration | (a) Keep open (CrowdSec captcha remediation for flagged IPs; no email verify until SMTP exists). (b) `keycloak_registration_enabled: false` until SMTP. | Open signup + no email verification = spam-account magnet on the public internet. |
| D4 | Travel fallback | (a) VPN endpoint (WireGuard on Photon or router) (b) add a second allowed country to the geo list. (c) accept lockout risk. | US-only geo drops the owner when traveling. |

Do not start implementation until D1 is answered; D2–D4 can be defaulted
(D2a whitelist, D3b closed, D4c accept) if the owner declines to choose.

## 5. Implementation plan

### Phase 1 — CrowdSec service (compose + vault)

1. Add `crowdsec` service to `templates/docker-compose.yml.j2` (profile:
   none — always on; or `security` profile if the owner wants it optional).
   ```yaml
   crowdsec:
     image: crowdsecurity/crowdsec:v1.7.8   # pin; re-check latest
     container_name: openwebui-stack_crowdsec
     restart: unless-stopped
     environment:
       COLLECTIONS: "crowdsecurity/traefik crowdsecurity/appsec-virtual-patching crowdsecurity/appsec-generic-rules crowdsecurity/geoip-enrich"
       BOUNCER_KEY_traefik: "${CROWDSEC_BOUNCER_KEY:?error}"
       GID: "1000"
     volumes:
       - crowdsec-config:/etc/crowdsec
       - crowdsec-data:/var/lib/crowdsec/data
       - /var/run/docker.sock:/var/run/docker.sock:ro   # for docker-source acquis
     networks:
       - backend    # same network as traefik
     healthcheck: ... (curl localhost:8080/healthy or cscli)
     security_opt: ["no-new-privileges:true"]
   ```
   Note: the docker-source acquisition needs the docker socket (read-only) —
   or switch to a file source if filePath access-log is ever added.
2. Acquisition config: bind-mount a repo-managed
   `crowdsec/acquis.yaml` (docker source, type traefik) and a
   `crowdsec/appsec-configs/` directory containing the geo rule file
   (see Phase 2). CrowdSec containers generate defaults on first run into
   `/etc/crowdsec`; mount only the specific files to override
   (see FoxxMD README note: first run with empty dirs, then overlay).
3. Bouncer key: new vault key `crowdsec_bouncer_key` (follow the
   `litellm_license`/`brave_search_api_key` optional→required pattern, but
   this one is required once the crowdsec service exists). Render into
   `templates/env.j2` as `CROWDSEC_BOUNCER_KEY=`.

### Phase 2 — AppSec + US geo rule

4. Create `crowdsec/appsec-configs/us-allowlist.yaml` with the `pre_eval`
   geo hook from §3. AppSec configs are mounted at
   `/etc/crowdsec/appsec-configs/` inside the container. Register it via
   env `APPSEC_CONFIGS: "crowdsecurity/virtual-patching us-allowlist"`
   (hub item + local config together). Verify with
   `docker compose exec crowdsec cscli appsec-configs list` and
   `cscli hub list`.
5. AppSec listens on `:7422` by default when a config exists — no port
   publish needed (Traefik reaches it over the docker network).

### Phase 3 — Traefik bouncer plugin

6. Traefik static config (`traefik/traefik.yml`): plugins need
   `experimental.plugins` and a `plugins-storage` volume; the plugin
   downloads at container start:
   ```yaml
   experimental:
     plugins:
       csbouncer:
         moduleName: github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin
         version: v1.7.1   # re-check at implementation time
   ```
   and add `- ./plugins-storage:/plugins-storage` style volume
   (see plugin README: https://github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin).
7. Dynamic config (`traefik/dynamic/middlewares.yml`): add
   ```yaml
   crowdsec:
     plugin:
       csbouncer:
         enabled: true
         crowdsecMode: live            # per-request, needed for AppSec
         crowdsecLapiScheme: http
         crowdsecLapiHost: crowdsec:8080
         crowdsecLapiKey: "{{ env \"CROWDSEC_BOUNCER_KEY\" }}"
         crowdsecAppsecEnabled: true
         crowdsecAppsecHost: crowdsec:7422
         crowdsecAppsecFailureBlock: true   # fail-open vs fail-closed: choose
         forwardedHeadersTrustedIPs: []     # only for CF case (D1b)
         clientTrustedIPs:
           - "10.0.105.0/24"               # LAN bypass: never ban the owner
   ```
   (FoxxMD pattern for the env-key indirection.)
8. Attach the middleware at the **websecure entrypoint** so every router
   inherits it without touching ~20 services' labels (FoxxMD pattern):
   ```yaml
   websecure:
     address: ":443"
     http:
       middlewares:
         - crowdsec@file
       tls: {}
   ```
   If entrypoint-level middlewares prove unreliable in this Traefik version,
   fall back to appending `crowdsec@file` to each router's middleware list.

### Phase 4 — TLS for public exposure

9. Currently origin/self-signed (see `scripts/generate-certs.sh` and
   `traefik/dynamic/certs.yml`, `ssl_cert_type` var). Public needs real certs:
   - D1a (direct): add a `certificatesResolvers.letsencrypt` with HTTP-01
     (`tls.certresolver: le` on routers). Requires port 80 reachable.
   - D1b (Cloudflare): either DNS-01 with a CF API token (vault key
     `cf_dns_api_token`), or CF origin certs + CF edge TLS.
   Keep the `mcp` internal entrypoint on the existing origin cert.

### Phase 5 — Exposure + hardening deltas

10. Open the edge: router/NAT port-forward 80+443 → Photon (D1a), or CF
    Tunnel (D1b). Photon iptables: the current accept-all INPUT rule means
    once the router forwards, the host is exposed — consider tightening to
    only 22/80/443 (owner decision; the docker daemon's own iptables rules
    handle container traffic).
11. Keycloak admin console per D2. Registration per D3. HSTS + full security
    headers (`secure-headers` middleware exists in
    `traefik/dynamic/middlewares.yml` — extend it) — recommended once public.
12. Community blocklist enrollment (free; now valuable since internet-facing):
    `docker compose exec crowdsec cscli console enroll <console_id>` —
    optional, owner decision (shares attack telemetry).

### Phase 6 — Verification (all from the Fedora VM)

13. `docker compose exec crowdsec cscli decisions list` — decisions appear.
14. `docker compose exec crowdsec cscli appsec-rules list` — geo +
    vpatch rules loaded; `cscli hub list` — collections enabled.
15. Geo test: request through the stack from a non-US source is impossible
    to test directly from the LAN — instead verify the rule loads and fires:
    `cscli appsec-rules inspect crowdsecurity/...` and check
    `docker compose logs crowdsec | grep -i "Forbidden Country"` after
    exposing. A US VPN exit node is the practical test.
16. Injection test: `curl -sk "https://chatbot.krouse.io/?id=1%27%20OR%20%271%27=%271"` →
    expect 403 from the bouncer + an AppSec alert (`cscli alerts list`).
17. Normal flows unaffected: Open WebUI SSO login, LiteLLM SSO login
    (`/sso/key/generate`), Grafana SSO, chat completion through LiteLLM,
    MCP entrypoint (8082) still internal-only.
18. Ban-safety: from a LAN client, deliberately trigger a scenario, confirm
    `clientTrustedIPs` exemption prevents self-lockout.

## 6. Risks and mitigations

| Risk | Mitigation |
|---|---|
| CrowdSec down → all requests fail-open (or blocked if `crowdsecAppsecFailureBlock`) | Start with fail-open (`crowdsecAppsecFailureBlock: false`) during rollout, flip after a week stable |
| GeoLite DB missing → `GeoIPEnrich` returns nil → `?` handles nil, `""` passes everything (fail-open on geo!) | After `cscli hub upgrade`, verify `GeoLite2-City.mmdb` exists in `/var/lib/crowdsec/geoip/` and test a known non-US IP shows `IsoCode` |
| Traefik plugin update requires container recreate | Pin plugin version; note in upgrade docs |
| Body-inspection latency on chat completions (large bodies) | AppSec `SetMaxBodySize` default (10MB) is fine; monitor p95 on websecure |
| Self-lockout from a bad ban | `clientTrustedIPs` LAN bypass (§5.7) + `cscli decisions delete` runbook |
| `users` in Open WebUI role-management 403 regression | Already fixed (`OAUTH_ALLOWED_ROLES: openwebui_user`) — do not regress when touching the template |

## 7. Effort estimate

~1 working day for a session with this doc: CrowdSec service + vault key
(1–2h), AppSec config + geo rule (1h), Traefik plugin + middleware (1–2h),
TLS (30min–2h depending on D1), exposure + verification (1–2h), plus D2/D3
hardening (30min).

## 8. Reference links

- CrowdSec WAF QuickStart for Traefik: https://docs.crowdsec.net/docs/appsec/quickstart/traefik/
- WAF rules examples (GeoBlocking hook): https://docs.crowdsec.net/docs/next/appsec/rules_examples/
- geoip-enrich hub item: https://app.crowdsec.net/hub/author/crowdsecurity/configurations/geoip-enrich
- Bouncer plugin: https://github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin
- FoxxMD traefik-homelab examples: https://github.com/FoxxMD/traefik-homelab
- CrowdSec blog (compose example with AppSec): https://www.crowdsec.net/blog/waf-traefik-crowdsec
- Virtual patching collection: https://app.crowdsec.net/hub/author/crowdsecurity/collections/appsec-virtual-patching
