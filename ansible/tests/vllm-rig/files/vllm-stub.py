#!/usr/bin/env python3
# Test rig stub for the vLLM container.
# `vllm start` -> real `ray start` (cluster formation is real).
# `vllm serve` -> HTTP server exposing /health and /v1/models.
import argparse
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def ray_start(argv):
    print("[vllm-stub] delegating to real ray start: " + " ".join(argv), flush=True)
    return subprocess.call(["ray", "start"] + argv)


def vllm_serve(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("model")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--served-model-name", dest="smn", default=None)
    args, _ = ap.parse_known_args(argv)
    smn = args.smn or args.model.rsplit("/", 1)[-1]
    print("[vllm-stub] serving %s on %s:%s" % (smn, args.host, args.port), flush=True)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self._json(b'{"status":"ok"}')
            elif self.path == "/v1/models":
                self._json(('{"object":"list","data":[{"id":"%s","object":"model"}]}' % smn).encode())
            else:
                self.send_response(404)
                self.end_headers()

        def _json(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    HTTPServer((args.host, args.port), H).serve_forever()
    return 0


def main():
    if len(sys.argv) < 2:
        sys.exit(2)
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "start":
        sys.exit(ray_start(rest))
    if cmd == "serve":
        sys.exit(vllm_serve(rest))
    print("[vllm-stub] unknown command %s" % cmd, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
