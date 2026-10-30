"""Backend for the collector and the recorder: ingest, service state, recharge, ops.

Standard library only, one SQLite file, no external service. `python -m ctrsrv serve` is a
complete deployment for a small fleet; SQLite handles a few hundred devices comfortably, and
the same code runs behind nginx (TLS terminated there).

Two API families live here:
  /v1/...      client-facing (docs/sync-api.md, docs/collector-api.md, docs/recharge-api.md)
  /admin/...   operations only, `Authorization: Bearer <CTRSRV_ADMIN_TOKEN>` (alias:
               `X-Admin-Token`); docs/backend.md
"""
from __future__ import annotations

__version__ = "1.0.0"
