"""Build the private Python runtime that ships inside the plugin (`runtime/`).

It is made from the build machine's own CPython 3.12 x64 — the interpreter the tests run
on — rather than python.org's embeddable zip, which has no Tk (the recharge window and the
control panel need it). The result looks like the embeddable distribution:

    runtime/python.exe, pythonw.exe, python312.dll, python3.dll, vcruntime140*.dll
    runtime/DLLs/*.pyd + their DLLs (Tk, OpenSSL, libffi, SQLite …)
    runtime/python312.zip      the standard library, compiled (.pyc only: no compile at startup)
    runtime/tcl/               Tcl/Tk script libraries
    runtime/python312._pth     python312.zip, DLLs, ..\\lib  — and no `import site`

With a ._pth file Python ignores PYTHONPATH and the registry, so the plugin's processes
see exactly this runtime plus the plugin's own lib directory.
"""
from __future__ import annotations

import os
import py_compile
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

TOP_FILES = ("python.exe", "pythonw.exe", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll",
             "LICENSE.txt")
# Top-level Lib entries nobody in the plugin imports; the test suite alone is ~30 MB.
# `sqlite3` stays: the session reader (csr) and the collector keep their read cursors and the
# spool index in SQLite, and both run inside this runtime.
EXCLUDED_LIB = {"test", "idlelib", "turtledemo", "ensurepip", "venv", "lib2to3", "pydoc_data",
                "site-packages", "unittest", "turtle.py", "__pycache__"}
EXCLUDED_DLLS = {"_ctypes_test.pyd", "_msi.pyd", "winsound.pyd",
                 "py.ico", "pyc.ico", "pyd.ico", "python_lib.cat"}
TCL_KEEP = ("tcl8.6", "tk8.6", "tcl8")
# Time-zone data and the `clock` command's translations (Tk needs neither); Tk's demo programs.
TCL_EXCLUDED = {("tcl8.6", "tzdata"), ("tcl8.6", "msgs"), ("tk8.6", "demos")}


def dll_name() -> str:
    return f"python{sys.version_info.major}{sys.version_info.minor}"


def check_interpreter() -> None:
    if os.name != "nt":
        raise SystemExit("The runtime is built on Windows.")
    if sys.version_info[:2] != (3, 12) or sys.maxsize <= 2 ** 32:
        raise SystemExit(f"Need CPython 3.12 x64 to build the runtime, not {sys.version.split()[0]}.")


def lib_members(lib: Path):
    """(source file, archive name) for every stdlib module that goes into the zip."""
    for path in sorted(lib.rglob("*.py")):
        rel = path.relative_to(lib)
        if rel.parts[0] in EXCLUDED_LIB or "__pycache__" in rel.parts:
            continue
        yield path, rel.with_suffix(".pyc").as_posix()


def _dll_wanted(name: str) -> bool:
    return name not in EXCLUDED_DLLS and not name.startswith("_test")


def _copy_tcl(src: Path, dest: Path) -> None:
    for top in TCL_KEEP:
        if not (src / top).is_dir():
            continue

        def ignore(folder, names, top=top):
            here = Path(folder).relative_to(src / top)
            return [n for n in names if here == Path(".") and (top, n) in TCL_EXCLUDED]

        shutil.copytree(src / top, dest / top, ignore=ignore)


def build(dest: Path, src: Path | None = None) -> dict:
    """Write a fresh runtime into `dest`; returns a short manifest."""
    check_interpreter()
    src = Path(src or sys.base_prefix)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    base = dll_name()
    for name in (*TOP_FILES, f"{base}.dll"):
        shutil.copy2(src / name, dest / name)
    (dest / "DLLs").mkdir()
    for item in sorted((src / "DLLs").iterdir()):
        if item.is_file() and _dll_wanted(item.name):
            shutil.copy2(item, dest / "DLLs" / item.name)
    failed = []
    count = 0
    with tempfile.TemporaryDirectory() as tmp, \
            zipfile.ZipFile(dest / f"{base}.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        cfile = Path(tmp) / "m.pyc"
        for path, arcname in lib_members(src / "Lib"):
            try:  # unchecked-hash pyc: valid without its source, never rewritten at runtime
                py_compile.compile(str(path), cfile=str(cfile), dfile=arcname[:-1], doraise=True,
                                   invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
            except py_compile.PyCompileError:
                failed.append(arcname)
                continue
            zf.write(cfile, arcname)
            count += 1
    _copy_tcl(src / "tcl", dest / "tcl")
    (dest / f"{base}._pth").write_text(f"{base}.zip\nDLLs\n..\\lib\n", encoding="ascii")
    size = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    return {"python": sys.version.split()[0], "modules": count, "failed": failed, "bytes": size}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build") / "runtime"
    print(build(target))
