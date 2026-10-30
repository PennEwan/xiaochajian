"""Operation log: fixed identifiers and numbers only — never prompts, CDKs, tokens, addresses
or e-mail addresses (same rule as cdkr.log and sync.log)."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

_SAFE = re.compile(r"^[a-z0-9_.:-]{1,64}$")
MAX_BYTES = 256 * 1024


def write(path: Path, op: str, outcome: str, **numbers) -> None:
    try:
        parts = [datetime.now().astimezone().isoformat(timespec="seconds"),
                 op if _SAFE.match(op) else "op", outcome if _SAFE.match(outcome) else "other"]
        parts += [f"{k}={v}" for k, v in numbers.items()
                  if _SAFE.match(k) and (isinstance(v, (int, float)) or (isinstance(v, str) and _SAFE.match(v)))]
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_BYTES:
            path.replace(path.with_suffix(".log.1"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(" ".join(parts) + "\n")
    except OSError:
        pass
