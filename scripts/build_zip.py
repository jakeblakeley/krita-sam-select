#!/usr/bin/env python3
"""Package the plugin as a versioned zip for Krita's plugin importer.

    python3 scripts/build_zip.py            # -> dist/samselect-v<version>.zip
    python3 scripts/build_zip.py --copy-to ~/Desktop

Install the zip in Krita with Tools > Scripts > Import Python Plugin from
File…, then restart Krita. The zip layout is what the importer expects:
``samselect.desktop``, ``samselect.action`` and the ``samselect/`` package at
the top level.

Each version is written once: rebuilding an existing version fails unless you
pass --force, so dist/ keeps every released build. Bump
src/samselect/version.py for a new version.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DIST = ROOT / "dist"
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_NAMES = {".DS_Store"}


def version() -> str:
    text = (SRC / "samselect" / "version.py").read_text()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        sys.exit("could not read __version__ from src/samselect/version.py")
    return match.group(1)


def files() -> list[Path]:
    out = [SRC / "samselect.desktop", SRC / "samselect.action"]
    for path in sorted((SRC / "samselect").rglob("*")):
        rel = path.relative_to(SRC)
        if path.is_dir() or EXCLUDE_DIRS & set(rel.parts) or path.suffix in EXCLUDE_SUFFIXES or path.name in EXCLUDE_NAMES:
            continue
        out.append(path)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="overwrite an existing zip for this version")
    parser.add_argument("--copy-to", type=Path, help="also copy the zip into this folder (e.g. ~/Desktop)")
    args = parser.parse_args()

    ver = version()
    DIST.mkdir(exist_ok=True)
    target = DIST / f"samselect-v{ver}.zip"
    if target.exists() and not args.force:
        sys.exit(f"{target.relative_to(ROOT)} already exists; bump the version or pass --force")

    tmp = target.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files():
            info = zipfile.ZipInfo.from_file(path, path.relative_to(SRC).as_posix())
            info.date_time = (2026, 1, 1, 0, 0, 0)  # reproducible archives
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
    tmp.replace(target)

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"built {target.relative_to(ROOT)}  ({target.stat().st_size / 1024:.0f} KB, sha256 {digest[:16]}…)")
    if args.copy_to:
        dest = args.copy_to.expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, dest / target.name)
        print(f"copied to {dest / target.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
