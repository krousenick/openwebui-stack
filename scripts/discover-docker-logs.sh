#!/bin/sh
# Symlinks each container json log to /var/log/containers/<name>.log.
# filelog reads that directory so Loki gets container_name.

set -eu

dest="${1:-/var/log/containers}"
socket="${DOCKER_SOCKET:-/var/run/docker.sock}"
mkdir -p "$dest"

python3 - "$dest" "$socket" <<'PY'
import http.client
import json
import os
import pathlib
import socket
import sys
import time

dest = pathlib.Path(sys.argv[1])
sock_path = sys.argv[2]


class DockerSocket(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.path)


while True:
    conn = DockerSocket(sock_path)
    conn.request("GET", "/containers/json?filters=" + json.dumps({"status": ["running"]}))
    response = conn.getresponse()
    body = response.read()
    conn.close()
    if response.status != 200:
        raise SystemExit(f"docker socket returned {response.status}")
    containers = json.loads(body)
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
    time.sleep(15)
PY
