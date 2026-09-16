# Windows Portable and MSI builds

The Portable build is `dist/PowerTerm-Portable.exe`. It runs without installation
and retains PyInstaller's single-file extraction at launch. "Portable" describes
the executable packaging; settings still use PowerTerm's normal per-user location.

The MSI is `dist/PowerTerm-VERSION-ARCH.msi`. It contains a PyInstaller directory
build, installs into Program Files, creates a Start menu shortcut, and registers
in Windows Installed Apps. Installation requires administrator approval. Running
the installed app needs neither Python nor WiX and avoids single-file extraction.

## Build prerequisites (Windows only)

Install Python 3.11+ with pip, and the .NET SDK/runtime required by WiX 4.
This script uses a pinned WiX tool and matching UI extension:

```powershell
dotnet tool install --global wix --version 4.0.6
wix extension add --global WixToolset.UI.wixext/4.0.6
wix --version
```

If you already have a different global WiX version, use a separate build account
or explicitly manage that tool installation. Ensure `wix` is on PATH. WiX 4 may
require the .NET 6 runtime alongside a newer SDK; follow the runtime instruction
printed by `wix --version` if it cannot start. No tool is installed globally by
PowerTerm's scripts. WiX setup: https://docs.firegiant.com/wix/using-wix/

After extracting the MSI build kit, double-click `BUILD-WINDOWS.bat`. The kit
selects the MSI target automatically. Python dependencies are installed in .venv.
Missing WiX or its extension is reported, rather than silently producing only a
portable build. From a full project checkout you can also run:

```bat
scripts\build-windows.bat windows msi --version 0.1.1
```

Build architecture follows the Windows Python executable (x86, x64, or ARM64).
Use the same architecture for successive releases unless you test a migration.

## Releases and configuration preservation

Use three numeric version fields: `major.minor.build`, up to `255.255.65535`.
Increment the version for each release, e.g. `0.1.0` then `0.1.1`. On Linux:

```bash
python3 scripts/build.py msi --version 0.1.1
```

Download the resulting Windows MSI build ZIP and build it on Windows. Users can
close PowerTerm and run the newer MSI to replace the older installation. The
installer uses a stable UpgradeCode and a new ProductCode per build. It removes
the older payload within the installation transaction, blocks downgrades, and
allows same-version rebuilds to replace an installation instead of creating a
second entry. Keep the UpgradeCode constant in `scripts/windows_msi.py`.

The installer never includes, writes, migrates or deletes `config.json` or
`hosts.json`. Application and organization names stay `PowerTerm`, so Qt keeps
the existing per-user configuration location. Installed and portable builds run
by the same user use the same configuration. Each Windows user retains their own
settings. Passwords remain in the operating-system credential store. Uninstall
also leaves these per-user settings intact. The installer does not search for or
remove portable EXEs that users copied elsewhere.

The original GPL licence is bundled unchanged and displayed in the installer.
Provide corresponding source with binary releases. The MSI is unsigned unless
you separately sign it; no signing certificate or publishing step is configured.

## Windows release validation

The Linux server can prepare source kits and check installer authoring, but cannot
validate Windows Installer execution. Before distributing an MSI:

1. Install version 0.1.0 on a Windows test machine and launch it from Start.
2. Save settings/host profiles and note the JSON path shown in Settings.
3. Close PowerTerm; copy the JSON somewhere safe and record its contents.
4. Run the 0.1.1 MSI. Confirm one Installed Apps entry, the new program files,
   unchanged JSON contents, and working settings, local terminal and SSH.
5. Confirm the older MSI is blocked, a same-version rebuild replaces cleanly,
   and uninstall removes program files/shortcut but preserves configuration.

Reference: https://docs.firegiant.com/wix/schema/wxs/majorupgrade/
