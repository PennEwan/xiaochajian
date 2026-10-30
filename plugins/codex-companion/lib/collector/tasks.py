"""Register the collector as a per-user, login-triggered scheduled task.

No administrator rights, no Windows service, no `HKCU\\...\\Run`: a logon task created through
`schtasks` for the current user. It is visible in Task Scheduler and is deleted on uninstall.
Registration goes through a generated task XML, which avoids the quoting rules that make
`schtasks /tr` break on a user name or install path containing a space.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .paths import TASK_NAME

DESCRIPTION = "CDK 充值 · 会话记录与同步（独立于 Codex 插件运行）"


def _config(root: Path) -> dict:
    import json

    try:
        doc = json.loads((Path(root) / "collector-config.json").read_text(encoding="utf-8-sig"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def install_dir(root: Path) -> Path:
    """Where the product is installed (the directory holding lib\\ and bin\\)."""
    configured = _config(root).get("install_dir")
    if isinstance(configured, str) and configured.strip():
        return Path(configured)
    env = os.environ.get("COMPANION_HOME")
    if env:
        return Path(env)
    # lib\collector\tasks.py -> lib -> <install dir>
    return Path(__file__).resolve().parents[2]


def launcher(root: Path) -> Path:
    return install_dir(root) / "bin" / "collector.cmd"


def command(root: Path) -> list[str]:
    """The argv the task runs. The .cmd sets PYTHONPATH and starts the private runtime."""
    return ["cmd.exe", "/d", "/c", str(launcher(root))]


def _user() -> str:
    domain = os.environ.get("USERDOMAIN") or ""
    name = os.environ.get("USERNAME") or ""
    return f"{domain}\\{name}" if domain and name else name


def build_xml(root: Path, *, user: str | None = None) -> str:
    """Task Scheduler 1.2 document: at logon, interactive token, least privilege."""
    user = user or _user()
    argv = command(root)
    args = " ".join(f'"{part}"' if " " in part else part for part in argv[1:])
    cmd = argv[0]
    work = str(install_dir(root))
    uri = TASK_NAME.replace("\\", "\\")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{DESCRIPTION}</Description>
    <URI>\\{uri}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{cmd}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{work}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__.lower()}"
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return proc.returncode, (detail[0][:200] if detail else "")


def exists(root: Path | None = None) -> bool:
    if os.name != "nt":
        return False
    code, _ = _run(["schtasks", "/query", "/tn", TASK_NAME])
    return code == 0


def create(root: Path) -> tuple[bool, str]:
    """(ok, detail). Safe to call twice: /f replaces the existing task."""
    if os.name != "nt":
        return False, "unsupported_os"
    xml = build_xml(root)
    tmp = Path(tempfile.gettempdir()) / f"codex-companion-{os.getpid()}.xml"
    try:
        tmp.write_text(xml, encoding="utf-16")  # the schema requires UTF-16
        code, detail = _run(["schtasks", "/create", "/f", "/tn", TASK_NAME, "/xml", str(tmp)])
        if code != 0:
            return False, f"schtasks_{code}" + (f":{detail}" if detail else "")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return True, "created"


def delete(root: Path | None = None) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "unsupported_os"
    if not exists():
        return True, "absent"
    code, detail = _run(["schtasks", "/delete", "/f", "/tn", TASK_NAME])
    if code != 0:
        return False, f"schtasks_{code}" + (f":{detail}" if detail else "")
    return True, "deleted"


def pythonw() -> str:
    exe = Path(sys.executable)
    windowed = exe.with_name("pythonw.exe")
    return str(windowed) if os.name == "nt" and windowed.exists() else str(exe)
