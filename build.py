#!/usr/bin/env python3
"""Build the Luma NVDA add-on package.

An NVDA add-on (.nvda-addon) is simply a ZIP archive with ``manifest.ini`` at
its root. This script collects the runtime files, excludes development-only
artifacts, and writes ``luma-<version>.nvda-addon`` to the repository root.

Usage:
    python build.py
"""
from __future__ import annotations

import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))

# Top-level files/dirs that make up the installable add-on. Anything not listed
# here (dev docs, .git, .claude, build.py, sounds.py, README.md, .gitignore …)
# is left out of the package.
INCLUDE_TOP_LEVEL = (
    "manifest.ini",
    "LICENSE",
    "globalPlugins",
    "locale",
    "doc",
    "assets",
)

# Directory names skipped anywhere in the tree.
SKIP_DIRS = {"__pycache__"}

# Path fragments (relative, POSIX-style) skipped anywhere in the tree.
# mupdf-devel holds C/C++ headers and a static lib — build-time only, never
# loaded at runtime.
SKIP_PATH_FRAGMENTS = ("globalPlugins/luma/lib/pymupdf/mupdf-devel/",)

# File suffixes skipped anywhere in the tree.
SKIP_SUFFIXES = (".pyc", ".pyo", ".po", ".pot", ".nvda-addon")


def read_version() -> str:
    version = "0.0.0"
    with open(os.path.join(ROOT, "manifest.ini"), encoding="utf-8") as fh:
        for line in fh:
            if line.strip().startswith("version"):
                version = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return version


def should_skip(rel_posix: str) -> bool:
    if any(frag in rel_posix for frag in SKIP_PATH_FRAGMENTS):
        return True
    if rel_posix.endswith(SKIP_SUFFIXES):
        return True
    return False


def collect_files() -> list[tuple[str, str]]:
    """Return (absolute_path, archive_name) pairs to add to the ZIP."""
    files: list[tuple[str, str]] = []
    for top in INCLUDE_TOP_LEVEL:
        abs_top = os.path.join(ROOT, top)
        if not os.path.exists(abs_top):
            continue
        if os.path.isfile(abs_top):
            files.append((abs_top, top))
            continue
        for dirpath, dirnames, filenames in os.walk(abs_top):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                abs_path = os.path.join(dirpath, name)
                rel = os.path.relpath(abs_path, ROOT).replace(os.sep, "/")
                if should_skip(rel):
                    continue
                files.append((abs_path, rel))
    return files


def main() -> int:
    version = read_version()
    out_name = f"luma-{version}.nvda-addon"
    out_path = os.path.join(ROOT, out_name)
    if os.path.exists(out_path):
        os.remove(out_path)

    files = collect_files()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for abs_path, arcname in files:
            zf.write(abs_path, arcname)

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"Built {out_name} ({len(files)} files, {size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
