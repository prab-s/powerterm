#!/usr/bin/env python3
"""Prepare a Windows venv and build locally, without Git or the push workflow."""
import os
from pathlib import Path
import subprocess
import sys
try:
    from scripts.console_ui import section, note
except ModuleNotFoundError:
    from console_ui import section, note

ROOT = Path(__file__).resolve().parent.parent


def main():
    if os.name != "nt":
        raise RuntimeError("Run this launcher on Windows. On Linux, select Windows in the build menu to create a transfer ZIP.")
    section("POWERTERM | Windows build")
    note("Prepare environment  >  Install requirements  >  Build executable")
    section("STEP 1 OF 3 | Python environment")
    python = ROOT / ".venv/Scripts/python.exe"
    if not python.exists():
        if (ROOT / ".venv").exists():
            raise RuntimeError("An incompatible .venv exists. Remove that environment and retry on Windows.")
        subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    if not (ROOT / ".venv/pyvenv.cfg").is_file():
        raise RuntimeError(".venv is not a virtual environment.")
    subprocess.run([str(python), "-m", "ensurepip", "--upgrade"], check=True)
    section("STEP 2 OF 3 | Python requirements")
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements-build.txt")], check=True)
    subprocess.run([str(python), "-m", "pip", "check"], check=True)
    args = sys.argv[1:]
    if not args:
        version_file = ROOT / "PACKAGE-VERSION.txt"
        version = version_file.read_text().strip() if version_file.exists() else "0.1.0"
        target_file = ROOT / "PACKAGE-TARGET.txt"
        target = target_file.read_text().strip() if target_file.exists() else "windows"
        args = [target, "--version", version]
    section("STEP 3 OF 3 | Build executable")
    subprocess.run([str(python), str(ROOT / "scripts/build.py"), *args], cwd=ROOT, check=True)
    section("Windows build complete")
    print(f"Build finished. Output folder: {ROOT / 'dist'}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        section("Windows build stopped", stream=sys.stderr)
        print(f"Windows build stopped: {exc}", file=sys.stderr)
        sys.exit(1)
