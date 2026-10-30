"""Data sync: upload recorded events to the sync backend, only with separate consent.

Hooks never touch the network. They append to the session files (the queue) and start
the uploader process (`python -m ctr.sync`) when it is not running. See
docs/superpowers/specs/2026-10-28-data-sync-design.md and docs/sync-api.md.
"""
