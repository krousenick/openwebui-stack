# =============================================================================
# Code Sandbox - pinned jupyter/scipy-notebook plus mesh/STL deps
# =============================================================================
# Built by the openwebui_stack role on the deployment host (Photon). The image
# ships numpy/scipy/matplotlib/pandas from the base stack; requirements.txt
# adds numpy-stl and trimesh. The base comes from quay.io, which carries the
# current daily tags; docker hub's jupyter/* mirror is years stale. Tag pins
# the daily stack build (x86_64 Photon host); check
# https://quay.io/repository/jupyter/scipy-notebook for the newest dated tag
# when rebuilding.
FROM quay.io/jupyter/scipy-notebook:x86_64-2026-11-02

COPY --chown=1000:1000 requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --requirement /tmp/requirements.txt

# Kernels are user code; keep the default jovyan (uid 1000) non-root user.
USER ${NB_UID}
