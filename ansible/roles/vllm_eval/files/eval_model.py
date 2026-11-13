#!/usr/bin/env python3
"""Reproducible model evaluation through the LiteLLM gateway.

Runs the tests used in the Qwen3-Coder-30B vs Gemma-4-26B-A4B comparison
(2026-11-03), so future models can be measured the same way:

  1. long-prompt  - N tokens of filler context (proves the deployed window;
                    the original failure was 16385 tokens vs a 16384 window)
  2. coding       - short code-gen prompt (quality smoke test)
  3. vision       - red/white split PNG, "which color is the left half"
                    (skipped unless the model is multimodal)
  4. concurrency  - N parallel coding prompts (verifies the KV capacity line
                    holds in practice, not just in the startup log)

Usage (run by the vllm_eval role; see defaults for the env contract):
  eval_model.py --endpoint URL --model NAME --out FILE
                 [--long-tokens N] [--concurrency N] [--vision]
                 [--timeout S]
The auth key is read from VLLM_EVAL_KEY.
"""
import argparse
import base64
import concurrent.futures
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib

FILLER = (
    "def process_record(record_id, payload): validates and stores the record. "
)
# Measured tokenization of FILLER (Gemma/Qwen tokenizers agree within a few
# percent): ~12 tokens. Used to size the long-prompt test - the earlier
# assumption of 8 caused a 30000-token request to actually send ~68K and
# fail against a healthy 64K window.
FILLER_TOKENS = 12
PROMPT_CODE = (
    "Write a Python function that safely reads a JSON file with a size "
    "limit and returns None on any error. Keep it short."
)


def chat(endpoint, key, body, timeout):
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    resp = json.load(urllib.request.urlopen(req, timeout=timeout))
    dt = time.monotonic() - t0
    return resp, dt


def red_white_png_b64():
    w = h = 64
    rows = b""
    for _ in range(h):
        row = bytearray([0])
        for x in range(w):
            row += bytes([255, 0, 0] if x < 32 else [255, 255, 255])
        rows += bytes(row)

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows))
           + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


def run(endpoint, key, model, args):
    results = {"model": model, "tests": {}}
    ok = True

    # 1. long prompt
    if args.long_tokens > 0:
        n = max(1, args.long_tokens // FILLER_TOKENS)
        content = ("Context: a Python module for inventory. "
                   + FILLER * n
                   + " Question: in one short sentence, what does this module do?")
        try:
            resp, dt = chat(endpoint, key, {
                "model": model, "max_tokens": 60,
                "messages": [{"role": "user", "content": content}]}, args.timeout)
            pt = resp["usage"]["prompt_tokens"]
            results["tests"]["long_prompt"] = {
                "status": "pass",
                "prompt_tokens": pt,
                "latency_s": round(dt, 1),
                "answer": resp["choices"][0]["message"]["content"][:120],
            }
            print(f"long-prompt: PASS ({pt} prompt tokens, {dt:.1f}s)")
        except urllib.error.HTTPError as e:
            ok = False
            body = e.read().decode()[:200]
            results["tests"]["long_prompt"] = {"status": "fail", "error": body}
            print(f"long-prompt: FAIL ({body[:120]})")

    # 2. coding smoke test
    try:
        resp, dt = chat(endpoint, key, {
            "model": model, "max_tokens": 150,
            "messages": [{"role": "user", "content": PROMPT_CODE}]}, args.timeout)
        ans = resp["choices"][0]["message"]["content"]
        scored = ("def" in ans and "json" in ans.lower())
        results["tests"]["coding"] = {
            "status": "pass" if scored else "weak",
            "latency_s": round(dt, 1),
            "contains_def_json": scored,
            "answer_head": ans[:200],
        }
        print(f"coding: {'PASS' if scored else 'WEAK'} ({dt:.1f}s)")
        if not scored:
            ok = False
    except urllib.error.HTTPError as e:
        ok = False
        results["tests"]["coding"] = {"status": "fail",
                                      "error": e.read().decode()[:200]}
        print("coding: FAIL")

    # 3. vision
    if args.vision:
        try:
            resp, dt = chat(endpoint, key, {
                "model": model, "max_tokens": 60,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64,"
                                          + red_white_png_b64()}},
                    {"type": "text", "text": "This image has two vertical "
                     "color regions. What color is the LEFT half? Answer in "
                     "a few words."}]}]}, args.timeout)
            ans = resp["choices"][0]["message"]["content"]
            scored = "red" in ans.lower()
            results["tests"]["vision"] = {
                "status": "pass" if scored else "fail",
                "latency_s": round(dt, 1),
                "answer": ans[:120],
            }
            print(f"vision: {'PASS' if scored else 'FAIL'} ({dt:.1f}s)")
        except urllib.error.HTTPError as e:
            # A text-only model rejects image content: record as skipped,
            # not a failure.
            body = e.read().decode()[:200]
            supported = "image" not in body.lower()
            results["tests"]["vision"] = {
                "status": "skipped" if supported else "fail", "error": body,
            }
            print(f"vision: {'SKIPPED (not multimodal)' if supported else 'FAIL'}")

    # 4. concurrency
    if args.concurrency > 0:
        t0 = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(args.concurrency) as ex:
            futs = [ex.submit(chat, endpoint, key, {
                "model": model, "max_tokens": 60,
                "messages": [{"role": "user", "content": PROMPT_CODE}]},
                args.timeout) for _ in range(args.concurrency)]
            errors = 0
            for f in futs:
                try:
                    f.result()
                except Exception:
                    errors += 1
        dt = time.monotonic() - t0
        status = "pass" if errors == 0 else "fail"
        results["tests"]["concurrency"] = {
            "status": status, "parallel": args.concurrency,
            "errors": errors, "wall_s": round(dt, 1),
        }
        print(f"concurrency: {'PASS' if errors == 0 else 'FAIL'} "
              f"({args.concurrency} parallel, {dt:.1f}s, {errors} errors)")
        if errors:
            ok = False

    results["overall"] = "pass" if ok else "fail"
    return results


def write_results(path, results):
    # 0644 so a non-root controller user can read the file back (the role
    # slurps it after the run).
    with open(path, "w") as f:
        json.dump(results, f, indent=2)
    os.chmod(path, 0o644)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--long-tokens", type=int, default=30000)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--vision", action="store_true")
    p.add_argument("--timeout", type=int, default=300)
    args = p.parse_args()

    key = os.environ.get("VLLM_EVAL_KEY")
    if not key:
        sys.exit("VLLM_EVAL_KEY not set")
    results = run(args.endpoint, key, args.model, args)
    write_results(args.out, results)
    print(f"overall: {results['overall'].upper()}")
    sys.exit(0 if results["overall"] == "pass" else 1)


if __name__ == "__main__":
    main()
