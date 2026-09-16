#!/usr/bin/env python3
"""Interactive native packaging. Never installs host tools, runs tests, or pushes Git."""
import argparse
import importlib.util
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP_ID = "io.github.prab_s.PowerTerm"
TARGETS = ("windows", "linux", "appimage", "flatpak", "deb")
MODULES = ("PyInstaller", "PySide6", "paramiko", "psutil", "pyte", "keyring")


def run(*args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def write(path, content, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def desktop(icon="powerterm"):
    return ("[Desktop Entry]\nType=Application\nName=PowerTerm\n"
            "Comment=Terminal and SSH client\nExec=powerterm\n"
            f"Icon={icon}\nTerminal=false\nCategories=System;TerminalEmulator;\n")


def availability(target, branch):
    required_os = "Windows" if target == "windows" else "Linux"
    if platform.system() != required_os:
        return f"requires a native {required_os} environment"
    needed = {"appimage": ("appimagetool",), "deb": ("dpkg-deb", "dpkg"),
              "flatpak": ("flatpak",)}.get(target, ())
    missing = [name for name in needed if not shutil.which(name)]
    if missing:
        return "missing tool(s): " + ", ".join(missing)
    if target == "flatpak":
        for runtime in ("org.freedesktop.Sdk", "org.freedesktop.Platform"):
            result = subprocess.run(["flatpak", "info", f"{runtime}//{branch}"],
                                    capture_output=True, text=True)
            if result.returncode:
                return f"missing {runtime}//{branch}; install the SDK and Platform first"
    else:
        modules = MODULES + (("winpty",) if target == "windows" else ())
        missing = [name for name in modules if importlib.util.find_spec(name) is None]
        if missing:
            return "missing Python modules: " + ", ".join(missing) + "; install requirements-build.txt"
    return None


def freeze(work, onefile):
    name = "PowerTerm" if platform.system() == "Windows" else "powerterm"
    mode = "onefile" if onefile else "onedir"
    out = work / mode
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            f"--{mode}", "--name", name, "--distpath", out,
            "--workpath", work / f"{mode}-work", "--specpath", work,
            "--add-data", f"{ROOT / 'powerterm.svg'}{os.pathsep}.",
            "--add-data", f"{ROOT / 'LICENSE'}{os.pathsep}.",
            "--collect-all", "keyring"]
    if platform.system() == "Windows":
        args += ["--windowed", "--collect-all", "winpty"]
    run(*args, ROOT / "main.py", cwd=ROOT)
    return out / (name + (".exe" if onefile and platform.system() == "Windows" else ""))


def build_flatpak(work, version, branch):
    # Freeze inside the SDK so the binary uses the runtime's libc, not the host's.
    stage = work / "flatpak"
    run("flatpak", "build-init", stage, APP_ID, "org.freedesktop.Sdk",
        "org.freedesktop.Platform", branch)
    files = stage / "files"
    source = files / "src"
    source.mkdir(parents=True)
    for name in ("main.py", "powerterm.svg", "LICENSE", "requirements.txt"):
        shutil.copy2(ROOT / name, source / name)
    write(source / "build.sh", """#!/bin/sh
set -eu
cd /app/src
python3 -m pip install --target=/app/build-deps -r requirements.txt 'PyInstaller>=6.16'
export PYTHONPATH=/app/build-deps
python3 -m PyInstaller --noconfirm --clean --onedir --name powerterm \\
    --distpath /app/lib --workpath /app/work --specpath /app/work \\
    --add-data powerterm.svg:. --add-data LICENSE:. --collect-all keyring main.py
""")
    run("flatpak", "build", "--share=network", stage, "sh", "/app/src/build.sh")
    for name in ("build-deps", "src", "work"):
        shutil.rmtree(files / name)
    write(files / "bin/powerterm", '#!/bin/sh\nexport SHELL=/bin/sh\nexec /app/lib/powerterm/powerterm "$@"\n', True)
    write(files / f"share/applications/{APP_ID}.desktop", desktop(APP_ID))
    icon = files / f"share/icons/hicolor/scalable/apps/{APP_ID}.svg"
    icon.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "powerterm.svg", icon)
    license_dir = files / "share/licenses/powerterm"
    license_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "LICENSE", license_dir / "LICENSE")
    run("flatpak", "build-finish", "--command=powerterm", "--share=network", "--share=ipc",
        "--socket=x11", "--socket=wayland", "--device=dri", "--filesystem=home",
        "--talk-name=org.freedesktop.secrets", stage)
    repo = work / "flatpak-repo"
    run("flatpak", "build-export", repo, stage)
    output = work / f"PowerTerm-{version}-{platform.machine()}.flatpak"
    run("flatpak", "build-bundle", repo, output, APP_ID,
        "--runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo")
    return output


def package(target, work, version, branch, frozen):
    if target == "flatpak":
        return build_flatpak(work, version, branch)
    onefile = target in ("linux", "windows")
    if onefile not in frozen:
        frozen[onefile] = freeze(work, onefile)
    binary = frozen[onefile]
    if onefile:
        return binary
    if target == "appimage":
        appdir = work / "PowerTerm.AppDir"
        shutil.copytree(binary, appdir / "usr/lib/powerterm")
        write(appdir / "AppRun", '#!/bin/sh\nappdir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nexec "$appdir/usr/lib/powerterm/powerterm" "$@"\n', True)
        write(appdir / "powerterm.desktop", desktop())
        shutil.copy2(ROOT / "powerterm.svg", appdir / "powerterm.svg")
        (appdir / ".DirIcon").symlink_to("powerterm.svg")
        output = work / f"PowerTerm-{version}-{platform.machine()}.AppImage"
        run("appimagetool", appdir, output, env={**os.environ, "ARCH": platform.machine()})
        return output
    arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    stage = work / "deb"
    shutil.copytree(binary, stage / "opt/powerterm")
    write(stage / "usr/bin/powerterm", '#!/bin/sh\nexec /opt/powerterm/powerterm "$@"\n', True)
    write(stage / "usr/share/applications/powerterm.desktop", desktop())
    icon = stage / "usr/share/icons/hicolor/scalable/apps/powerterm.svg"
    icon.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "powerterm.svg", icon)
    license_dir = stage / "usr/share/doc/powerterm"
    license_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "LICENSE", license_dir / "copyright")
    write(stage / "DEBIAN/control", f"""Package: powerterm
Version: {version}
Section: utils
Priority: optional
Architecture: {arch}
Maintainer: PowerTerm maintainers <prab-s@users.noreply.github.com>
Depends: libc6, libgl1, libegl1, libxkbcommon0, libxkbcommon-x11-0, libdbus-1-3, libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-render-util0, libxcb-xinerama0
Description: PowerTerm terminal and SSH client
 Python and PySide6 desktop terminal with local and SSH sessions.
""")
    output = work / f"powerterm_{version}_{arch}.deb"
    run("dpkg-deb", "--build", "--root-owner-group", stage, output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", choices=(*TARGETS, "all"))
    parser.add_argument("--version", default="0.1.0", help="package version (default: 0.1.0)")
    parser.add_argument("--flatpak-branch", default="25.08")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+~]*", args.version):
        parser.error("version must start with a digit and contain only letters, digits, '.', '+', or '~'")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.flatpak_branch):
        parser.error("invalid Flatpak branch")
    for name in ("main.py", "powerterm.svg", "LICENSE", "requirements.txt"):
        if not (ROOT / name).is_file():
            parser.error(f"missing required project file: {name}")
    available = {t: availability(t, args.flatpak_branch) for t in TARGETS}
    for target, reason in available.items():
        print(f"{target}: {reason or 'available'}")
    target = args.target
    if target is None:
        target = input("Build which target? windows/linux/appimage/flatpak/deb/all [all]: ").strip().lower() or "all"
        if target not in (*TARGETS, "all"):
            parser.error("unknown target")
    selected = [t for t, reason in available.items() if reason is None] if target == "all" else [target]
    if not selected:
        print("No native targets available.", file=sys.stderr)
        return 1
    if target != "all" and available[target]:
        print(f"Cannot build {target}: {available[target]}", file=sys.stderr)
        return 1
    if "flatpak" in selected:
        print("Flatpak is experimental: local shells/system information run inside its sandbox. Build downloads Python dependencies inside the SDK.")
    DIST.mkdir(exist_ok=True)
    build_root = ROOT / "build"
    build_root.mkdir(exist_ok=True)
    failed = False
    with tempfile.TemporaryDirectory(prefix="packaging-", dir=build_root) as temp:
        work = Path(temp)
        frozen = {}
        for selected_target in selected:
            try:
                output = package(selected_target, work, args.version, args.flatpak_branch, frozen)
                destination = DIST / output.name
                shutil.copy2(output, destination)
                shutil.copy2(ROOT / "LICENSE", DIST / "LICENSE")
                print(f"BUILT {selected_target}: {destination}", flush=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                failed = True
                print(f"FAILED {selected_target}: {exc}", file=sys.stderr)
    return int(failed)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (EOFError, KeyboardInterrupt, OSError) as exc:
        print(f"Build stopped: {exc}", file=sys.stderr)
        sys.exit(1)
