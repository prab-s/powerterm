#!/usr/bin/env python3
"""Prepare a Windows venv and build locally, without Git or the push workflow."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    if os.name != "nt":
        raise RuntimeError("Run this launcher on Windows. On Linux, select Windows in the build menu to create a transfer ZIP.")
    python = ROOT / ".venv/Scripts/python.exe"
    if not python.exists():
        if (ROOT / ".venv").exists():
            raise RuntimeError("An incompatible .venv exists. Remove that environment and retry on Windows.")
        subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    if not (ROOT / ".venv/pyvenv.cfg").is_file():
        raise RuntimeError(".venv is not a virtual environment.")
    subprocess.run([str(python), "-m", "ensurepip", "--upgrade"], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements-build.txt")], check=True)
    subprocess.run([str(python), "-m", "pip", "check"], check=True)
    args = sys.argv[1:]
    if not args:
        version_file = ROOT / "PACKAGE-VERSION.txt"
        version = version_file.read_text().strip() if version_file.exists() else "0.1.0"
        args = ["windows", "--version", version]
    subprocess.run([str(python), str(ROOT / "scripts/build.py"), *args], cwd=ROOT, check=True)
    print(f"Build finished. Output folder: {ROOT / 'dist'}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        print(f"Windows build stopped: {exc}", file=sys.stderr)
        sys.exit(1)
