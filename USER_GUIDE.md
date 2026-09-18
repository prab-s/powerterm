# PowerTerm User and Build Guide

Detailed usage notes, development history, and instructions for testing,
pushing changes, and packaging PowerTerm.

[Back to the project overview](README.md)

## Find your way around

- [Run from source](#install)
- [Terminal and file-browser usage](#layout)
- [Keyboard shortcuts](#useful-shortcuts)
- [Find your configuration file](#finding-the-configuration-file)
- [One-command development workflow](#one-command-workflow)
- [Run tests](#tests-separate-from-pushing-and-building)
- [Safe Git push](#explicit-git-push)
- [Build menu and packaging targets](#build-setup-and-selection)
- [Windows MSI installation and upgrades](#windows-msi-installation-and-upgrades)
- [Experimental Flatpak packaging](#experimental-flatpak)

The usage and change notes below were written as the application evolved. Earlier
layout descriptions may refer to older versions; later entries document changes.
For a current overview, see the [README](README.md).

---

A deliberately compact cross-platform terminal/SSH client built with Python and PySide6.

## Layout

- **Menus:** Connection, Terminal, Files and View. Actions live in the menu that matches their purpose.
- **Top toolbars:** three separate, labelled toolbars for Connection, Terminal and Files. They use platform icons, subtle dark gradients and visible button borders.
- **Left / Files:** one context-sensitive file browser. Local tab = local filesystem; SSH tab = that session's SFTP filesystem.
- **Left / Hosts:** saved SSH profiles plus a permanent **Local** entry.
- **Centre:** movable, closeable terminal session tabs.
- **Bottom:** clickable, icon-labelled CPU, RAM, Disk, Network and USB controls for the active session.

The app starts with no terminal session. There is no redundant Start toolbar action; the start page is only the empty-session launcher.

## Terminal Find

`Ctrl+F` opens a persistent, modeless Find panel.

- **Find All** lists every current match.
- **Next** and **Previous** wrap through the matches.
- If you change the search text or Match Case and immediately press Next/Previous, the result set is automatically refreshed first.
- Every result includes its terminal line number and the full line.
- If the same substring occurs several times on one line, every occurrence is a separate result and **that specific occurrence is highlighted inside the displayed line**.
- Double-click a result to jump to it.
- `F3` = next; `Shift+F3` = previous.

The current implementation searches the rendered terminal screen. A persistent scrollback/search buffer remains a worthwhile future terminal-engine feature.

## Appearance settings

**Terminal → Appearance & Terminal Settings…** stores these values in `config.json`:

- cursor style: I-beam / block / underline;
- blinking cursor on/off;
- terminal font family and size;
- system-information font family and size;
- file-browser follows terminal directory on/off.

The default cursor remains a blinking I-beam.

## System-information windows

The CPU, RAM, Disk, Network and USB controls are real bordered/gradient buttons rather than label-like text. Each has an icon and opens details for the active local or SSH session.

The detail windows use the configured system-information font and lightweight automatic highlighting for common sysadmin output, including:

- section headings;
- device names such as `/dev/sda`, `nvme0n1`, etc.;
- IP addresses;
- sizes, percentages and numeric values;
- key/value labels;
- warning/error/down/unavailable text.

Linux/local and Linux/SSH information uses tools such as `lscpu`, `free`, `df -hT`, `lsblk`, `ip` and `lsusb`. Windows local mode uses PowerShell equivalents.

## File browser

The path bar is editable: type/paste a path and press Enter or **Go**.

Visual distinctions:

- folders use file-manager folder icons and **bold names**;
- files retain file icons;
- hidden dotfiles/dotfolders are *italic* and muted grey;
- hidden local entries are enabled in the model so they can actually be seen and managed.

The context menu supports Terminal Here, Copy Path, rename, create, delete, refresh and permissions. Remote operations use SFTP.

## Transfers

The Files toolbar has one **Transfer** split/drop-down control rather than separate permanent Upload/Upload Folder/Download buttons.

For SSH sessions it offers:

- Upload Files…
- Upload Folder…
- Download Selected…

The main Transfer click defaults to Upload Files. Transfers are disabled for a local-only session.

## Saved hosts and passwords

Normal app settings and host profiles live in Qt's per-user application configuration directory as `config.json`.

Passwords are not written into JSON. If you choose to remember one, PowerTerm uses Python `keyring` and the operating-system credential store where available.

## Install

Python 3.11+ is recommended.

```bash
python -m venv .venv
```

Linux/macOS:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Then:

```bash
python -m pip install -r requirements.txt
python main.py
```

## Useful shortcuts

- `Ctrl+Shift+L` — new Local terminal tab
- `Ctrl+Shift+S` — Quick SSH
- `Ctrl+Shift+V` — paste into active terminal
- `Ctrl+F` — Find
- `F3` — next Find result
- `Shift+F3` — previous Find result
- `F5` — refresh current file browser

## Performance

PTY/SSH chunks are coalesced into GUI-frame-sized batches, and adjacent terminal cells with identical formatting are inserted into Qt as text runs rather than one character at a time. The next meaningful rendering optimisation, if needed, is dirty-row rendering plus a proper scrollback buffer.

## 2026-09 UI and Windows sysinfo fixes

- Fixed the font chooser crash by using an instance `QFontDialog` and `selectedFont()` rather than relying on the static `getFont()` tuple order.
- Replaced the three stacked toolbars plus visible menu bar with one compact ribbon made of Connection, Terminal, and Files groups. Each group has icon/text buttons and a group label along the bottom. Keyboard shortcuts remain active.
- Improved local Windows system-information dialogs. PowerShell output now uses explicit sections and wide output for physical disks, partitions, volumes, network adapters, IP configuration, routes, USB devices, processor details, and memory modules.
- Windows PowerShell subprocess output is forced to UTF-8 and captured with a wider formatting width to reduce wrapping/truncation.

## UI fixes in this build

- Fixed Windows sysadmin detail windows failing with `strip_ansi is not defined`.
- The top ribbon now contains only Connection and Terminal groups.
- Home, Refresh, Terminal Here and Transfer now live in a compact toolbar inside the Files pane.
- Added a custom PowerTerm application icon (`powerterm.svg`) and Windows AppUserModelID so the running script gets a recognisable taskbar icon.


## Latest additions

- Ribbon Appearance button now shows `Appearance` over `Terminal Settings`.
- Third left-pane tab: **Saved Commands**.
  - Create named groups and named commands.
  - Reorder groups and commands with Up/Down controls.
  - Sort groups or a group’s commands A-Z from the context menu.
  - Double-click or press Run to send a command to the active terminal session.
  - Duplicate commands.
  - Optional confirmation before running a command.
  - Saved in the existing `config.json`; passwords remain in the OS credential store.
- Terminal line-number gutter added so Find result line numbers correspond to visible terminal rows.

Saved command example in `config.json`:

```json
{
  "command_groups": [
    {
      "id": "...",
      "name": "Docker",
      "commands": [
        {
          "id": "...",
          "name": "List containers",
          "command": "docker ps -a",
          "confirm": false
        }
      ]
    }
  ]
}
```


## Scrollback

Appearance and Terminal Settings includes a configurable per-session scrollback limit (default 10,000 lines). The Follow terminal directory option is kept only in the Files pane.

## Terminal stability and saved-command nesting

- Terminal line numbers are shown only while Find is open.
- Mouse-wheel history browsing now keeps the viewed history stable; new PTY output is queued while browsing and resumes when returning to the live terminal or typing.
- Saved Command groups can contain subgroups to arbitrary depth.
- Commands that use sudo/su or otherwise request elevation prompt through the active terminal. PowerTerm deliberately does not store or auto-inject sudo/elevation passwords; only SSH login credentials may use the OS credential store.

## Terminal resize stability fix

This build fixes a PTY resize feedback loop that could make typed characters appear on separate lines and could make SSH shells repeatedly redraw as though Enter were being pressed. The QTextEdit scrollbars are disabled, terminal geometry changes are debounced, and the visual terminal cursor no longer moves Qt's internal text cursor during every repaint.

## Terminal presentation and resize behaviour

- Appearance and Terminal Settings now includes optional semantic terminal highlighting. It colours only otherwise uncoloured text, so application-provided ANSI colours still take priority.
- Terminal rows/columns are recalculated and immediately redrawn after window and font changes.
- Windows PowerShell predictive suggestions can be enabled/disabled separately. When enabled, PowerTerm asks PSReadLine to render inline predictions in subdued grey.
- One-line ribbon actions use a shorter button height; the two-line Appearance and Terminal Settings action keeps the taller format.


## Minor UI polish

- The status-bar label now uses `Disc` spelling.
- Hosts and Saved Commands action buttons now include compact icons to the left of their text labels.

## UI and PowerShell prediction fixes

- `Disc` spelling is now used by both the initial status button and every periodic local/remote status update.
- Disabling Windows PowerShell predictive suggestions now reasserts `PredictionSource None` at each prompt, preventing profiles/modules from re-enabling PSReadLine prediction.
- Ribbon groups fill the available ribbon height again, while action buttons are top-aligned within each group.

## Tab completion and left-pane polish

- Tab/Shift+Tab stay inside the active terminal and are forwarded to the PTY instead of moving Qt focus.
- PowerShell prediction setup explicitly loads PSReadLine before enabling History/InlineView predictions.
- File-pane toolbar buttons are slightly shorter; Hosts and Saved Commands use the same compact gradient styling with icons beside their labels.

## Remote navigation fix

- Remote SFTP browsing can now move above the login user's home directory all the way to `/`, subject to the SSH account's permissions.
- A `..` parent entry and an Up button were added to the file browser.
- CWD-follow hook installation now clears a partial input line first, preventing the hidden setup command from being concatenated with user typing.

## File-browser and editor additions

- The Files pane now has a persistent `Show hidden files` toggle alongside `Follow terminal directory`; it affects both local and SFTP browsing.
- Files/Hosts/Saved Commands controls use consistent vertical margins, spacing and compact button heights.
- Right-clicking a text file exposes `Open / Edit…`. The built-in editor supports local and SFTP files, Save, line numbers, Find/Previous/Next, line wrapping, and extension-aware syntax/context highlighting.
- The built-in editor intentionally rejects binary files and text files larger than 5 MiB to keep the UI responsive.

## Editor status, tabs, hidden items and bell

- The built-in editor status row now shows cursor line/column, line count, character count, current UTF-8 byte size, encoding, line-ending style, Unix-style permission text/octal mode, and Saved/Modified state.
- The Files option is explicitly named `Show hidden files and folders` and applies to both local and SFTP trees.
- Files toolbar spacing includes extra bottom breathing room.
- Left-pane and terminal-session tabs use custom trapezoidal tab painting.
- Terminal BEL (`0x07`) now triggers the operating system/application beep, while OSC terminator BELs are ignored.

## Critical terminal resize/scrollback fix

This build removes direct use of `pyte.HistoryScreen.prev_page()` / `next_page()` for mouse scrolling. Scrollback is rendered from immutable copies of pyte's history instead, avoiding `RuntimeError: dictionary changed size during iteration` on Python 3.14/pyte combinations.

Window/font width changes also no longer call pyte's destructive in-place `Screen.resize()` for the displayed state. PowerTerm keeps a bounded raw-output replay journal and reconstructs the emulator at the new terminal geometry, while also sending the new rows/columns to the PTY so interactive programs can redraw. This avoids permanently losing cells clipped from the right/top when shrinking a window.

## Tab and Files-toolbar spacing polish

- Terminal tab close buttons are pulled inward from the trapezoid's sloped right edge.
- Trapezoid tabs now have subtle rounded corners.
- The Files toolbar has additional internal bottom padding so its buttons no longer sit against the lower border.

## Cursor preservation on resize

Replay-based screen reconstruction now preserves the live terminal cursor relative to the bottom of the viewport instead of accepting the cursor position produced by replaying old wrapped output at a new width. This fixes post-resize typing appearing in the middle of the terminal.

Terminal tab titles and close controls are also shifted another 4 px to the left.


## Tab alignment, startup prompt and configuration

- Hosts / Files / Saved Commands tab titles remain centered; only terminal-session titles receive the 4 px left shift.
- Terminal close buttons are inset farther from the right sloped edge.
- Resize cursor preservation now keeps absolute terminal row/column coordinates rather than anchoring the cursor to the bottom edge.
- PowerShell clears the screen after one-time PSReadLine setup so the first visible content is the actual interactive prompt.

## Finding the configuration file

Appearance and Terminal Settings now displays the exact `config.json` path being used and includes an **Open Config Folder** button. The configuration remains per-user and outside the application installation directory, which is appropriate for Windows executables and Debian/APT packages alike.

## Explicit terminal-tab close-button positioning

The custom trapezoid tab bar now positions close buttons directly instead of relying on Qt stylesheet margins.

The key tuning value is:

```python
self.close_button_inset = 20
```

Increase it to move the close `×` farther left; decrease it to move the `×` closer to the right edge.

## Terminal-tab positioning

PowerTerm now exposes two independent values in `TrapezoidTabBar`:

```python
self.title_shift_x = 0
self.close_button_inset = 20
```

- Increase `title_shift_x` to move the terminal tab title **right**.
- Decrease `title_shift_x` to move the terminal tab title **left**.
- Increase `close_button_inset` to move the close `×` **left**.
- Decrease `close_button_inset` to move the close `×` **right**.

For example:

```python
self.title_shift_x = 4
self.close_button_inset = 24
```

moves the title 4 px right and the close button 4 px left compared with the current defaults.

## Close-button layout fix

PowerTerm now repositions terminal-tab close buttons *after* Qt completes its own tab layout. This prevents Qt from moving the close widgets back to the right after the custom `tabLayoutChange()` code runs.

The position is still controlled by:

```python
self.close_button_inset = 20
```

Increasing that value moves the close `×` farther left.

## Fully custom terminal-tab close button

PowerTerm no longer uses Qt's native tab close widget. The custom trapezoid tab bar paints and hit-tests its own `×`, so its position is now completely independent of Qt's internal title/close-button layout.

Useful tuning values:

```python
self.title_shift_x = 0
self.close_button_inset = 15
self.close_button_reserved_width = 24
```

- Increase `title_shift_x` to move the title right.
- Increase `close_button_inset` to move the `×` left.
- Increase `close_button_reserved_width` only if you want more guaranteed separation between the title and the `×`.

## Text-file encoding support

The built-in editor no longer requires UTF-8 only. It now detects and preserves:

- UTF-8
- UTF-8 with BOM
- UTF-16 / UTF-16 LE / UTF-16 BE
- UTF-32 with BOM
- the operating system's preferred text encoding
- Windows-1252 as a legacy Windows fallback

The editor status row shows the detected encoding, and Save writes back using the same encoding and original line-ending convention.

## File-pane resize and navigation fixes

- The left navigation pane can be narrowed but cannot be collapsed completely; its minimum width is 220 px.
- Remote Name stretches while Size is an independently resizable column with a 32 px minimum.
- Local file-table sections also allow small interactive column widths.
- Selecting a remote file no longer changes the address bar/current directory.
- Double-clicking a remote folder is treated as actual browser navigation.
- Entering a remote file path into the address bar is rejected cleanly instead of trying to load it as a directory.

## Saved Commands toolbar layout

The Saved Commands actions now use two rows:

- Row 1: `+ Group`, `+ Command`, with `Run` retained at the far right.
- Row 2: `Edit`, `Remove`, `Up`, `Down`.

The command tree remains explicitly added below the action area with stretch priority, so saved command groups and commands continue to occupy the remaining pane height.

## Maintenance and native packaging

The Linux checkout is the authoritative repository and build environment. These
scripts locate project files relative to themselves; no development path is baked
in. Application code and the GPL-3.0 licence are unchanged.

### One-command workflow

From the project root, run:

```bash
python3 workflow.py
python3 workflow.py "Describe the change"
```

On Windows use `python workflow.py`. No virtual-environment activation is needed.
The console uses horizontal separators, numbered stages, spaced menu options,
and a final build-results section with output paths. Formatting is plain text so
it remains readable in terminal sessions and saved logs.
The workflow creates or reuses the project's `.venv`, installs missing Python
application and build dependencies from `requirements-build.txt`, checks dependency
consistency and library imports, runs the separate test script, then offers GitHub
and build menus. Existing dependencies are kept when they
satisfy the requirements. It does not install into the system Python.

After tests, choose `1` to review and push, `2` to skip GitHub and choose builds,
or `0` to finish. The push script shows the commit message, offers a tracked-file
diff, and requires a final confirmation before staging or pushing anything.
A failed setup, failed test, failed push, or cancelled push stops later steps.
Explicitly skipping GitHub opens the build menu using your current local files.
Build failures cannot undo a push that has already succeeded. Every script can
still run separately.

Python and Git must already be installed. If Python lacks both `ensurepip` and an
external pip, install your distribution's Python venv/pip packages first. OS shared
libraries and optional packaging tools/SDKs are not installed by this workflow;
missing native libraries stop the import/test checks, and unavailable packaging
targets are reported by the build menu as described below.

### Tests (separate from pushing and building)

Activate the project's virtual environment, install `requirements.txt`, then run:

```bash
python scripts/test.py
```

This runs unittest discovery with Qt's offscreen platform by default and returns
a nonzero exit status on failure. Neither the push script nor build scripts run
tests; run them explicitly before publishing changes.

### Explicit Git push

From anywhere inside this checkout:

```bash
python scripts/push.py
python scripts/push.py "Describe the change"
```

The script requires this repository on `main`, validates both effective fetch and
push URLs for `origin` against `github.com/prab-s/powerterm` (HTTPS or SSH), rejects
in-progress merge/rebase operations, and displays status including untracked files.
It fetches remote `main` and stops if local `main` is behind or diverged. Reconcile
branches manually; the script never switches branches, merges, or force-pushes.

After showing outgoing commits and the commit message, choose `1` to confirm,
`2` to review the tracked-file diff, or `0` to cancel (the default). `yes` is also
accepted as confirmation. Confirming stages **all
non-ignored changes**, including deletions, commits if there are staged changes,
and pushes only `main` to `origin main`. Review the displayed files first. The
optional message defaults to an interactive prompt, then `Update PowerTerm`.
An unchanged tree skips the commit and can still push previously created commits.
Cancellation does not stage, commit, or push. A failed push leaves local commits
intact. Cancellation exits with status 2. The root `workflow.py` calls this script
for its push step; the test and build scripts never push to GitHub.

### Build setup and selection

Use Python 3.11+ and an activated virtual environment on the target OS:

```bash
python -m pip install -r requirements-build.txt
```

On Linux, creating a virtual environment may first require your distribution's
`python3-venv` package. Build commands:

```bash
./scripts/build-linux.sh
./scripts/build-linux.sh linux
./scripts/build-linux.sh all --version 0.1.0
```

For Windows, select `1` in the Linux build menu, or run:

```bash
python3 scripts/build.py windows
```

This creates `dist/PowerTerm-0.1.0-windows-build-kit.zip`. Copy/download it to
Windows, extract the entire ZIP, then double-click **BUILD-WINDOWS.bat**. Install
Python 3.11+ with pip and the Python launcher on Windows first; internet access is
needed for dependencies. The launcher creates a Windows virtual environment,
installs the requirements and builds `dist/PowerTerm-Portable.exe`. It pauses on completion
or failure so the output remains visible. `START-HERE.txt` contains instructions.

The ZIP includes application source, icon, original licence, requirements and the
necessary build scripts. It excludes Git history, push scripts, virtual environments,
local settings and Linux binaries. No Git installation is needed on Windows.
This is a source/build kit; the `.exe` is produced on Windows, not on Linux.

If you already have the project files on Windows, the launcher also works directly:

```bat
scripts\build-windows.bat
scripts\build-windows.bat windows
```

`python scripts/build.py` also works directly on either OS. Without targets, the
numbered menu shows each option and its availability:

1. Windows Portable executable (on Windows) or portable build ZIP (on Linux)
2. Linux standalone executable
3. Linux AppImage
4. Linux Flatpak
5. Debian/Ubuntu package
6. Windows MSI installer (on Windows) or MSI build ZIP (on Linux)

Enter one number, several separated by commas (for example `1,2,5`), or `A` for
all available choices. Review the selection and answer `y` to continue. The
script then asks for the release version used in package filenames and metadata;
press Enter to use `0.1.0`. MSI builds require a three-part version such as
`0.1.1`. Enter `0` or press Enter at the selection prompt to finish without
building. Invalid or unavailable selections explain the problem and let you
choose again.

For direct commands, names and numbers work, including multiple targets:

```bash
python scripts/build.py windows linux deb
python scripts/build.py 1,2,5 --version 0.1.0
```

`all` creates every output whose prerequisites are available, including the
Windows Portable and MSI transfer ZIPs on Linux. It
prints unavailable targets, continues after individual failures, and returns
nonzero if a selected build fails or no targets are available. Supplying an
unavailable target directly fails. Scripts do not install host tools, download
SDKs, install packages into the system, or publish artifacts.

| Target | Output in `dist/` | Requirements |
| --- | --- | --- |
| `windows` on Linux | `PowerTerm-VERSION-windows-build-kit.zip` | Python and the project files |
| `windows` on Windows | `PowerTerm-Portable.exe` | Windows, application dependencies and PyInstaller (installed by the Windows launcher) |
| `msi` on Linux | `PowerTerm-VERSION-windows-msi-build-kit.zip` | Python and the project files |
| `msi` on Windows | `PowerTerm-VERSION-ARCH.msi` | Windows, Python build dependencies, WiX 4.0.6 and matching UI extension |
| `linux` | `PowerTerm-VERSION-ARCH` | Linux, application dependencies and PyInstaller |
| `appimage` | `PowerTerm-VERSION-ARCH.AppImage` | Linux, Python build dependencies and `appimagetool` on PATH |
| `deb` | `powerterm_VERSION_ARCH.deb` | Linux, Python build dependencies, `dpkg` and `dpkg-deb` |
| `flatpak` | `PowerTerm-VERSION-ARCH.flatpak` | Linux, `flatpak`, Freedesktop SDK and Platform, network access for Python dependencies |

The build menu offers `0.1.0` as its default package version; pass `--version`
to use a specific value without being prompted. This is packaging metadata, not
a change to the app. Temporary build files go under
`build/`; successful artifacts replace matching filenames in `dist/`. Old
artifacts from previous runs remain, so check the `OK`/`FAILED` build results.

PyInstaller bundles Python, app dependencies, the SVG icon and unchanged GPL
licence. Windows Portable uses one-file GUI mode; the MSI uses a directory build.
Both include the Windows PTY backend.
Linux executable mode uses one file; AppImage and Debian wrap a one-directory
bundle. Running the finished executable needs no Python installation; building
the Windows kit does require Python. Native
system libraries and services are still required. Windows builds must run on
Windows and Linux builds on Linux, matching the destination architecture:
[PyInstaller's platform restrictions](https://www.pyinstaller.org/en/stable/operating-mode.html).

Build Linux releases on the oldest distribution you intend to support. Bundling
does not make glibc or Qt's system requirements portable to older distributions.
The Debian package declares common Qt system dependencies but has not been
qualified against every Debian/Ubuntu release. AppImage creation follows the
[AppDir format](https://docs.appimage.org/reference/appdir.html); `appimagetool`
may require FUSE or an extracted tool installation on restricted servers.
Smoke-test each artifact on its destination desktop, including local terminals,
SSH, file access, icon display and credential storage, before distribution.

### Windows MSI installation and upgrades

Choose menu option `6` or prepare an MSI build kit on Linux with:

```bash
python3 scripts/build.py msi --version 0.1.1
```

Extract the resulting ZIP on Windows and follow `WINDOWS-MSI.md` to install WiX
and its UI extension, then run `BUILD-WINDOWS.bat`. The MSI kit selects the MSI
target automatically. The full [Windows MSI guide](scripts/WINDOWS-MSI.md) also
covers building both Portable and MSI outputs in one command.

The MSI installs unpacked application files into Program Files with a Start menu
shortcut and an Installed Apps entry. This avoids the portable executable's
extraction step at launch; the total installed size is not necessarily smaller.
It requires administrator approval to install, but users run PowerTerm normally.

Increment the three-part version for every release. A newer MSI replaces the
previous MSI installation; older versions are blocked. Same-version rebuilds
replace the existing installation rather than creating a duplicate entry. This
uses a stable product-family UpgradeCode and transactional removal of the old
payload, as described in [WiX's major-upgrade documentation](https://docs.firegiant.com/wix/schema/wxs/majorupgrade/).

Close PowerTerm before upgrading. The installer does not own `config.json` or
`hosts.json` and never deletes the per-user configuration directory, including
during uninstall. The app's Qt application/organization names remain unchanged,
so the existing configuration is reused for the same Windows user. Passwords stay
in the OS credential store. Existing portable EXEs are not removed by the MSI.

The MSI build and actual install/upgrade/uninstall sequence must be validated on
Windows before distributing a release. Linux checks cover the generated installer
recipe and build-kit contents, not Windows Installer execution. The guide includes
a test checklist for verifying configuration retention across upgrades.

### Experimental Flatpak

Install matching `org.freedesktop.Sdk` and `org.freedesktop.Platform` runtimes
first. The default branch is `25.08`; override with `--flatpak-branch` if needed.
For example, if the Flathub remote is already configured:

```bash
flatpak install --user flathub org.freedesktop.Sdk//25.08 org.freedesktop.Platform//25.08
./scripts/build-linux.sh flatpak
```

This local packaging script builds PyInstaller **inside the SDK**, fetching Python
build dependencies there through pip, then exports a `.flatpak` bundle using the
[Flatpak build commands](https://docs.flatpak.org/en/latest/first-build.html).
The SDK must provide Python and pip. It does not reuse a host-built binary. This
is an experimental local build, not an offline, reproducible Flathub submission.
Installing the bundle requires the matching runtime; the bundle does not embed it.

The sandbox grants home-directory access, networking, display/GPU access and
access to the Secret Service credential store. Local terminals use `/bin/sh`
**inside the sandbox**, and system-information commands see the sandbox's view.
Host shells, arbitrary host paths, and host administration do not behave like
the native executable. The launcher sets the sandbox shell without modifying
application code. Use the native Linux build when host-terminal behaviour is
required; Flatpak needs further integration work for that use case.

The original `LICENSE` remains unchanged and is included in packages and beside
artifacts. When distributing binaries, also provide corresponding source and
applicable dependency licence notices under their respective terms.
