"""Build the Codex 助手包 installer.

    python packaging\\build.py --version 1.0.0 --profile mock
    python packaging\\build.py --version 1.0.0 --recharge-url https://… --sync-url https://… --update-url https://…

Steps: private runtime (cached in build\\runtime-<python>) -> stage the install tree in
build\\stage-<version> (marketplace + plugin: manifest, hooks, lib\\companion + lib\\collector
+ lib\\ctr + lib\\cdkr + lib\\csr, bin\\collector.cmd, runtime, product.json) -> smoke-test it with
its own runtime -> ISCC -> dist\\.

`lib\\collector` is the independent background component (recording keeps running after the plugin
is disabled or removed) and `lib\\csr` is the session reader it parses Codex's rollout files with.
Both sit next to `ctr` in `lib\\`, which is the one directory the private runtime adds to sys.path
(`python312._pth`: `..\\lib`) — so `import collector` / `import csr` work exactly like `import ctr`.

Profiles: `mock` points every backend at the local mock servers (scripts\\mock_*_server.py);
`prod` (default) takes the URLs from the command line and leaves unset ones unconfigured.
Build machine needs CPython 3.12 x64 with Tk and Inno Setup 6 (--iscc or %ISCC%).
"""
from __future__ import annotations

import argparse
import compileall
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
import make_hooks  # noqa: E402
import runtime  # noqa: E402

PLUGINS = REPO / "plugins"
SESSION_READER = REPO / "tools" / "session-reader"
# product.json: where the collector package is and which scheduled task runs it. The task name is
# the one collector.paths.TASK_NAME uses; build.py keeps its own copy so a checkout without the
# collector package still builds.
COLLECTOR_ENTRY = "collector"
COLLECTOR_TASK = "CodexCompanion\\Collector"
COLLECTOR_LAUNCHER = """@echo off
rem Codex Companion collector - written by packaging\\build.py.
rem Started by the per-user logon task "CodexCompanion\\Collector"; runs lib\\collector.
setlocal
set "PLUGIN_ROOT=%~dp0.."
set "PYTHONPATH=%PLUGIN_ROOT%\\lib"
set "PYTHONIOENCODING=utf-8"
set "PY=%PLUGIN_ROOT%\\runtime\\pythonw.exe"
if exist "%PY%" goto launch
set "PY=pythonw"
:launch
"%PY%" -m collector run %*
"""
MOCK = {"recharge": "http://127.0.0.1:18787", "sync": "http://127.0.0.1:18788",
        "update": "http://127.0.0.1:18789/manifest.json"}
COMPAT = {"min_kernel": "0.163.0", "tested_kernels": ["0.163.0-alpha.10.2"], "tested_apps": ["26.1013.4894.0"]}
ISCC_CANDIDATES = [os.environ.get("ISCC"), r"D:\tools\InnoSetup6\ISCC.exe",
                   os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
                   r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe"]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.tmp")
# The wizard's notice page: companion.iss keeps the text in one #define; when the collector package
# is present this line is replaced with collector.contract.TERMS_TEXT, so the page can never show
# something other than the terms the collector records under.
NOTICE_DEFINE = "#define CollectorNoticeText "
CONTRACT_MODULE = PLUGINS / "codex-companion" / "lib" / "collector" / "contract.py"


def step(text: str) -> None:
    print(f"==> {text}", flush=True)


def build_runtime(cache: Path, fresh: bool) -> Path:
    target = cache / f"runtime-{sys.version.split()[0]}"
    if fresh or not (target / "python.exe").exists():
        step(f"building private runtime in {target}")
        info = runtime.build(target)
        print(f"    {info['modules']} modules, {info['bytes'] / 1e6:.1f} MB", flush=True)
    return target


def stage(version: str, urls: dict, runtime_dir: Path, out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    market = out / "marketplace"
    plugin = market / "plugins" / "codex-companion"
    src = PLUGINS / "codex-companion"
    # The shipped plugin has no hooks (MCP only): nothing to verify, nothing to stage. The
    # recorder plugin keeps its own hooks for the development build.
    shutil.copytree(src / ".codex-plugin", plugin / ".codex-plugin", ignore=IGNORE)
    shutil.copytree(src / "lib" / "companion", plugin / "lib" / "companion", ignore=IGNORE)
    shutil.copytree(src / "lib" / "collector", plugin / "lib" / "collector", ignore=IGNORE)
    shutil.copytree(PLUGINS / "codex-task-recorder" / "lib" / "ctr", plugin / "lib" / "ctr", ignore=IGNORE)
    shutil.copytree(PLUGINS / "codex-cdk-recharge" / "lib" / "cdkr", plugin / "lib" / "cdkr", ignore=IGNORE)
    # The collector parses Codex's rollout files with the session reader; a plain copy of the whole
    # package goes next to ctr, so `..\lib` in python312._pth makes `import csr` work as well.
    shutil.copytree(SESSION_READER / "csr", plugin / "lib" / "csr", ignore=IGNORE)
    shutil.copytree(runtime_dir, plugin / "runtime")
    stage_mcp(src, plugin)
    stage_bin(plugin)
    product = {"name": "Codex 助手包", "version": version, "plugin_id": "codex-companion@codex-companion",
               "recharge_base_url": urls.get("recharge"), "sync_base_url": urls.get("sync"),
               "update_manifest_url": urls.get("update"), "has_hooks": False,
               "collector_entry": COLLECTOR_ENTRY,
               "collector_task": COLLECTOR_TASK, "compat": COMPAT}
    (plugin / "product.json").write_text(json.dumps(product, ensure_ascii=False, indent=2), encoding="utf-8")
    (market / ".agents" / "plugins").mkdir(parents=True)
    (market / ".agents" / "plugins" / "marketplace.json").write_text(json.dumps({
        "name": "codex-companion", "interface": {"displayName": "Codex 助手包"},
        "plugins": [{"name": "codex-companion", "source": {"source": "local", "path": "./plugins/codex-companion"},
                     "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                     "category": "Productivity"}]}, ensure_ascii=False, indent=2), encoding="utf-8")
    # Compiled once here: the hook never pays for compiling the plugin's own modules.
    if not compileall.compile_dir(str(plugin / "lib"), quiet=1):
        raise SystemExit("compileall failed")
    return out


def stage_mcp(src: Path, plugin: Path) -> None:
    """Copy `\\.mcp.json`, the MCP tool surface `plugin.json` points at ("mcpServers").

    Copied when it exists; a plugin.json that declares mcpServers without it would install a
    server entry pointing at a file that is not there, so that combination fails the build.
    """
    mcp = src / ".mcp.json"
    if mcp.exists():
        shutil.copy2(mcp, plugin / ".mcp.json")
        return
    try:
        manifest = json.loads((src / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(manifest, dict) and manifest.get("mcpServers"):
        raise SystemExit('.mcp.json is missing: plugin.json declares "mcpServers"')


def stage_bin(plugin: Path) -> None:
    """Write `bin\\collector.cmd`, the file the logon task runs (`collector.tasks.command`).

    A `bin\\` in the source tree wins, so it can be edited there; otherwise the launcher is
    generated here and the staged tree is self-consistent. Without it the task would point at a
    missing file: the collector would silently never start.
    """
    source = PLUGINS / "codex-companion" / "bin"
    if source.is_dir():
        shutil.copytree(source, plugin / "bin", ignore=IGNORE, dirs_exist_ok=True)
    launcher = plugin / "bin" / "collector.cmd"
    if not launcher.exists():
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(COLLECTOR_LAUNCHER, encoding="ascii", newline="\r\n")


def smoke(staged: Path) -> None:
    plugin = staged / "marketplace" / "plugins" / "codex-companion"
    py = plugin / "runtime" / "python.exe"
    with tempfile.TemporaryDirectory() as tmp:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update(COMPANION_DATA_DIR=str(Path(tmp) / "c"), CTR_DATA_DIR=str(Path(tmp) / "r"),
                   CDKR_HOME=str(Path(tmp) / "k"), PYTHONIOENCODING="utf-8")
        checks = [
            ("imports + Tk", [str(py), "-c",
                              "import sys, tkinter, ssl, tomllib, sqlite3, companion.dispatch, companion.panel,"
                              " companion.setup, companion.status, cdkr.window, ctr.sync.worker, collector.contract,"
                              " collector.tasks, collector.mcp,"
                              " csr.reader, csr.storage, csr.scanner, csr.parser; tkinter.Tk().destroy();"
                              " assert sys.path[-1].endswith('lib'), sys.path"], None, ""),
            ("mcp: tools/list", [str(py), "-m", "collector", "mcp"],
             {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "jsonrpc"),
            ("mcp: initialize", [str(py), "-m", "collector", "mcp"],
             {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, "jsonrpc"),
            ("cli", [str(py), "-m", "companion", "update", "mode"], None, "notify"),
        ]
        for name, argv, stdin, expect in checks:
            data = (json.dumps(stdin) if isinstance(stdin, dict) else (stdin or "")).encode("utf-8")
            proc = subprocess.run(argv, input=data, capture_output=True, env=env, timeout=120)
            out = proc.stdout.decode("utf-8", "replace").strip()
            ok = ('"jsonrpc"' in out) if expect == "jsonrpc" else out == expect
            if proc.returncode != 0 or not ok:
                raise SystemExit(f"smoke test '{name}' failed: exit {proc.returncode}, out {out!r}, "
                                 f"err {proc.stderr.decode('utf-8', 'replace')[-800:]}")
            print(f"    ok  {name}", flush=True)
    launcher = plugin / "bin" / "collector.cmd"  # what the logon task runs: must be in the package
    if not launcher.exists():
        raise SystemExit(f"the collector launcher is missing from the staged tree: {launcher}")
    print("    ok  collector launcher", flush=True)


def find_iscc(given: str | None) -> Path:
    for cand in [given, *ISCC_CANDIDATES]:
        if cand and Path(cand).exists():
            return Path(cand)
    raise SystemExit("Inno Setup 6 (ISCC.exe) not found: install it or pass --iscc")


def collector_terms() -> tuple[str, int | None] | None:
    """(terms text, terms version) from the collector's own `contract.py`, or None when absent.

    Imported by path: that module has no package-relative imports and no import-time side effects,
    so the build machine needs neither the plugin on sys.path nor a runtime for it.
    """
    if not CONTRACT_MODULE.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_build_collector_contract", CONTRACT_MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - the checked-in text in companion.iss is the fallback
        return None
    text, version = getattr(module, "TERMS_TEXT", None), getattr(module, "TERMS_VERSION", None)
    if not isinstance(text, str) or not text.strip():
        return None
    return text, version if isinstance(version, int) else None


def ispp_text(text: str) -> str:
    """A whole text as one ISPP string body: escapes for the preprocessor, `%n` for line breaks.

    `%` is doubled because the page text goes through ExpandConstant, which would otherwise read
    `%n` / `%foo` as an Inno constant.
    """
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return "%n".join(text.replace("\r\n", "\n").split("\n"))


def with_notice(script: str, terms: tuple[str, int | None] | None) -> str:
    """Put the shipped terms into the wizard page's `#define` (unchanged without the collector)."""
    if terms is None:
        return script
    line = f'{NOTICE_DEFINE}"{ispp_text(terms[0])}"'
    pattern = rf"(?m)^{re.escape(NOTICE_DEFINE.strip())} .*$"
    if not re.search(pattern, script):
        raise SystemExit(f"companion.iss has no `{NOTICE_DEFINE.strip()}` line to fill in")
    return re.sub(pattern, lambda _m: line, script, count=1)


def compile_installer(iscc: Path, version: str, staged: Path, out_dir: Path, build_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    script = build_dir / "companion.iss"  # ISCC reads UTF-8 only with a BOM
    terms = collector_terms()
    text = with_notice((HERE / "companion.iss").read_text(encoding="utf-8"), terms)
    script.write_text(text, encoding="utf-8-sig")
    if terms:
        print(f"    wizard notice: collector.contract terms v{terms[1]}", flush=True)
    else:
        print("    wizard notice: text checked in companion.iss (no collector package)", flush=True)
    argv = [str(iscc), "/Qp", f"/DAppVersion={version}", f"/DStageDir={staged}", f"/DOutputDir={out_dir}",
            f"/DLangFile={HERE / 'ChineseSimplified.isl'}", str(script)]
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SystemExit(f"ISCC failed ({proc.returncode}):\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}")
    payload = out_dir / f"CodexCompanion-Setup-{version}.exe"
    return wrap_with_launcher(payload, version, out_dir, build_dir)


def wrap_with_launcher(payload: Path, version: str, out_dir: Path, build_dir: Path) -> Path:
    """Ship a launcher as `CodexCompanion-Setup-<ver>.exe`.

    Inno creates a "protected" temp directory under %TEMP% before the wizard appears; on cloud/VHD
    system disks that create is denied and the user sees "Error 5: access denied" with no window.
    The launcher picks a temp location where that actually works (see packaging/setup_launcher.cs)
    and hands it to Setup as /TMP=. One file, double-click, no user action changed.
    """
    source = HERE / "setup_launcher.cs"
    csc = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if not source.is_file() or not csc.is_file():
        print("    launcher: source or csc.exe missing, shipping the plain Inno setup", flush=True)
        return payload
    packed = build_dir / f"setup-payload-{version}.exe"
    shutil.move(str(payload), str(packed))  # keep the raw setup out of dist/
    target = out_dir / f"CodexCompanion-Setup-{version}.exe"
    argv = [str(csc), "/nologo", "/target:winexe", f"/out:{target}",
            "/reference:System.Windows.Forms.dll", "/reference:System.Security.dll",
            f"/resource:{packed},payload.exe", str(source)]
    built = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if built.returncode != 0 or not target.is_file():
        shutil.move(str(packed), str(payload))
        raise SystemExit(f"launcher build failed ({built.returncode}):\n{built.stdout[-2000:]}\n{built.stderr[-2000:]}")
    print(f"    launcher: wrapped {packed.name} -> {target.name}", flush=True)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True)
    parser.add_argument("--profile", choices=["prod", "mock"], default="prod")
    parser.add_argument("--recharge-url")
    parser.add_argument("--sync-url")
    parser.add_argument("--update-url")
    parser.add_argument("--iscc")
    parser.add_argument("--out", default=str(REPO / "dist"))
    parser.add_argument("--fresh-runtime", action="store_true")
    parser.add_argument("--stage-only", action="store_true")
    args = parser.parse_args()
    runtime.check_interpreter()
    urls = dict(MOCK) if args.profile == "mock" else {}
    for key in ("recharge", "sync", "update"):
        if getattr(args, f"{key}_url"):
            urls[key] = getattr(args, f"{key}_url")
    build_dir = REPO / "build"
    rt = build_runtime(build_dir, args.fresh_runtime)
    step(f"staging {args.version} ({args.profile}: {urls or 'no backends'})")
    staged = stage(args.version, urls, rt, build_dir / f"stage-{args.version}")
    step("smoke tests with the bundled runtime")
    smoke(staged)
    if args.stage_only:
        print(staged)
        return 0
    iscc = find_iscc(args.iscc)
    step(f"compiling installer with {iscc}")
    exe = compile_installer(iscc, args.version, staged, Path(args.out), build_dir)
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    exe.with_name(exe.name + ".sha256").write_text(f"{digest}  {exe.name}\n", encoding="ascii")
    print(f"{exe}\n  {exe.stat().st_size / 1e6:.1f} MB  sha256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
