"""Uploader process entry point: `pythonw -m ctr.sync` (started by launcher.kick)."""
import sys

from ..paths import Paths
from . import log
from .config import SyncPaths
from .worker import Worker


def main() -> int:
    paths = Paths.default()
    try:
        Worker(paths).run()
    except BaseException:  # noqa: BLE001 - a background process has nobody to report to
        log.write(SyncPaths(paths).log, "worker", "crashed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
