"""Package the CDK recharge plugin as the folder + zip a user unzips and double-clicks.

    python packaging\\..\\scripts\\package_native.py     (no arguments)

Reads the version from the plugin manifest, so the folder name, `install.cmd` text and the zip
can never disagree with the plugin they ship. Output:

    dist\\CDKRecharge-<version>\\            marketplace.json + plugin tree + install.cmd/uninstall.cmd
    dist\\CDKRecharge-<version>.zip          the same tree, for handing to someone
    dist\\CDKRecharge-<version>.zip.sha256

`packaging/native/install.cmd` and `uninstall.cmd` are the templates; `__VERSION__` in
`install.cmd` is replaced. Nothing here touches the installed plugin or ~/.codex: installing is
`install.cmd` (marketplace add + plugin add) and is always a user action.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN_SRC = REPO / "plugins" / "codex-native-assist"
TEMPLATES = REPO / "packaging" / "native"
DIST = REPO / "dist"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.tmp", "browser-extension")


def version() -> str:
    manifest = json.loads((PLUGIN_SRC / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    return manifest["version"]


def stage(ver: str) -> Path:
    out = DIST / f"CDKRecharge-{ver}"
    if out.exists():
        shutil.rmtree(out)
    (out / ".agents" / "plugins").mkdir(parents=True)
    (out / ".agents" / "plugins" / "marketplace.json").write_text(json.dumps({
        "name": "native-assist",
        "interface": {"displayName": "CDK 充值"},
        "plugins": [{"name": "codex-native-assist",
                     "source": {"source": "local", "path": "./plugins/codex-native-assist"},
                     "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                     "category": "Productivity"}],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copytree(PLUGIN_SRC, out / "plugins" / "codex-native-assist", ignore=IGNORE)
    install = (TEMPLATES / "install.cmd").read_text(encoding="ascii").replace("__VERSION__", ver)
    (out / "install.cmd").write_text(install, encoding="ascii", newline="\r\n")
    uninstall = (TEMPLATES / "uninstall.cmd").read_text(encoding="ascii")
    (out / "uninstall.cmd").write_text(uninstall, encoding="ascii", newline="\r\n")
    return out


def zip_tree(tree: Path, ver: str) -> Path:
    target = DIST / f"CDKRecharge-{ver}.zip"
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(tree.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(tree).as_posix())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_name(target.name + ".sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    return target


def main() -> int:
    ver = version()
    tree = stage(ver)
    files = [p for p in tree.rglob("*") if p.is_file()]
    size = sum(p.stat().st_size for p in files)
    print(f"staged {tree}  ({len(files)} files, {size / 1024:.0f} KB)")
    for path in sorted(files):
        print(f"  {path.relative_to(tree).as_posix():<62} {path.stat().st_size:>8}")
    archive = zip_tree(tree, ver)
    print(f"{archive}\n  {archive.stat().st_size / 1024:.0f} KB  sha256 {hashlib.sha256(archive.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
