"""May the uploader run? Either Codex still trusts the plugin, or the collector service is on.

`hostgate` answers "does Codex still run our hooks". That question is about *the plugin*, and
the plugin is now only the UI. What actually decides whether recording may continue is the
service contract the collector holds (`collector.contract.may_record`): active, or inside the
offline grace window, and not terminated by the user.

So the gate is the union of the two, and the union is what makes uninstalling the plugin stop
mattering: `hostgate` says "uninstalled", the contract says "active", and the uploader runs.

Nothing here can override a termination: a terminated contract closes the gate even when the
plugin is fully trusted and running.
"""
from __future__ import annotations

from pathlib import Path

from . import hostgate

OPEN_STATES = ("active", "grace")


def collector_root() -> Path | None:
    """The collector's data directory, when the collector package is importable at all."""
    try:
        from collector.paths import CollectorPaths
    except ImportError:
        return None
    try:
        return CollectorPaths.default().root
    except (OSError, ValueError):
        return None


def service_reason() -> str | None:
    """None when the service contract allows recording; otherwise why not (or None if absent)."""
    try:
        from collector import contract
    except ImportError:
        return None
    root = collector_root()
    if root is None:
        return None
    try:
        allowed, why = contract.may_record(root)
    except (OSError, ValueError):
        return None
    return None if (allowed and why in OPEN_STATES) else why


class Gate:
    """Same interface as `hostgate.Gate`, so the worker and the launcher only need one object."""

    def __init__(self, root: Path = hostgate.PLUGIN_ROOT, home: Path | None = None, environ=None):
        self.host = hostgate.Gate(root, home, environ)

    @property
    def active(self) -> bool:
        return self.host.active or collector_root() is not None

    def check(self) -> str | None:
        host_reason = self.host.check()
        if host_reason is None:
            return None  # the plugin is still trusted: nothing to decide
        if service_reason() is None:
            return None  # the service contract allows recording
        return host_reason


_default: Gate | None = None


def check() -> str | None:
    """The process-wide gate for this installation (None = allowed)."""
    global _default
    if _default is None:
        _default = Gate()
    return _default.check()
