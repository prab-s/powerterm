# PowerTerm

**Remote (SSH) terminals, and SCP/SFTP file management in one desktop application.**

![PowerTerm running on Windows, with an SSH terminal, remote file browser, and system status indicators](PowerTerm.png)

[Getting started](#getting-started) · [User and build guide](USER_GUIDE.md) · [Windows installers](scripts/WINDOWS-MSI.md) · [Licence](LICENSE)

Work with local and remote shells, browse files beside your terminal, and keep hosts
and frequently used commands close at hand.

PowerTerm is a terminal and SSH client for Linux and Windows, written in Python/PySide6 with a lot of help from Codex.
It's kind of like MobaXterm, but cross platform, and FOSS.

---

## What you can do

- **Work locally or over SSH** with tabbed sessions and a side-by-side split view.
- **Manage files alongside your terminal** using a local file browser or remote
  SFTP, with uploads, downloads, and a built-in text editor.
- **Keep navigation in sync** by letting the terminal follow the file browser,
  or the file browser follow the terminal.
- **Reuse hosts and commands** with saved SSH profiles and grouped saved commands.
- **Search terminal output and adjust its appearance**, including fonts and cursor settings.
- **Inspect system information** through CPU, memory, disk, network, and USB controls.

Settings and host profiles are stored in a per-user JSON configuration file.
Remembered passwords use the operating-system credential store through `keyring`
where available; they are not stored in that JSON file.

## Getting started

**Linux and Windows binaries are available in the releases (RECOMMENDED)** 

---

Otherwise...
To run from source, clone or download this repository and open a terminal in its
folder. Use Python 3.11 or newer.

**Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

**Windows PowerShell**

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

Choose **Local Terminal** to open a local shell, or **Quick SSH** to connect to a
remote machine. Use **Saved Hosts** for connections you want to reuse.

See the [user guide](USER_GUIDE.md) for terminal controls, file transfers,
[keyboard shortcuts](USER_GUIDE.md#useful-shortcuts), and
[where to find your configuration](USER_GUIDE.md#finding-the-configuration-file).

## Build your own package

PowerTerm includes a numbered build menu and a workflow for checking dependencies,
running tests, reviewing a Git push, and choosing build targets.

| Platform | Packaging options |
| --- | --- |
| Windows | Portable `.exe` and MSI installer with upgrade support |
| Linux | Standalone executable, AppImage, Debian `.deb`, and experimental Flatpak |

Windows executables and installers are built on Windows. From Linux, the Windows
options create ZIP kits containing the source and launchers to build on your
Windows computer. Linux packages require Linux and the relevant packaging tools.

Start with the [build guide](USER_GUIDE.md#build-setup-and-selection), or follow
the [Windows Portable and MSI instructions](scripts/WINDOWS-MSI.md). The MSI
recipe is designed to preserve per-user configuration during upgrades; native
Windows installation and upgrade validation is still required before distribution.

## Development

The Linux repository is the authoritative development and build environment.
Application code lives in `main.py`, regression tests in `tests/`, and maintenance
and packaging scripts in `scripts/`.

For the guided workflow, run `python3 workflow.py` from the project root. Tests,
Git pushes, and builds can also be run independently. See the
[development workflow](USER_GUIDE.md#one-command-workflow) for details.

## Licence

PowerTerm is licensed under the **GNU General Public License, version 3**.
See [LICENSE](LICENSE) for the full terms.
