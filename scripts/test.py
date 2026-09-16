#!/usr/bin/env python3
"""Run PowerTerm's existing regression suite independently of builds and pushes."""
import os
from pathlib import Path
import subprocess
import sys

if __name__ == "__main__":
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    sys.exit(subprocess.call(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v", *sys.argv[1:]],
        cwd=Path(__file__).resolve().parent.parent, env=env,
    ))
