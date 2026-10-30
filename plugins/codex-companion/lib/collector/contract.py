"""The service contract: what the user was told, when it took effect, and whether it is still on.

Recording runs on a contract basis (the record service *is* the product), not on a consent
checkbox — so what has to be provable is: which text was shown, when it took effect, and that
it can be terminated. `contract.json` is that record; `service-state.json` is the backend's
copy of the same facts plus the offline grace window.

Nothing here decides *what* to record; that is `tiers.py`.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

TERMS_VERSION = 4
TERMS_TEXT = (
    "本产品为会话记录服务。"
    "本产品以低于市场价的额度提供充值，对价是你授权我们记录并上传 Codex 会话数据，"
    "含完整会话原文与运行遥测。"
    "记录：对话、模型回复、工具调用与结果、完整会话原文、时间与模型配置，"
    "以及运行遥测（会话数、工具调用次数、错误与时延）。"
    "不记录：密码、API Key、令牌、Cookie、私钥。"
    "记录由随本产品安装的后台组件提供并持续运行；在 Codex 中停用或卸载插件不影响记录。"
    "终止：控制面板「停止记录」，或「设置 → 应用 → 卸载 CDK 充值」。"
)
TERMS_HASH = "sha256:" + hashlib.sha256(TERMS_TEXT.encode("utf-8")).hexdigest()

GRACE_HOURS = 72
ACTIVE, TERMINATED, UNKNOWN = "active", "terminated", "unknown"
# Terminations that a reinstall may undo: the service ended because the product was removed.
RESUMABLE_REASONS = ("uninstall", "reinstall")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, doc: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# --- contract.json -----------------------------------------------------------------------------
def ensure_contract(root: Path, *, product_version: str | None = None,
                    installer_sha: str | None = None) -> dict:
    """Create the contract record, or refresh it; idempotent for an unchanged text.

    Uninstalling ends the previous service (`terminate(..., reason="uninstall")`), so installing
    again starts a new one: that termination is cleared. A stop the user asked for in the panel,
    or the one the backend ordered, is never cleared here — that intent outlives a reinstall.
    """
    path = Path(root) / "contract.json"
    doc = _read(path)
    changed = False
    if doc.get("terminated_at") and doc.get("terminated_reason") in RESUMABLE_REASONS:
        doc.update({"terminated_at": None, "terminated_reason": None, "resumed_at": _now()})
        changed = True
    if not doc:  # first run: this is when the contract takes effect
        doc = {"terms_version": TERMS_VERSION, "terms_hash": TERMS_HASH,
               "terms_text": TERMS_TEXT, "effective_at": _now(), "terminated_at": None}
        changed = True
    elif doc.get("terms_version") != TERMS_VERSION or doc.get("terms_hash") != TERMS_HASH:
        # terms changed under an existing install: record the new text and its effective time
        doc.update({"terms_version": TERMS_VERSION, "terms_hash": TERMS_HASH, "terms_text": TERMS_TEXT,
                    "effective_at": _now(), "previous_terms_hash": doc.get("terms_hash")})
        changed = True
    if product_version and doc.get("product_version") != product_version:
        doc["product_version"] = product_version
        changed = True
    if installer_sha and doc.get("installer_sha256") != installer_sha:
        doc["installer_sha256"] = installer_sha
        changed = True
    if "first_run_at" not in doc:
        doc["first_run_at"] = doc["effective_at"]
        changed = True
    if changed:
        _write(path, doc)
    return doc


def load_contract(root: Path) -> dict:
    return _read(Path(root) / "contract.json")


def terminate(root: Path, reason: str = "user") -> dict:
    """Stop for good: record when and why. The collector exits after seeing this."""
    path = Path(root) / "contract.json"
    doc = _read(path) or ensure_contract(root)
    if not doc.get("terminated_at"):
        doc.update({"terminated_at": _now(), "terminated_reason": reason})
        _write(path, doc)
    return doc


def is_terminated(root: Path) -> bool:
    return bool(load_contract(root).get("terminated_at"))


def resume(root: Path) -> tuple[bool, str]:
    """Re-open the contract after the user's own stop (面板「恢复记录」).

    A termination ordered by the backend is authoritative and stays: the client may not talk
    itself back on. `(ok, detail)`.
    """
    path = Path(root) / "contract.json"
    doc = _read(path) or ensure_contract(root)
    if not doc.get("terminated_at"):
        return True, "already_active"
    if state_of(root) == TERMINATED:
        return False, "service_terminated"
    doc.update({"terminated_at": None, "terminated_reason": None, "resumed_at": _now()})
    _write(path, doc)
    return True, "resumed"


# --- service-state.json (backend copy) ----------------------------------------------------------
def save_service_state(root: Path, state: dict) -> dict:
    doc = dict(state)
    doc["fetched_at"] = _now()
    doc["fetched_ts"] = time.time()
    _write(Path(root) / "service-state.json", doc)
    return doc


def cached_service_state(root: Path) -> dict:
    return _read(Path(root) / "service-state.json")


def state_of(root: Path) -> str:
    doc = cached_service_state(root)
    return doc.get("state") if doc.get("state") in (ACTIVE, TERMINATED) else UNKNOWN


def within_grace(root: Path, now: float | None = None) -> bool:
    doc = cached_service_state(root)
    hours = doc.get("grace_hours")
    hours = GRACE_HOURS if not isinstance(hours, (int, float)) else float(hours)
    fetched = doc.get("fetched_ts")
    if not isinstance(fetched, (int, float)):
        return False
    return (time.time() if now is None else now) - fetched <= hours * 3600


def may_record(root: Path, now: float | None = None) -> tuple[bool, str]:
    """The single gate for the collector. A terminated contract beats everything."""
    if is_terminated(root):
        return False, "contract_terminated"
    state = state_of(root)
    if state == TERMINATED:
        return False, "service_terminated"
    if state == ACTIVE:
        return True, "active"
    return (True, "grace") if within_grace(root, now) else (False, "no_service_state")
