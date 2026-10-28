#!/usr/bin/env python3
import http.client
import json
import os
import pathlib
import socket
import time
import urllib.parse

dest = pathlib.Path(os.environ.get("LOG_LINK_DIR", "/var/log/containers"))
sock_path = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
dest.mkdir(parents=True, exist_ok=True)
offsets = {}


class DockerSocket(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.path)


def docker_json(method, path):
    conn = DockerSocket(sock_path)
    conn.request(method, path)
    response = conn.getresponse()
    body = response.read()
    status = response.status
    conn.close()
    if status != 200:
        raise RuntimeError(f"docker socket returned {status} for {path}")
    return json.loads(body)


while True:
    containers = docker_json(
        "GET",
        "/containers/json?filters="
        + urllib.parse.quote(json.dumps({"status": ["running"]})),
    )
    wanted = set()
    for container in containers:
        name = (container.get("Names") or ["/unknown"])[0].lstrip("/").replace("/", "_")
        container_id = container["Id"]
        path = dest / f"{name}.log"
        wanted.add(path.name)
        since = offsets.get(container_id, int(time.time()))
        logs = DockerSocket(sock_path)
        query = urllib.parse.urlencode(
            {
                "stdout": "true",
                "stderr": "true",
                "timestamps": "true",
                "since": str(since),
            }
        )
        logs.request("GET", f"/containers/{container_id}/logs?{query}")
        response = logs.getresponse()
        if response.status != 200:
            response.read()
            logs.close()
            continue
        with path.open("a", encoding="utf-8") as handle:
            while True:
                header = response.read(8)
                if len(header) < 8:
                    break
                size = int.from_bytes(header[4:8], "big")
                payload = response.read(size).decode("utf-8", "replace").rstrip("\n")
                stream = "stderr" if header[0] == 2 else "stdout"
                record = {
                    "log": payload + "\n",
                    "stream": stream,
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime()),
                }
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        logs.close()
        offsets[container_id] = int(time.time())
    for stale in offsets.keys() - {container["Id"] for container in containers}:
        offsets.pop(stale, None)
    for path in dest.glob("*.log"):
        if path.name not in wanted and path.is_symlink():
            path.unlink()
    time.sleep(5)
