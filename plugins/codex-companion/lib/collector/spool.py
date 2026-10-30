"""Durable on-disk queue, one directory per tier.

Appends are plain JSON-lines segments of at most 1 MiB, so a crash mid-write loses at most the
last line and never the file. Uploads always read from the head of the oldest segment, and a
confirmed batch is removed by rewriting that segment — an unconfirmed batch is simply retried,
and the backend deduplicates by `event_id`.

Tiers are separate directories because they have separate budgets: when space runs out, L2 is
dropped before L1, and L1 before L0. Metadata is never sacrificed for content.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

TIERS = ("L0", "L1", "L2", "L3")
SEGMENT_MAX_BYTES = 1 << 20
CAPS = {"L0": 300 << 20, "L1": 120 << 20, "L2": 400 << 20, "L3": 900 << 20}
MIN_FREE_BYTES = 200 << 20


@dataclass
class Batch:
    tier: str
    path: Path
    items: list = field(default_factory=list)
    size: int = 0

    @property
    def events(self) -> int:
        return len(self.items)


class Spool:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.dropped = 0

    # --- layout -----------------------------------------------------------------------------
    def dir(self, tier: str) -> Path:
        path = self.root / tier
        path.mkdir(parents=True, exist_ok=True)
        return path

    def segments(self, tier: str) -> list[Path]:
        return sorted(self.dir(tier).glob("seg-*.jsonl"))

    def _next_segment(self, tier: str) -> Path:
        existing = self.segments(tier)
        serial = 1
        if existing:
            try:
                serial = int(existing[-1].stem.split("-")[1]) + 1
            except (IndexError, ValueError):
                serial = len(existing) + 1
        return self.dir(tier) / f"seg-{serial:08d}.jsonl"

    # --- write ------------------------------------------------------------------------------
    def append(self, record: dict, tier: str | None = None) -> None:
        tier = tier or record.get("tier")
        if tier not in TIERS:
            raise ValueError(f"bad tier {tier!r}")
        segments = self.segments(tier)
        target = segments[-1] if segments else None
        if target is None or target.stat().st_size >= SEGMENT_MAX_BYTES:
            target = self._next_segment(tier)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(target, "a", encoding="utf-8", newline="\n") as f:
            f.write(line)

    def append_many(self, records) -> dict:
        n = {tier: 0 for tier in TIERS}
        for rec in records:
            tier = rec.get("tier")
            if tier in n:
                self.append(rec, tier)
                n[tier] += 1
        return n

    # --- read -------------------------------------------------------------------------------
    def next_batch(self, limit_events: int = 200, limit_bytes: int = 1 << 20,
                   tiers=TIERS) -> Batch | None:
        for tier in tiers:
            for path in self.segments(tier):
                items, size = [], 0
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for raw in f:
                            raw = raw.rstrip("\n")
                            if not raw:
                                continue
                            if len(items) >= limit_events or size + len(raw) > limit_bytes:
                                break
                            try:
                                items.append(json.loads(raw))
                            except ValueError:
                                continue  # a torn last line: dropped by the rewrite below
                            size += len(raw)
                except OSError:
                    continue
                if items:
                    return Batch(tier=tier, path=path, items=items, size=size)
        return None

    def commit(self, batch: Batch) -> int:
        """Drop the confirmed prefix of the segment; delete it when nothing is left."""
        try:
            with open(batch.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return 0
        drop = len(batch.items)
        # The uploaded items are the first `drop` *parseable* lines; a torn line is skipped
        # rather than shifted, so count on the parsed content instead of the raw index.
        kept, removed = [], 0
        for raw in lines:
            if removed < drop:
                try:
                    json.loads(raw)
                except ValueError:
                    continue  # torn line: discard with the batch
                removed += 1
                continue
            kept.append(raw)
        tmp = batch.path.with_suffix(".tmp")
        try:
            if kept:
                tmp.write_text("".join(kept), encoding="utf-8", newline="\n")
                os.replace(tmp, batch.path)
            else:
                batch.path.unlink()
        except OSError:
            return removed
        return removed

    # --- accounting -------------------------------------------------------------------------
    def size(self, tier: str | None = None) -> int:
        tiers = (tier,) if tier else TIERS
        total = 0
        for t in tiers:
            for path in self.segments(t):
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total

    def depth(self) -> dict:
        """Records waiting per tier (line counts; segments are small)."""
        out = {}
        for tier in TIERS:
            n = 0
            for path in self.segments(tier):
                try:
                    with open(path, "rb") as f:
                        n += sum(1 for line in f if line.strip())
                except OSError:
                    pass
            out[tier] = n
        return out

    def total_depth(self) -> int:
        return sum(self.depth().values())

    def prune(self) -> dict:
        """Drop whole oldest segments per tier while over budget (or the volume is nearly full)."""
        result = {}
        for tier in TIERS:
            segments = self.segments(tier)
            total = sum(p.stat().st_size for p in segments if p.exists())
            cap = CAPS.get(tier, 0)
            removed = 0
            while segments and (total > cap or _low_disk(self.root)):
                oldest = segments.pop(0)
                try:
                    size = oldest.stat().st_size
                    oldest.unlink()
                    total -= size
                    removed += 1
                except OSError:
                    break
            result[tier] = removed
            self.dropped += removed
        return result

    def purge(self) -> int:
        n = 0
        for tier in TIERS:
            for path in self.segments(tier):
                try:
                    path.unlink()
                    n += 1
                except OSError:
                    pass
        return n


def _low_disk(path: Path) -> bool:
    try:
        import shutil
        return shutil.disk_usage(str(path)).free < MIN_FREE_BYTES
    except OSError:
        return False
