# AGENTS.md - Agent Coding Guidelines

This is a Docker Compose stack for Open WebUI with LiteLLM, Keycloak SSO, observability, and security best practices. The main "code" consists of:
- Shell scripts (`*.sh`) for automation
- YAML configuration files (`*.yml`, `*.yaml`) for Docker Compose, Traefik, Grafana, etc.
- Environment files (`.env`)

## Commands

### Development Workflow

```bash
# Start the full stack (including monitoring)
docker compose --profile monitoring up -d

# Start only core services (no monitoring)
docker compose up -d

# Start with GPU support
docker compose --profile gpu up -d

# View logs
docker compose logs -f [service_name]

# Stop all services
docker compose down

# Run setup script to generate .env
./scripts/setup.sh
```

### Keycloak Commands

```bash
# Configure Keycloak OIDC clients (after stack is running)
./scripts/configure-keycloak-clients.sh

# Access Keycloak admin console
# URL: https://auth.<domain>/admin
# Credentials: admin / $KEYCLOAK_ADMIN_PASSWORD

# Import realm on startup
# Place realm-export.json in keycloak/import/ directory
```

### Backup Commands

```bash
# Run Keycloak database backup
docker compose --profile backup up keycloak-backup

# Schedule automated backups (add to crontab)
# 0 2 * * * cd /path/to/project && docker compose --profile backup up -d keycloak-backup
```

### Linting & Quality Checks

**Shell Scripts:**
```bash
shellcheck scripts/*.sh
shfmt -i 4 -w scripts/*.sh
```

**YAML Validation:**
```bash
docker compose config --quiet && echo "Valid"
```

### Testing Changes

```bash
docker compose config
docker compose up --dry-run
docker compose pull
```

## Code Style Guidelines

### General Principles
1. **Configuration over Code**: Prefer declarative YAML configs over custom scripts.
2. **Idempotency**: Scripts must be re-runnable without side effects.
3. **Security First**: Never commit secrets. Use `.env` files and `${VAR:?error}` for required variables.

### YAML Configuration

- Use 2-space indentation
- Use anchors (`&anchor`) and aliases (`*alias`) to reduce duplication
- Include section headers with `====` for major sections
- Always define `logging` configuration using anchors
- Use `healthcheck` for all persistent services
- Use `profiles` for optional features (monitoring, gpu, backup)
- Set `security_opt` with `no-new-privileges:true`
- Use internal networks for backend services
- Add `deploy.resources` for production services (memory/CPU limits)

### Environment Variables

**Naming Convention:**
- Use SCREAMING_SNAKE_CASE
- Prefix with service name (e.g., `OPENWEBUI_`, `LITELLM_`)
- Suffix passwords with `_PASSWORD`, keys with `_KEY`

**Security:**
- Use `${VAR:?error}` for required variables (fails if missing)
- Provide defaults only for optional: `${VAR:-default}`

### Shell Scripts

**Formatting:**
- Use `#!/bin/bash` (not `/bin/sh`)
- 4-space indentation
- Use `set -euo pipefail` for error handling
- Use `[[ ]]` for tests (not `[ ]`)
- Quote all variable expansions: `"$VAR"`

**Best Practices:**
- Use `local` for all function variables
- Use `readonly` for constants
- Cross-platform compatibility (macOS vs Linux): use helper functions for `sed -i`

### Error Handling

- Docker Compose: Use `condition: service_healthy` for service dependencies
- Shell scripts: Always use `set -euo pipefail`
- Validate required environment variables at startup

## File Structure

```
.
├── AGENTS.md
├── docker-compose.yml
├── .env.example
├── .env (never commit)
├── traefik/
│   ├── traefik.yml
│   └── dynamic/
├── grafana/provisioning/
├── litellm/config.yaml
├── otel/config.yaml
├── qdrant/config.yaml
├── keycloak/
│   └── import/          # Realm import files
└── scripts/
    ├── setup.sh
    ├── generate-certs.sh
    ├── configure-keycloak-clients.sh
    ├── backup-keycloak.sh
    └── sync-ollama-models.sh
```

## Common Tasks

### Adding a New Service
1. Add service to `docker-compose.yml` with labels, networks, healthcheck
2. Add required environment variables to `.env.example`
3. Update README.md with service documentation

### Updating an Image Version
1. Check version in `docker-compose.yml`
2. Test with `docker compose up -d [service]`
3. Verify healthcheck passes

### Modifying Configuration
1. Make changes to appropriate YAML file
2. Validate: `docker compose config`
3. Test: `docker compose up -d`
4. Verify service health

## Security Notes

- Never commit `.env` files or secrets
- Use strong, randomly generated passwords (see `scripts/setup.sh`)
- Use `readonly` volumes where possible
- Restrict network access using internal networks
- Configure admin IP restriction for Keycloak via `KEYCLOAK_ADMIN_IP_RANGE`
- Use OIDC for authentication instead of service-specific credentials