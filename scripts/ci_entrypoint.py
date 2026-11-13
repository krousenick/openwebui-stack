import os
import sys

# CI-only entrypoint: fakes LiteLLM enterprise licensing for e2e tests in an
# airgapped network (no license, no phone-home to litellm.ai). Wired in by
# docker-compose.ci.yml; never used in production. Patches the LicenseCheck
# instance and the module-level premium_user flag created at proxy_server
# import time, plus airgapped_license_data so feature gates that read the
# license payload (max_users, allowed_features) see an enterprise deployment.
# Validated against ghcr.io/berriai/litellm:main-latest (digest
# sha256:030121401c60): enterprise worker_registry config loads with this
# entrypoint and raises "must be a LiteLLM Enterprise user" without it. A
# newer image may rename these internals; re-run that check when bumping.
from litellm.proxy import proxy_server


def _mock_is_premium(*args, **kwargs):
    print("[CI] Simulating LiteLLM Enterprise Mode: True")
    return True


proxy_server._license_check.is_premium = _mock_is_premium
proxy_server.premium_user = True

if proxy_server._license_check.airgapped_license_data is None:
    from litellm.proxy._types import EnterpriseLicenseData

    proxy_server._license_check.airgapped_license_data = EnterpriseLicenseData(
        allowed_features=None,
        max_users=None,
        expiration_date="2099-12-31",
    )

from litellm.proxy.proxy_cli import run_server

if __name__ == "__main__":
    run_server()
