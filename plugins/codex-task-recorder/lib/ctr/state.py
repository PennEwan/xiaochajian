"""Consent state: whether recording is on, and the windows in which it was on.

Missing state file  -> the user never consented (load() returns None).
Corrupt state file  -> treated as recording off (fail closed).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import store, timeutil

CONSENT_VERSION = 1


@dataclass
class Window:
    start: str
    end: str | None = None


@dataclass
class State:
    enabled: bool = False
    consent_accepted_at: str | None = None
    consent_version: int | None = None
    scope_cwds: list[str] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    install_dir: str | None = None
    corrupt: bool = False


def load(path: Path) -> State | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return State(corrupt=True)
    try:
        doc = json.loads(raw)
        return State(
            enabled=doc.get("enabled") is True,
            consent_accepted_at=doc.get("consent_accepted_at"),
            consent_version=doc.get("consent_version"),
            scope_cwds=[str(c) for c in doc.get("scope_cwds") or []],
            windows=[Window(w["start"], w.get("end")) for w in doc.get("windows") or []],
            install_dir=doc.get("install_dir"),
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return State(corrupt=True)


def save(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = asdict(state)
    doc.pop("corrupt", None)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    store.replace(tmp, path)


def enable(path: Path, now: datetime, scope_cwds: list[str] | None = None,
           install_dir: str | None = None) -> State:
    state = load(path)
    if state is None or state.corrupt:
        state = State()
    stamp = timeutil.iso(now)
    if state.consent_version != CONSENT_VERSION:
        state.consent_accepted_at = stamp
        state.consent_version = CONSENT_VERSION
    if scope_cwds is not None:
        state.scope_cwds = list(scope_cwds)
    if install_dir is not None:
        state.install_dir = install_dir
    if not state.windows or state.windows[-1].end is not None:
        state.windows.append(Window(start=stamp))
    state.enabled = True
    save(path, state)
    return state


def disable(path: Path, now: datetime) -> State | None:
    state = load(path)
    if state is None:
        return None
    if state.corrupt:
        state = State()
    if state.windows and state.windows[-1].end is None:
        state.windows[-1].end = timeutil.iso(now)
    state.enabled = False
    save(path, state)
    return state


def is_recording(state: State | None) -> bool:
    return bool(state and state.enabled and not state.corrupt and state.consent_version)


def in_window(state: State, ts: object) -> bool:
    moment = ts if isinstance(ts, datetime) else timeutil.parse(ts)
    if moment is None:
        return False
    for w in state.windows:
        start = timeutil.parse(w.start)
        end = timeutil.parse(w.end) if w.end else None
        if start and moment >= start and (end is None or moment <= end):
            return True
    return False


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def cwd_in_scope(state: State, cwd: object) -> bool:
    if not state.scope_cwds:
        return True
    if not isinstance(cwd, str) or not cwd:
        return False
    target = _norm(cwd)
    for root in state.scope_cwds:
        base = _norm(root)
        if target == base or target.startswith(base.rstrip("\\/") + os.sep):
            return True
    return False
