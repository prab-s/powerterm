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
import zipfile
try:
    from scripts.console_ui import section, note, option, rule
    from scripts import windows_msi
except ModuleNotFoundError:
    from console_ui import section, note, option, rule
    import windows_msi

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP_ID = "io.github.prab_s.PowerTerm"
TARGETS = ("windows", "linux", "appimage", "flatpak", "deb", "msi")
MODULES = ("PyInstaller", "PySide6", "paramiko", "psutil", "pyte", "keyring")
DEFAULT_VERSION = "0.1.0"
PACKAGE_VERSION_RE = re.compile(r"[0-9][A-Za-z0-9.+~]*")


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
    if target in ("windows", "msi") and platform.system() != "Windows":
        return None  # Source kit only; the executable will be built on Windows.
    required_os = "Windows" if target in ("windows", "msi") else "Linux"
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
        modules = MODULES + (("winpty",) if target in ("windows", "msi") else ())
        missing = [name for name in modules if importlib.util.find_spec(name) is None]
        if missing:
            return "missing Python modules: " + ", ".join(missing) + "; install requirements-build.txt"
    return windows_msi.tool_problem() if target == "msi" else None


def target_label(target):
    return {
        "windows": "Windows Portable — single-file executable" if platform.system() == "Windows" else "Windows Portable — create a build ZIP for Windows",
        "msi": "Windows MSI — installer with upgrades" if platform.system() == "Windows" else "Windows MSI — create an installer build ZIP for Windows",
        "linux": "Linux — standalone executable",
        "appimage": "Linux — AppImage",
        "flatpak": "Linux — Flatpak (experimental)",
        "deb": "Linux — Debian/Ubuntu .deb package",
    }[target]


def parse_selection(value, available):
    tokens = value.lower().replace(",", " ").split()
    if not tokens or tokens in (["q"], ["0"], ["skip"]):
        return []
    if tokens in (["a"], ["all"]):
        selected = [target for target in TARGETS if available[target] is None]
        if not selected:
            raise ValueError("No targets are available in this environment.")
        return selected
    selected = []
    for token in tokens:
        target = TARGETS[int(token) - 1] if token.isdigit() and 1 <= int(token) <= len(TARGETS) else token
        if target not in TARGETS:
            raise ValueError(f"Unknown choice: {token}. Use numbers 1–{len(TARGETS)} or target names.")
        if available[target]:
            raise ValueError(f"Cannot select {target}: {available[target]}")
        if target not in selected:
            selected.append(target)
    return selected


def choose_targets(available):
    section("Build selection | What would you like to create?")
    for number, target in enumerate(TARGETS, 1):
        option(number, target_label(target), "UNAVAILABLE: " + available[target] if available[target] else "READY")
    rule()
    option("A", "All available choices")
    option("0", "Finish without building")
    note("Choose one or more numbers, separated by commas. Example: 1,2,5")
    print()
    while True:
        try:
            selected = parse_selection(input("  Your selection [0]: "), available)
        except ValueError as exc:
            print(exc)
            continue
        if not selected:
            return []
        section("Review build selection")
        for target in selected:
            note(f"- {target_label(target)}")
        print()
        if input("  Create these now? [y/N]: ").strip().lower() in ("y", "yes"):
            return selected
        print("Nothing started. Choose again, or enter 0 to finish.")


def validate_package_version(version, selected):
    if not PACKAGE_VERSION_RE.fullmatch(version):
        raise ValueError("version must start with a digit and contain only letters, digits, '.', '+', or '~'")
    if "msi" in selected:
        windows_msi.validate_version(version)
    return version


def choose_version(selected):
    section("Package version")
    note("This version is used in package filenames and installer metadata.")
    if "msi" in selected:
        note("MSI requires major.minor.build, for example 0.1.1.")
    else:
        note("Example: 0.1.1. Press Enter to use 0.1.0.")
    while True:
        version = input(f"  Release version [{DEFAULT_VERSION}]: ").strip() or DEFAULT_VERSION
        try:
            return validate_package_version(version, selected)
        except ValueError as exc:
            print(exc)


def windows_kit(work, version, target="windows"):
    folder = f"PowerTerm-{version}-{'windows' if target == 'windows' else 'windows-msi'}-build-kit"
    output = work / (folder + ".zip")
    # Explicit allowlist excludes Git history, credentials, caches and host binaries.
    files = ("main.py", "powerterm.svg", "LICENSE", "requirements.txt",
             "requirements-build.txt", "scripts/build.py", "scripts/windows_setup.py",
             "scripts/build-windows.bat", "scripts/console_ui.py", "scripts/windows_msi.py")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            archive.write(ROOT / name, f"{folder}/{name}")
        archive.write(ROOT / "scripts/windows-kit.bat", f"{folder}/BUILD-WINDOWS.bat")
        archive.writestr(f"{folder}/PACKAGE-VERSION.txt", version + "\n")
        archive.writestr(f"{folder}/PACKAGE-TARGET.txt", target + "\n")
        archive.write(ROOT / "scripts/WINDOWS-MSI.md", f"{folder}/WINDOWS-MSI.md")
        archive.writestr(f"{folder}/START-HERE.txt", """PowerTerm Windows build kit

1. Install Python 3.11 or newer for Windows, including pip and the Python launcher.
2. Extract the ENTIRE ZIP to a writable folder (do not run inside the ZIP).
3. Double-click BUILD-WINDOWS.bat.
4. Wait for dependency installation and the PyInstaller build to finish.
5. Output is in dist: PowerTerm-Portable.exe or PowerTerm-VERSION-ARCH.msi.

For the MSI kit, first follow WINDOWS-MSI.md to install WiX and its UI extension.
The MSI installs a directory build; the portable EXE remains a single-file build.

Internet access is needed for Python dependencies. Python's architecture determines
the executable's architecture. No Git installation or repository is needed.
The launcher never commits or pushes anything and pauses so errors remain visible.
To retry a failed build, run BUILD-WINDOWS.bat again.

This ZIP contains source and build scripts, not a prebuilt Windows executable.
The Windows executable must be built on Windows. The original GPL licence is
included in LICENSE and is copied beside the executable. Keep the corresponding
source available when distributing the executable.
""")
    return output


def freeze(work, onefile):
    name = "PowerTerm" if platform.system() == "Windows" else "powerterm"
    if platform.system() == "Windows" and onefile:
        name = "PowerTerm-Portable"
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
    if target in ("windows", "msi") and platform.system() != "Windows":
        return windows_kit(work, version, target)
    if target == "flatpak":
        return build_flatpak(work, version, branch)
    onefile = target in ("linux", "windows")
    if onefile not in frozen:
        frozen[onefile] = freeze(work, onefile)
    binary = frozen[onefile]
    if target == "msi":
        return windows_msi.build_msi(binary, work, version, ROOT / "LICENSE")
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
    parser.add_argument("targets", nargs="*", help="target names or numbers, separated by spaces or commas; all selects every available choice")
    parser.add_argument("--version", help="package version; prompts when omitted")
    parser.add_argument("--flatpak-branch", default="25.08")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.flatpak_branch):
        parser.error("invalid Flatpak branch")
    for name in ("main.py", "powerterm.svg", "LICENSE", "requirements.txt"):
        if not (ROOT / name).is_file():
            parser.error(f"missing required project file: {name}")
    available = {t: availability(t, args.flatpak_branch) for t in TARGETS}
    if args.targets:
        section("Build availability")
        for target, reason in available.items():
            option(target, target_label(target), "UNAVAILABLE: " + reason if reason else "READY")
        try:
            selected = parse_selection(" ".join(args.targets), available)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        selected = choose_targets(available)
    if not selected:
        print("Finished without building.")
        return 0
    if args.version is None:
        args.version = choose_version(selected)
    else:
        try:
            validate_package_version(args.version, selected)
        except ValueError as exc:
            parser.error(str(exc))
    if "flatpak" in selected:
        print("Flatpak is experimental: local shells/system information run inside its sandbox. Build downloads Python dependencies inside the SDK.")
    DIST.mkdir(exist_ok=True)
    build_root = ROOT / "build"
    build_root.mkdir(exist_ok=True)
    failed = False
    results = []
    with tempfile.TemporaryDirectory(prefix="packaging-", dir=build_root) as temp:
        work = Path(temp)
        frozen = {}
        for index, selected_target in enumerate(selected, 1):
            section(f"BUILD {index} OF {len(selected)} | {target_label(selected_target)}")
            try:
                output = package(selected_target, work, args.version, args.flatpak_branch, frozen)
                destination = DIST / output.name
                shutil.copy2(output, destination)
                shutil.copy2(ROOT / "LICENSE", DIST / "LICENSE")
                results.append((selected_target, destination, None))
                note(f"OK: {destination.name}")
            except (OSError, subprocess.CalledProcessError, ValueError) as exc:
                failed = True
                results.append((selected_target, None, str(exc)))
                print(f"FAILED {selected_target}: {exc}", file=sys.stderr)
    section("Build results")
    for target, destination, error in results:
        if error:
            option("FAILED", target_label(target), error)
        else:
            option("OK", target_label(target))
            print(f"  File: {destination}\n", flush=True)
            if destination.suffix == ".zip":
                note("Next: copy the ZIP to Windows, extract it, and double-click BUILD-WINDOWS.bat. See START-HERE.txt for instructions.")
                print()
    rule()
    return int(failed)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (EOFError, KeyboardInterrupt, OSError) as exc:
        section("Build stopped", stream=sys.stderr)
        print(f"Build stopped: {exc}", file=sys.stderr)
        sys.exit(1)
