#!/bin/bash
# =============================================================================
# Open WebUI Stack - Complete Reset
# =============================================================================
# Destroys the deployed stack on the Photon host so the playbook can be
# retested from scratch:
#
#   1. docker compose down -v --remove-orphans (all profiles), falling back
#      to plain `docker compose down -v` when the deployed docker-compose.yml
#      no longer parses (e.g. half-rendered template).
#   2. Every docker volume, network, container, and image carrying the
#      compose project prefix (${COMPOSE_PROJECT_NAME:-openwebui-stack}).
#   3. The deployed tree only (default /mnt/data1/openwebui-stack, which is
#      the playbook's project_root). Everything else under /mnt/data1 is
#      left alone: other data there (backups, stashes, Docker's data-root
#      at /mnt/data1/docker, old trees) is not part of this stack's lifecycle.
#
# Run ON the Photon host (root), from anywhere:
#   bash scripts/reset-stack.sh            # destroy stack + deployed tree
#   bash scripts/reset-stack.sh --dry-run   # show what would be removed
#
# The script never touches the git checkout or the vault file; after a reset,
# rerun the playbook (scripts/setup.sh) to rebuild the rendered tree. Keycloak
# users, Open WebUI chats, uploaded files, and generated models are all lost.

set -euo pipefail

readonly COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-openwebui-stack}"
readonly DEFAULT_DEPLOY_DIR="/mnt/data1/openwebui-stack"

DEPLOY_DIR="$DEFAULT_DEPLOY_DIR"
DRY_RUN="false"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
die() {
    echo "ERROR: $*" >&2
    exit 1
}

run() {
    if [[ $DRY_RUN == "true" ]]; then
        log "[dry-run] $*"
    else
        "$@"
    fi
}

usage() {
    cat <<EOF
Usage: bash scripts/reset-stack.sh [--dry-run] [--dir PATH]

  --dir PATH   Deployed tree to delete (default: $DEFAULT_DEPLOY_DIR).
               Only this directory is removed; anything else under
               /mnt/data1 (backups, stashes, Docker's data-root) survives.
  --dry-run    Print every destructive action instead of executing it
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
    --dir)
        [[ $# -ge 2 ]] || die "--dir needs a path"
        DEPLOY_DIR="$2"
        shift 2
        ;;
    --dry-run)
        DRY_RUN="true"
        shift
        ;;
    -h | --help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        die "unknown argument: $1"
        ;;
    esac
done

[[ $EUID -eq 0 ]] || die "must run as root on the Photon host (docker + /mnt/data1 access)"
command -v docker >/dev/null 2>&1 || die "docker not found on this host"

# The deployed compose file is the only source for the service set; refuse to
# reset from a path that was never deployed (would silently no-op the compose
# step and still delete the tree).
if [[ ! -f "$DEPLOY_DIR/docker-compose.yml" ]]; then
    log "WARNING: no docker-compose.yml under $DEPLOY_DIR;"
    log "the compose down step will be skipped, but prefix cleanup below"
    log "still removes every container/volume/network of project '$COMPOSE_PROJECT'."
fi

# ---------------------------------------------------------------------------
# 1. Compose down (all profiles, anonymous volumes, orphans)
# ---------------------------------------------------------------------------
if [[ -f "$DEPLOY_DIR/docker-compose.yml" ]]; then
    log "Stopping the stack (all profiles, volumes, orphans)..."
    if [[ $DRY_RUN == "true" ]]; then
        log "[dry-run] docker compose --profile '*' -p $COMPOSE_PROJECT -f $DEPLOY_DIR/docker-compose.yml down -v --remove-orphans"
    else
        if ! docker compose --profile '*' -p "$COMPOSE_PROJECT" -f "$DEPLOY_DIR/docker-compose.yml" \
            down -v --remove-orphans; then
            log "WARNING: full-profile down failed (rendered file may be broken); trying plain down -v"
            docker compose -p "$COMPOSE_PROJECT" -f "$DEPLOY_DIR/docker-compose.yml" \
                down -v --remove-orphans || log "WARNING: compose down failed; continuing with prefix cleanup"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 2. Prefix sweep (catches leftovers compose did not know about)
# ---------------------------------------------------------------------------
mapfile -t PROJECT_CONTAINERS < <(docker ps -aq --filter "name=^${COMPOSE_PROJECT}_" 2>/dev/null || true)
if [[ ${#PROJECT_CONTAINERS[@]} -gt 0 ]]; then
    log "Removing ${#PROJECT_CONTAINERS[@]} leftover container(s) with prefix '${COMPOSE_PROJECT}_'..."
    [[ $DRY_RUN == "true" ]] && log "[dry-run] docker rm -f ${PROJECT_CONTAINERS[*]}"
    [[ $DRY_RUN == "true" ]] || docker rm -f "${PROJECT_CONTAINERS[@]}" >/dev/null || true
fi

mapfile -t PROJECT_VOLUMES < <(docker volume ls -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" 2>/dev/null || true)
if [[ ${#PROJECT_VOLUMES[@]} -eq 0 ]]; then
    # Volumes created before the compose project label was applied (or by a
    # different tool) are still matched by the ${project}_ name pattern used
    # by this stack's compose file (name: ${COMPOSE_PROJECT_NAME:-...}_...).
    mapfile -t PROJECT_VOLUMES < <(docker volume ls -q 2>/dev/null | grep "^${COMPOSE_PROJECT}_" || true)
fi
if [[ ${#PROJECT_VOLUMES[@]} -gt 0 ]]; then
    log "Removing ${#PROJECT_VOLUMES[@]} volume(s) of project '$COMPOSE_PROJECT'..."
    [[ $DRY_RUN == "true" ]] && log "[dry-run] docker volume rm -f ${PROJECT_VOLUMES[*]}"
    [[ $DRY_RUN == "true" ]] || docker volume rm -f "${PROJECT_VOLUMES[@]}" >/dev/null || true
fi

mapfile -t PROJECT_NETWORKS < <(docker network ls --format '{{.Name}}' 2>/dev/null | grep "^${COMPOSE_PROJECT}_" || true)
if [[ ${#PROJECT_NETWORKS[@]} -gt 0 ]]; then
    log "Removing ${#PROJECT_NETWORKS[@]} network(s) with prefix '${COMPOSE_PROJECT}_'..."
    [[ $DRY_RUN == "true" ]] && log "[dry-run] docker network rm ${PROJECT_NETWORKS[*]}"
    [[ $DRY_RUN == "true" ]] || docker network rm "${PROJECT_NETWORKS[@]}" >/dev/null || true
fi

mapfile -t PROJECT_IMAGES < <(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep "^${COMPOSE_PROJECT}/" || true)
if [[ ${#PROJECT_IMAGES[@]} -gt 0 ]]; then
    log "Removing ${#PROJECT_IMAGES[@]} locally built image(s) in '${COMPOSE_PROJECT}/'..."
    [[ $DRY_RUN == "true" ]] && log "[dry-run] docker rmi -f ${PROJECT_IMAGES[*]}"
    [[ $DRY_RUN == "true" ]] || docker rmi -f "${PROJECT_IMAGES[@]}" >/dev/null || true
fi

# ---------------------------------------------------------------------------
# 3. Delete the deployed tree (only the stack folder; /mnt/data1 keeps its
#    other contents - backups, stashes, and Docker's data-root)
# ---------------------------------------------------------------------------
TARGET="$DEPLOY_DIR"

case "$TARGET" in
/ | /bin | /boot | /dev | /etc | /home | /lib | /media | /mnt | /opt | /proc | /root | /run | /sbin | /srv | /sys | /tmp | /usr | /var)
    die "refusing to delete system path: $TARGET"
    ;;
esac
[[ $TARGET == /*/* ]] || die "refusing unsafe relative target: '$TARGET' (must be an absolute path with at least two components)"
[[ $TARGET != "/mnt/data1" ]] || die "refusing to wipe the whole deployment area; pass the stack folder itself"

if [[ -e $TARGET ]]; then
    log "Deleting $TARGET..."
    run rm -rf --one-file-system "$TARGET"
else
    log "Nothing to delete at $TARGET (already clean)."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
if [[ $DRY_RUN == "true" ]]; then
    log "Dry run complete. Rerun without --dry-run to perform the reset."
else
    log "Reset complete."
    log "Rerun the playbook to redeploy from scratch:"
    log "  ./scripts/setup.sh   (or: cd ansible && ansible-playbook -i inventory/hosts.yml -i inventory/hosts.local.yml playbooks/site.yml)"
fi
