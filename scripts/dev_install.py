#!/usr/bin/env python3
"""Link the working tree into Krita for development (macOS).

    python3 scripts/dev_install.py            # symlink + enable the plugin
    python3 scripts/dev_install.py --uninstall

Symlinks src/ into ~/Library/Application Support/krita/{pykrita,actions} so
edits take effect on the next Krita restart, and sets
``[python] enable_samselect=true`` in kritarc (a backup is written first).
Quit Krita before running it: Krita rewrites kritarc when it exits.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
RESOURCES = Path.home() / "Library" / "Application Support" / "krita"
KRITARC = Path.home() / "Library" / "Preferences" / "kritarc"
LINKS = {
    RESOURCES / "pykrita" / "samselect": SRC / "samselect",
    RESOURCES / "pykrita" / "samselect.desktop": SRC / "samselect.desktop",
    RESOURCES / "actions" / "samselect.action": SRC / "samselect.action",
}


def krita_running() -> bool:
    return subprocess.run(["pgrep", "-x", "krita"], capture_output=True).returncode == 0


def set_enabled(enabled: bool) -> None:
    text = KRITARC.read_text() if KRITARC.exists() else ""
    backup = KRITARC.with_name(f"kritarc.samselect-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    if text:
        backup.write_text(text)
    line = f"enable_samselect={'true' if enabled else 'false'}"
    if re.search(r"^enable_samselect=.*$", text, flags=re.M):
        text = re.sub(r"^enable_samselect=.*$", line, text, flags=re.M)
    elif re.search(r"^\[python\]\s*$", text, flags=re.M):
        text = re.sub(r"^\[python\]\s*$", "[python]\n" + line, text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n\n[python]\n{line}\n"
    KRITARC.write_text(text)
    print(f"kritarc: {line}" + (f" (backup: {backup.name})" if backup.exists() else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--force", action="store_true", help="run even if Krita is running")
    args = parser.parse_args()
    if krita_running() and not args.force:
        sys.exit("Krita is running. Quit it first (it rewrites kritarc on exit), or pass --force.")

    for link, target in LINKS.items():
        if link.is_symlink() or link.exists():
            if link.is_symlink():
                link.unlink()
            elif link.is_dir():
                shutil.rmtree(link)
            else:
                link.unlink()
        if not args.uninstall:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
            print(f"linked {link} -> {target}")
    set_enabled(not args.uninstall)
    print("done; start Krita" if not args.uninstall else "uninstalled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
