"""Exit 0 once the Ray cluster has at least --expect alive nodes.

tasks/services.yml pipes this file to the Python of the Ray head container
(`<cli> exec -i <name>-ray python - --expect N`) and retries until it
exits 0: `vllm serve` can only place its pipeline stages once every node
has registered. Any failure, including a head that is not up yet, exits
non-zero and is retried the same way.
"""

from __future__ import annotations

import argparse
import sys

import ray


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--expect", type=int, required=True, help="number of alive nodes to wait for")
    args = parser.parse_args(argv)

    ray.init(address="auto")
    alive = sum(1 for node in ray.nodes() if node["Alive"])
    print(f"{alive} of {args.expect} Ray nodes alive")
    return 0 if alive >= args.expect else 1


if __name__ == "__main__":
    sys.exit(main())
