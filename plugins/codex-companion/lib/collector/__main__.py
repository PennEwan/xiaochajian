"""`python -m collector …` — see `cli.py` for the subcommands (run / status / task / mcp)."""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
