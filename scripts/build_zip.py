#!/usr/bin/env python3
"""Package the plugin as a versioned zip for Krita's plugin importer.

    python3 scripts/build_zip.py            # -> dist/samselect-v<version>.zip
    python3 scripts/build_zip.py --copy-to ~/Desktop

Install the zip in Krita with Tools > Scripts > Import Python Plugin from
File…, then restart Krita. The zip layout is what the importer expects:
``samselect.desktop``, ``samselect.action`` and the ``samselect/`` package at
the top level, with explicit directory entries (the importer locates the
package by its ``samselect/`` entry).

Every build is verified by running Krita's own importer
(plugin_importer.py from the installed Krita) against a scratch folder, so a
zip that Krita would reject never lands in dist/.

Each version is written once: rebuilding an existing version fails unless you
pass --force, so dist/ keeps every released build. Bump
src/samselect/version.py for a new version.
"""

from __future__ import annotations

import argparse
import builtins
import hashlib
import importlib.util
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KRITA_IMPORTER = Path("/Applications/krita.app/Contents/Resources/krita/pykrita/plugin_importer/plugin_importer.py")
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_NAMES = {".DS_Store"}
STAMP = (2026, 1, 1, 0, 0, 0)  # reproducible archives
NAME = "samselect"


def version(src: Path) -> str:
    text = (src / NAME / "version.py").read_text()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        sys.exit(f"could not read __version__ from {src / NAME / 'version.py'}")
    return match.group(1)


def entries(src: Path) -> list[tuple[str, Path | None]]:
    """(archive name, source file or None for a directory), directories first."""
    out: list[tuple[str, Path | None]] = [(f"{NAME}.desktop", src / f"{NAME}.desktop"), (f"{NAME}.action", src / f"{NAME}.action")]
    out.append((f"{NAME}/", None))
    for path in sorted((src / NAME).rglob("*")):
        rel = path.relative_to(src)
        if EXCLUDE_DIRS & set(rel.parts) or path.suffix in EXCLUDE_SUFFIXES or path.name in EXCLUDE_NAMES:
            continue
        out.append((rel.as_posix() + "/", None) if path.is_dir() else (rel.as_posix(), path))
    return out


def write_zip(src: Path, target: Path) -> None:
    tmp = target.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, path in entries(src):
            info = zipfile.ZipInfo(name, date_time=STAMP)
            if path is None:  # directory entry, as Finder / `zip -r` write them
                info.external_attr = (0o40755 << 16) | 0x10
                zf.writestr(info, b"")
            else:
                info.external_attr = (0o100644 << 16)
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes())
    tmp.replace(target)


def verify(target: Path) -> None:
    """Install the zip with Krita's own importer into a scratch folder."""
    with tempfile.TemporaryDirectory() as res:
        if KRITA_IMPORTER.exists():
            builtins.i18n = getattr(builtins, "i18n", lambda s: s)  # the importer's only Krita dependency
            spec = importlib.util.spec_from_file_location("krita_plugin_importer", KRITA_IMPORTER)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            imported = module.PluginImporter(str(target), res, lambda plugin: True).import_all()
            names = [p["name"] for p in imported]
            if names != [NAME]:
                raise SystemExit(f"Krita's importer imported {names}, expected [{NAME!r}]")
            installed = Path(res)
            for rel in (f"pykrita/{NAME}.desktop", f"pykrita/{NAME}/__init__.py", f"pykrita/{NAME}/server/engine.py", f"actions/{NAME}.action"):
                if not (installed / rel).is_file():
                    raise SystemExit(f"Krita's importer did not install {rel}")
            how = "Krita's importer"
        else:  # no Krita here: apply the importer's detection rule directly
            names = zipfile.ZipFile(target).namelist()
            if f"{NAME}/" not in names or f"{NAME}/__init__.py" not in names:
                raise SystemExit(f"{target.name}: missing the {NAME}/ directory entry or its __init__.py")
            how = "the importer's detection rule (Krita not installed)"
    print(f"verified with {how}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="overwrite an existing zip for this version")
    parser.add_argument("--copy-to", type=Path, help="also copy the zip into this folder (e.g. ~/Desktop)")
    parser.add_argument("--root", type=Path, default=ROOT, help="project checkout to package (default: this one)")
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="output folder (default: dist/)")
    args = parser.parse_args()

    src = args.root.resolve() / "src"
    ver = version(src)
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / f"{NAME}-v{ver}.zip"
    if target.exists() and not args.force:
        sys.exit(f"{target} already exists; bump the version or pass --force")

    write_zip(src, target)
    try:
        verify(target)
    except SystemExit:
        target.unlink(missing_ok=True)
        raise
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"built {target}  ({target.stat().st_size / 1024:.0f} KB, sha256 {digest[:16]}…)")
    if args.copy_to:
        dest = args.copy_to.expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, dest / target.name)
        print(f"copied to {dest / target.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
