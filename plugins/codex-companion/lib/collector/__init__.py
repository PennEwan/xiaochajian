"""Independent collector: records Codex sessions even when the plugin is gone.

The plugin is UI only; this package runs as its own installed component (a login-triggered
scheduled task) and reads the rollout files Codex writes itself, so it needs neither the
plugin's hooks nor a running Codex process.
"""
from __future__ import annotations

__version__ = "1.1.0"

SCHEMA_BATCH = "companion.sync.batch/v1"
SCHEMA_EVENT = "comp.event/v1"
