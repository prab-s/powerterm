#!/usr/bin/env python3
"""Explicit, interactive commit and push for the authoritative PowerTerm checkout."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = {
    f"{prefix}prab-s/powerterm{suffix}"
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/")
    for suffix in ("", ".git")
}


def git(*args, capture=False):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def validate():
    actual = Path(git("rev-parse", "--show-toplevel", capture=True).strip()).resolve()
    current = Path(subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True, stderr=subprocess.PIPE).strip()).resolve()
    if actual != ROOT or current != ROOT or not (ROOT / "main.py").is_file():
        raise RuntimeError("Run this script from inside the PowerTerm repository containing it.")
    for args in (("remote", "get-url", "--all", "origin"),
                 ("remote", "get-url", "--push", "--all", "origin")):
        urls = git(*args, capture=True).splitlines()
        if len(urls) != 1 or urls[0] not in ALLOWED:
            raise RuntimeError("origin must have exactly one fetch/push URL for github.com/prab-s/powerterm.")
    if git("symbolic-ref", "--quiet", "--short", "HEAD", capture=True).strip() != "main":
        raise RuntimeError("Switch to main yourself before pushing; no branches are switched automatically.")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        path = Path(git("rev-parse", "--git-path", marker, capture=True).strip())
        if (ROOT / path).exists():
            raise RuntimeError("Finish or abort the current Git operation first.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", nargs="?", help="optional commit message")
    args = parser.parse_args()
    validate()
    print("\nGitHub destination: prab-s/powerterm — branch main", flush=True)
    print("Files to review (?? = new, M = modified, D = deleted):", flush=True)
    git("status", "--short", "--branch", "--untracked-files=all")
    # Refresh only the expected branch; reject divergence before changing the index.
    git("fetch", "--no-tags", "origin", "refs/heads/main")
    if subprocess.run(["git", "merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"], cwd=ROOT).returncode:
        raise RuntimeError("Local main is behind or diverged from origin/main. Reconcile it manually first.")
    dirty = bool(git("status", "--porcelain", "--untracked-files=all", capture=True))
    print("\nExisting commits waiting to be pushed:", flush=True)
    git("log", "--oneline", "FETCH_HEAD..HEAD")
    message = args.message
    if dirty and not message:
        message = input("Commit message [Update PowerTerm]: ").strip() or "Update PowerTerm"
    if dirty and not message.strip():
        raise RuntimeError("Commit message must not be blank.")
    if dirty:
        print(f"\nCommit message: {message}")
        print("Continuing will stage ALL non-ignored changes shown above, commit, and push to origin main.")
    else:
        print("\nNo file changes; no new commit will be created.")
    while True:
        print("\n  1. Confirm and push to GitHub\n  2. Review tracked-file diff\n  0. Cancel")
        choice = input("Choose [0]: ").strip().lower()
        if choice in ("1", "y", "yes"):
            break
        if choice == "2":
            git("--no-pager", "diff", "HEAD", "--", ".")
            print("New untracked files are listed in status above; review their contents before pushing.")
            continue
        if choice in ("", "0", "n", "no", "q"):
            print("Cancelled; nothing staged, committed, or pushed.")
            return 2
        print("Choose 1 to push, 2 to review, or 0 to cancel.")
    validate()
    if dirty:
        git("add", "--all", "--", ".")
        diff = subprocess.run(["git", "diff", "--cached", "--quiet", "--exit-code"], cwd=ROOT).returncode
        if diff == 1:
            git("commit", "-m", message)
        elif diff == 0:
            print("No staged changes; skipping empty commit.")
        else:
            raise RuntimeError("Could not inspect staged changes.")
    else:
        print("No changes; skipping empty commit.")
    validate()
    git("-c", "remote.origin.mirror=false", "push", "--no-force", "--no-follow-tags",
        "--recurse-submodules=no", "origin", "refs/heads/main:refs/heads/main")
    print("GitHub push completed: origin main.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError, EOFError, KeyboardInterrupt) as exc:
        print(f"Push stopped: {exc}", file=sys.stderr)
        sys.exit(1)
