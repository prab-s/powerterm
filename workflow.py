#!/usr/bin/env python3
"""Install Python requirements, test, safely push, then choose a native build."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
from scripts.console_ui import section, note, option

ROOT = Path(__file__).resolve().parent


def run(*args):
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=ROOT, check=True,
                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def has_pip(python):
    return subprocess.run([str(python), "-m", "pip", "--version"], cwd=ROOT,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def prepare_environment():
    env_dir = ROOT / ".venv"
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        if env_dir.exists():
            raise RuntimeError(".venv exists but has no usable native Python. Recreate it on this OS first.")
        # Bootstrap separately so systems without ensurepip can use external pip.
        run(sys.executable, "-m", "venv", "--without-pip", env_dir)
    config = env_dir / "pyvenv.cfg"
    if not config.is_file():
        raise RuntimeError(".venv is not a virtual environment; refusing to install into it.")
    if not has_pip(python):
        bootstrap = subprocess.run([str(python), "-m", "ensurepip", "--upgrade"],
                                   cwd=ROOT, capture_output=True, text=True)
        if bootstrap.returncode:
            if has_pip(sys.executable):
                installer = [sys.executable, "-m", "pip"]
            else:
                external = shutil.which("pip3") or shutil.which("pip")
                if not external:
                    raise RuntimeError("Python's pip bootstrap is unavailable. Install python3-venv and python3-pip on Debian/Ubuntu (or pip for your Python), then rerun.")
                installer = [external]
            run(*installer, "--python", python, "install", "pip")
    run(python, "-m", "pip", "install", "-r", ROOT / "requirements-build.txt")
    run(python, "-m", "pip", "check")
    # Also catch missing shared libraries before attempting the test suite.
    run(python, "-c", "from PySide6.QtWidgets import QApplication; import paramiko, psutil, pyte, keyring, PyInstaller")
    return python


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", nargs="?", help="optional Git commit message")
    args = parser.parse_args(argv)
    if not shutil.which("git"):
        raise RuntimeError("Git is required; install it before running this workflow.")
    section("POWERTERM | Project workflow")
    note("Dependencies  >  Tests  >  GitHub  >  Builds")
    section("STEP 1 OF 4 | Python requirements")
    note("Installing/checking dependencies in the project's .venv.")
    python = prepare_environment()
    section("STEP 2 OF 4 | Tests")
    note("All tests must pass before continuing.")
    run(python, ROOT / "scripts/test.py")
    section("STEP 3 OF 4 | GitHub")
    option("1", "Review changes and push", "You will review the commit and confirm before pushing.")
    option("2", "Skip GitHub and choose builds", "Build from the files currently in this folder.")
    option("0", "Finish now")
    while True:
        choice = input("Choose [1]: ").strip() or "1"
        if choice in ("0", "1", "2"):
            break
        print("Enter 1, 2, or 0.")
    if choice == "0":
        print("Finished after tests. Nothing pushed or built.")
        return
    if choice == "1":
        push_args = [python, ROOT / "scripts/push.py"]
        if args.message is not None:
            push_args.append(args.message)
        run(*push_args)
    else:
        print("GitHub push skipped. Builds will use the files currently in this folder.")
    section("STEP 4 OF 4 | Builds and Windows transfer ZIP")
    run(python, ROOT / "scripts/build.py")
    section("Workflow complete")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        section("Workflow stopped", stream=sys.stderr)
        print(f"Workflow stopped: command exited with status {exc.returncode}. Later steps were not run.", file=sys.stderr)
        sys.exit(exc.returncode if exc.returncode > 0 else 1)
    except (RuntimeError, OSError, EOFError, KeyboardInterrupt) as exc:
        section("Workflow stopped", stream=sys.stderr)
        print(f"Workflow stopped: {exc}", file=sys.stderr)
        sys.exit(1)
