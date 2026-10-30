"""Uploader log for troubleshooting. Only fixed identifiers and numbers — never free text.

A line looks like `2026-10-28T12:00:00Z upload ok status=200 n=37 ms=84`. There is no
parameter that could carry event content, a token or the backend address.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 256 * 1024
_IDENT = re.compile(r"^[a-z_]{1,32}$")


def write(path: Path, op: str, outcome: str, status: int | None = None, n: int | None = None,
          ms: int | None = None) -> None:
    if not (_IDENT.match(op) and _IDENT.match(outcome)):
        raise ValueError("log fields must be fixed identifiers")
    parts = [datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), op, outcome]
    for name, value in (("status", status), ("n", n), ("ms", ms)):
        if isinstance(value, int):
            parts.append(f"{name}={value}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_BYTES:
            keep = path.read_bytes()[-MAX_BYTES // 2:]
            path.write_bytes(keep[keep.find(b"\n") + 1:])
        with open(path, "a", encoding="utf-8") as f:
            f.write(" ".join(parts) + "\n")
    except OSError:
        pass
