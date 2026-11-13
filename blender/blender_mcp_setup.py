"""One-shot Blender script: configure the MCP for Blender addon preferences.

Runs headless (blender --background --python). Sets the addon preference
bind_address to 0.0.0.0 and allow_remote_connections to True so the listener
started by the addon's auto-start binds all interfaces (the stdio MCP servers
connect from other containers on the backend network, not from loopback), then
persists userpref. Idempotent.
"""

import bpy

ADDON_MODULE = "blender_mcp"

prefs = bpy.context.preferences.addons.get(ADDON_MODULE)
if prefs is None:
    # The setup CLI enables the addon; if this script ever runs before that,
    # enable it here so the preferences section exists.
    import addon_utils

    addon_utils.enable(ADDON_MODULE, default_set=True)
    prefs = bpy.context.preferences.addons.get(ADDON_MODULE)

if prefs is None:
    raise SystemExit(f"Could not enable addon {ADDON_MODULE}; preferences not found")

prefs.preferences.bind_address = "0.0.0.0"
prefs.preferences.allow_remote_connections = True

bpy.ops.wm.save_userpref()
print("blender_mcp preferences set: bind 0.0.0.0, allow_remote_connections=True")
