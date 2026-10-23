#!/bin/sh
# Symlinks each container json log to /var/log/containers/<name>.log.
# filelog reads that directory so Loki gets container_name.

set -eu

dest="${1:-/var/log/containers}"
socket="${DOCKER_SOCKET:-/var/run/docker.sock}"
mkdir -p "$dest"

while true; do
	containers="$(curl --silent --fail --unix-socket "$socket" \
		--get "http://localhost/containers/json" \
		--data-urlencode "filters={\"status\":[\"running\"]}")"

	python3 - "$dest" "$containers" <<'PY'
import json
import os
import pathlib
import sys

dest = pathlib.Path(sys.argv[1])
containers = json.loads(sys.argv[2])
wanted = set()
for container in containers:
    name = (container.get("Names") or ["/unknown"])[0].lstrip("/").replace("/", "_")
    container_id = container["Id"]
    source = pathlib.Path(
        f"/var/lib/docker/containers/{container_id}/{container_id}-json.log"
    )
    link = dest / f"{name}.log"
    wanted.add(link.name)
    current = os.readlink(link) if link.is_symlink() else None
    if current != str(source):
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(source)
for path in dest.glob("*.log"):
    if path.name not in wanted and path.is_symlink():
        path.unlink()
PY
	sleep 15
done
