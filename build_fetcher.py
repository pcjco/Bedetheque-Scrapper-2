"""
Builds BedethequeFetcher(.exe) with PyInstaller.

Usage (run from the repo root):
    python build_fetcher.py

Layout assumptions:
    ./build_fetcher.py            <- this script
    ./tools/BedethequeFetcher.py  <- entry point
    ./src/                        <- output: BedethequeFetcher.exe is dropped here,
                                    next to BedethequeScraper2.py, ready to be
                                    zipped up by the packaging workflow.

    pyinstaller your_app.py
"""

import os
import sys

from PyInstaller.__main__ import run as pyinstaller_run

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
ENTRY_POINT = os.path.join(ROOT_DIR, "tools", "BedethequeFetcher.py")
DIST_DIR = os.path.join(ROOT_DIR, "src")
BUILD_DIR = os.path.join(ROOT_DIR, "build")
SPEC_DIR = ROOT_DIR

# PyInstaller's --add-data separator between "source" and "dest" is ';' on
# Windows and ':' on macOS/Linux.


def main():
    os.chdir(ROOT_DIR)

    if not os.path.isfile(ENTRY_POINT):
        raise FileNotFoundError("Entry point not found: " + ENTRY_POINT)

    pyinstaller_run([
        ENTRY_POINT,
        "--name=BedethequeFetcher",
        "--onefile",
        "--console",
        "--distpath=" + DIST_DIR,
        "--workpath=" + BUILD_DIR,
        "--specpath=" + SPEC_DIR,
        "--clean",
        "--noconfirm",
    ])


if __name__ == "__main__":
    sys.exit(main())
