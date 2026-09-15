# PowerTerm

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
