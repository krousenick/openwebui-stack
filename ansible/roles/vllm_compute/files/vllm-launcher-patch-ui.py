"""Fit the launcher's built frontend to the stack.

Runs once during the launcher image build (role: vllm_compute). The
launcher ships a compiled React bundle, so nothing is rebuilt:

  Sign-in chip   two extra files land next to the bundle in static/assets
                 and index.html gains the tags that load them.

  Chat off       unless LAUNCHER_STACK_CHAT is "enabled": index.html gains a
                 rule that hides the Open WebUI panel, and the three places
                 where the launcher tells people to "run a test chat" are
                 reworded to point at Open WebUI. The "Test chat" button has
                 no class of its own, so stack-auth.js hides it by label.
                 This is only the page; stack_entry.py refuses the requests.

Rebuilding the image re-applies everything to a fresh copy of the source,
so there is nothing to undo. Imports nothing beyond the standard library
and the launcher's own one-line package __init__, so it runs on any base
image (and on a FIPS host, where importing the launcher's HTTP stack during
a build can fail).
"""

from __future__ import annotations

import os
import shutil
import sys
from importlib import resources
from pathlib import Path

MARKER = "stack-auth"
TAGS = (
    '    <link rel="stylesheet" href="/assets/stack-auth.css">\n'
    '    <script defer src="/assets/stack-auth.js"></script>\n'
)

CHAT_ENV = "LAUNCHER_STACK_CHAT"
NO_CHAT_MARKER = "stack-no-chat"
# In <head>, so the panel never paints: the script that hides the button
# runs later, after the page has rendered once.
NO_CHAT_STYLE = f'    <style id="{NO_CHAT_MARKER}">.webui-panel {{ display: none !important; }}</style>\n'
# The routes stack_entry.py replaces at start. Checked here as text so a
# launcher version that moved them fails the build, not the first start.
CHAT_ROUTE_DECORATORS = (
    '@router.post("/servers/{server_id}/chat")',
    '@router.post("/openwebui/launch")',
)
# (file glob under the package, old text, new text). Wording only: a miss is
# reported and the build goes on.
CHAT_HINTS = (
    (
        "static/assets/index-*.js",
        "it will appear with its log, status light, and a test chat.",
        "it will appear with its log and status light.",
    ),
    (
        "static/assets/index-*.js",
        "Run a test chat on Servers, then choose Benchmark to save",
        "Send the model a message from Open WebUI, then choose Benchmark on Servers to save",
    ),
    (
        "api.py",
        "Run a test chat, then try Benchmark again.",
        "Send the model a message from Open WebUI, then try Benchmark again.",
    ),
)


def chat_enabled() -> bool:
    return os.environ.get(CHAT_ENV, "").strip().lower() == "enabled"


def install_chip(static: Path, index: Path, html: str) -> str:
    if MARKER not in html:
        html = html.replace("</head>", TAGS + "  </head>", 1)
    assets = static / "assets"
    assets.mkdir(exist_ok=True)
    overlay = Path(__file__).resolve().parent
    for name in ("stack-auth.js", "stack-auth.css"):
        shutil.copyfile(overlay / name, assets / name)
    print(f"patch_ui: sign-in chip installed in {index}")
    return html


def switch_chat_off(package: Path, html: str) -> str | None:
    api_source = (package / "api.py").read_text(encoding="utf-8")
    missing = [decorator for decorator in CHAT_ROUTE_DECORATORS if decorator not in api_source]
    if missing:
        print(
            f"patch_ui: api.py has no {' or '.join(missing)}: this launcher version moved its chat "
            "endpoints. Update vllm-launcher-entry.py and this file for the new paths, or set "
            "vllm_launcher_chat_enabled: true to build it with chat.",
            file=sys.stderr,
        )
        return None

    if NO_CHAT_MARKER not in html:
        html = html.replace("</head>", NO_CHAT_STYLE + "  </head>", 1)

    reworded = 0
    for pattern, old, new in CHAT_HINTS:
        for path in sorted(package.glob(pattern)):
            text = path.read_text(encoding="utf-8")
            if old in text:
                path.write_text(text.replace(old, new), encoding="utf-8")
                reworded += 1
                break
        else:
            print(f"patch_ui: chat hint not found in {pattern} (left as is): {old!r}")
    print(f"patch_ui: chat switched off in the page ({reworded} of {len(CHAT_HINTS)} hints reworded)")
    return html


def main() -> int:
    package = Path(str(resources.files("local_llm_launcher")))
    static = package / "static"
    index = static / "index.html"
    if not index.is_file():
        print(f"patch_ui: {index} not found; the launcher ships no built frontend", file=sys.stderr)
        return 1

    html = index.read_text(encoding="utf-8")
    if "</head>" not in html:
        print("patch_ui: no </head> in index.html; the frontend layout changed", file=sys.stderr)
        return 1

    html = install_chip(static, index, html)
    if not chat_enabled():
        patched = switch_chat_off(package, html)
        if patched is None:
            return 1
        html = patched

    index.write_text(html, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
