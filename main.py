import codecs
import hashlib
import html
import locale
import json
import os
import posixpath
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import threading
import tempfile
import time
import uuid
import queue
from datetime import datetime
from collections import deque
from pathlib import Path
from urllib.error import URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import paramiko
import psutil
import pyte
import keyring

from PySide6.QtCore import QDir, QEventLoop, QFileSystemWatcher, QObject, QStandardPaths, Qt, QTimer, Signal, QRect, QSize, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileSystemModel,
    QFormLayout, QGridLayout, QFileDialog, QFontDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QComboBox, QHeaderView, QGroupBox,
    QSplitter, QStackedWidget, QStyle, QTabBar, QTabWidget, QTreeView, QTreeWidget, QProgressBar,
    QTreeWidgetItem, QVBoxLayout, QWidget, QToolBar, QToolButton, QFrame, QSizePolicy, QAbstractItemView,
)


ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
GITHUB_LATEST_RELEASE_URL = "https://api.github.com/repos/prab-s/powerterm/releases/latest"
GITHUB_RELEASES_URL = "https://github.com/prab-s/powerterm/releases"


def application_version():
    """Read packaging metadata when present, with a source-tree fallback."""
    roots = [Path(__file__).resolve().parent]
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        roots.insert(0, Path(bundle_root))
    for root in roots:
        try:
            value = (root / "PACKAGE-VERSION.txt").read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass
    return "0.1.0"


APP_VERSION = application_version()

def strip_ansi(text: str) -> str:
    """Remove ANSI/VT escape sequences from captured command output."""
    if not text:
        return ""
    return ANSI_ESCAPE_RE.sub("", text)


def file_fingerprint(path):
    """Return a content fingerprint suitable for detecting external saves."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    attrs = os.stat(path)
    return attrs.st_size, digest.hexdigest()


def prune_nested_delete_targets(targets, remote=False):
    """Remove duplicate targets and children already covered by a folder."""
    separator = "/" if remote else os.sep
    normalized = []
    for path, is_dir in targets:
        value = posixpath.normpath(path) if remote else os.path.normpath(path)
        key = value if remote else os.path.normcase(value)
        normalized.append((value, bool(is_dir), key))
    result = []
    for path, is_dir, key in sorted(normalized, key=lambda item: (len(item[2]), item[2])):
        if any(parent_is_dir and (key == parent_key or key.startswith(parent_key.rstrip(separator) + separator))
               for _, parent_is_dir, parent_key in result):
            continue
        if not any(key == existing_key for _, _, existing_key in result):
            result.append((path, is_dir, key))
    return [(path, is_dir) for path, is_dir, _ in result]


def bash_private_command(command: str) -> str:
    """Wrap a Bash command so it is silent, unrecorded, and state-neutral."""
    # Bash may add an interactive input line before executing its first token,
    # so merely starting with `set +o history` is insufficient. Capture the
    # current history number first and explicitly delete that wrapper entry.
    # The leading space plus temporary ignorespace is defence in depth for
    # shells whose history admission is delayed until execution completes.
    return (
        " { __pt_private_marker=__powerterm_private_command__; __pt_hn=$HISTCMD; "
        "__pt_latest=\"$(history 1)\"; __pt_hc_was_set=${HISTCONTROL+x}; "
        "__pt_hc=$HISTCONTROL; if shopt -qo history; then __pt_hist_was_on=1; else __pt_hist_was_on=0; fi; "
        "case :${HISTCONTROL-}: in *:ignorespace:*|*:ignoreboth:*) ;; "
        "*) HISTCONTROL=\"${HISTCONTROL:+$HISTCONTROL:}ignorespace\" ;; esac; "
        "set +o history; case \"$__pt_latest\" in *__powerterm_private_command__*) history -d \"$__pt_hn\" ;; esac; { " + command + "; }; "
        "if [ \"$__pt_hc_was_set\" = x ]; then HISTCONTROL=$__pt_hc; "
        "else unset HISTCONTROL; fi; if [ \"$__pt_hist_was_on\" = 1 ]; then set -o history; else set +o history; fi; "
        "unset __pt_private_marker __pt_hn __pt_latest __pt_hc_was_set __pt_hc __pt_hist_was_on; "
        "} >/dev/null 2>&1"
    )


def bash_cwd_hook_command() -> str:
    """Return the private one-time setup for shell cwd reporting.

    Bash normally keeps recent commands only in the current process until it
    exits.  Appending after each accepted prompt makes those normal commands
    available to a replacement SSH shell after PowerTerm restarts a session.
    This does not change HISTFILE, HISTCONTROL, or the user's shell startup
    files, and the outer private wrapper removes the installer itself.
    """
    return r'''if [ -n "$BASH_VERSION" ]; then mapfile -t __pt_old < <(history | sed -n '/__powerterm_cwd_report/ s/^ *\([0-9][0-9]*\).*/\1/p'); for ((__pt_i=${#__pt_old[@]}-1;__pt_i>=0;__pt_i--)); do history -d "${__pt_old[__pt_i]}"; done; unset __pt_old __pt_i; __powerterm_last_pwd=; __powerterm_cwd_report(){ history -a; [ "$PWD" = "$__powerterm_last_pwd" ] && return; __powerterm_last_pwd=$PWD; printf "\033]7;file://%s%s\007" "${HOSTNAME:-localhost}" "$PWD"; }; if [[ -n "$PROMPT_COMMAND" ]]; then PROMPT_COMMAND="__powerterm_cwd_report;$PROMPT_COMMAND"; else PROMPT_COMMAND="__powerterm_cwd_report"; fi; elif [ -n "$ZSH_VERSION" ]; then typeset -ga precmd_functions; precmd_functions+=(__powerterm_cwd_report); __powerterm_last_pwd=; function __powerterm_cwd_report { [[ "$PWD" = "$__powerterm_last_pwd" ]] && return; __powerterm_last_pwd=$PWD; printf "\033]7;file://%s%s\007" "${HOST:-localhost}" "$PWD"; }; fi'''


def ssh_auth_probe_options(password):
    """Avoid slow key/agent probing when an explicit password is available."""
    probe_keys = not bool(password)
    return {"look_for_keys": probe_keys, "allow_agent": probe_keys}


def decode_text_bytes(data: bytes):
    """Decode a text file while preserving a useful encoding label for saving."""
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig"), "utf-8-sig", "UTF-8 with BOM"

    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16"), "utf-16", "UTF-16"

    if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
        return data.decode("utf-32"), "utf-32", "UTF-32"

    # UTF-8 remains the preferred default.
    try:
        return data.decode("utf-8"), "utf-8", "UTF-8"
    except UnicodeDecodeError:
        pass

    # BOM-less UTF-16 commonly contains alternating NUL bytes. Check that
    # before treating NULs as evidence of a binary file.
    sample = data[:8192]
    if sample:
        even_nuls = sample[0::2].count(0)
        odd_nuls = sample[1::2].count(0)
        pairs = max(1, len(sample) // 2)

        if odd_nuls / pairs > 0.30 and even_nuls / pairs < 0.10:
            try:
                return data.decode("utf-16-le"), "utf-16-le", "UTF-16 LE"
            except UnicodeDecodeError:
                pass

        if even_nuls / pairs > 0.30 and odd_nuls / pairs < 0.10:
            try:
                return data.decode("utf-16-be"), "utf-16-be", "UTF-16 BE"
            except UnicodeDecodeError:
                pass

    # After UTF-16 checks, embedded NULs are a good binary-file signal.
    if b"\x00" in sample:
        raise ValueError("This appears to be a binary file and cannot be edited as text.")

    # Try the user's normal OS encoding. On many Windows systems this is a
    # Windows code page rather than UTF-8.
    preferred = locale.getpreferredencoding(False) or ""
    candidates = []
    if preferred:
        candidates.append((preferred, preferred))

    # cp1252 is a very common legacy Windows text encoding.
    candidates.append(("cp1252", "Windows-1252"))

    seen = set()
    for encoding, label in candidates:
        key = encoding.lower()
        if key in seen or key in ("utf-8", "utf8"):
            continue
        seen.add(key)
        try:
            return data.decode(encoding), encoding, label
        except (UnicodeDecodeError, LookupError):
            pass

    raise UnicodeDecodeError(
        "utf-8", data, 0, min(1, len(data)),
        "PowerTerm could not determine a supported text encoding"
    )


# ----------------------------- Terminal backends -----------------------------

class TerminalBackend(QObject):
    data = Signal(str)
    closed = Signal()
    error = Signal(str)
    connected = Signal()
    shell_ready = Signal()

    def write(self, text: str):
        raise NotImplementedError

    def resize(self, cols: int, rows: int):
        pass

    def close(self):
        pass


class UnixPtyBackend(TerminalBackend):
    def __init__(self, parent=None):
        super().__init__(parent)
        import fcntl
        import pty
        import struct
        import termios

        self._fcntl = fcntl
        self._termios = termios
        self._struct = struct
        self.master_fd, slave_fd = pty.openpty()
        shell = os.environ.get("SHELL") or "/bin/sh"
        self.proc = subprocess.Popen(
            [shell],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(Path.home()),
            start_new_session=True,
            close_fds=True,
            env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(slave_fd)
        self._alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        QTimer.singleShot(0, self.connected.emit)

    def _reader(self):
        try:
            while self._alive:
                data = os.read(self.master_fd, 65536)
                if not data:
                    break
                self.data.emit(data.decode("utf-8", errors="replace"))
        except OSError:
            pass
        finally:
            self.closed.emit()

    def write(self, text):
        if self._alive:
            try:
                os.write(self.master_fd, text.encode("utf-8"))
            except OSError:
                pass

    def resize(self, cols, rows):
        try:
            packed = self._struct.pack("HHHH", rows, cols, 0, 0)
            self._fcntl.ioctl(self.master_fd, self._termios.TIOCSWINSZ, packed)
        except OSError:
            pass

    def close(self):
        self._alive = False
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass


class WindowsPtyBackend(TerminalBackend):
    def __init__(self, parent=None, predictions_enabled=True, cwd_reporting=False):
        super().__init__(parent)
        try:
            from winpty import PtyProcess
        except ImportError as exc:
            raise RuntimeError(
                "Windows local terminals require pywinpty. Run: python -m pip install pywinpty"
            ) from exc

        pwsh = shutil.which("pwsh")
        powershell = shutil.which("powershell")
        comspec = os.environ.get("COMSPEC") or shutil.which("cmd") or "cmd.exe"

        if predictions_enabled:
            prediction_setup = (
                "try { "
                "Import-Module PSReadLine -ErrorAction SilentlyContinue; "
                "Set-PSReadLineOption -PredictionSource History -ErrorAction Stop; "
                "Set-PSReadLineOption -PredictionViewStyle InlineView -ErrorAction SilentlyContinue; "
                "Set-PSReadLineOption -Colors @{ InlinePrediction = '#666666' } -ErrorAction SilentlyContinue "
                "} catch { }"
            )
        else:
            # Some profiles/modules can alter PSReadLine after startup. Disable
            # predictions now and wrap the prompt so the setting is reasserted
            # before every command line is presented.
            prediction_setup = (
                "try { Import-Module PSReadLine -ErrorAction SilentlyContinue; Set-PSReadLineOption -PredictionSource None -ErrorAction SilentlyContinue } catch { }; "
                "$global:__bt_original_prompt = $function:prompt; "
                "function global:prompt { "
                "try { Set-PSReadLineOption -PredictionSource None -ErrorAction SilentlyContinue } catch { }; "
                "if ($global:__bt_original_prompt) { & $global:__bt_original_prompt } else { 'PS ' + (Get-Location) + '> ' } "
                "}"
            )

        if cwd_reporting:
            prediction_setup += (
                "; if (-not $global:__bt_old_prompt) { $global:__bt_old_prompt = $function:prompt }; "
                "function global:prompt { $e=[char]27; $b=[char]7; "
                "Write-Host -NoNewline \"$e]7;file://$env:COMPUTERNAME$((Get-Location).Path)$b\"; "
                "& $global:__bt_old_prompt }"
            )

        # Clear-Host at the end removes any visual artefacts generated by the
        # one-time PSReadLine setup before the first real interactive prompt.
        prediction_setup = prediction_setup + "; Clear-Host"

        if pwsh:
            argv = [pwsh, "-NoLogo", "-NoExit", "-Command", prediction_setup]
            self.shell_kind = "powershell"
        elif powershell:
            argv = [powershell, "-NoLogo", "-NoExit", "-Command", prediction_setup]
            self.shell_kind = "powershell"
        else:
            argv = [comspec]
            self.shell_kind = "cmd"
        self._powerterm_cwd_hook = bool(cwd_reporting and self.shell_kind == "powershell")
        self._powerterm_cwd_hook_version = 3 if self._powerterm_cwd_hook else 0

        env = dict(os.environ)
        env.setdefault("TERM", "xterm-256color")

        try:
            self.proc = PtyProcess.spawn(
                argv,
                cwd=str(Path.home()),
                env=env,
                dimensions=(30, 100),
            )
        except Exception as exc:
            raise RuntimeError(f"Could not start the Windows terminal: {exc}") from exc

        self._alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        QTimer.singleShot(0, self.connected.emit)

    def _reader(self):
        try:
            while self._alive and self.proc.isalive():
                try:
                    text = self.proc.read(4096)
                except EOFError:
                    break
                if text:
                    self.data.emit(text)
        except Exception as exc:
            if self._alive:
                self.error.emit(str(exc))
        finally:
            self.closed.emit()

    def write(self, text):
        if self._alive and self.proc.isalive():
            self.proc.write(text)

    def resize(self, cols, rows):
        try:
            self.proc.setwinsize(rows, cols)
        except Exception:
            pass

    def close(self):
        self._alive = False
        try:
            self.proc.terminate(force=True)
        except Exception:
            pass


class SshBackend(TerminalBackend):
    # Paramiko channels do not expose a Qt-ready signal. Keep the fallback
    # poll short enough that Bash echo/readline redraws do not feel key-bound.
    ssh_poll_interval = 0.002

    def __init__(self, host, port, username, password, parent=None, autostart=True):
        super().__init__(parent)
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client = None
        self.channel = None
        self._alive = True
        self._shell_ready = False
        self._started = False
        self._pty_size = (100, 30)
        if autostart:
            self.start()

    def start(self):
        """Open the SSH channel after the terminal has supplied its size."""
        if self._started or not self._alive:
            return
        self._started = True
        threading.Thread(target=self._connect_and_read, daemon=True).start()

    def _connect_and_read(self):
        try:
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password or None,
                **ssh_auth_probe_options(self.password),
                timeout=10,
            )
            # Bash/readline is an interactive, tiny-packet workload. Disable
            # Nagle coalescing when Paramiko exposes its transport socket so
            # keystroke echo and ANSI redraws are not held for packet batching.
            try:
                transport = client.get_transport()
                if transport and getattr(transport, "sock", None):
                    transport.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except (OSError, AttributeError):
                pass
            self.client = client
            width, height = self._pty_size
            self.channel = client.invoke_shell(
                term="xterm-256color", width=width, height=height
            )
            self.channel.settimeout(0.25)
            # Authentication and transport are ready now. File-browser work
            # can begin without waiting for shell startup/profile output.
            self.connected.emit()
            # Drain the login banner, shell startup output, and first prompt
            # before installing the private prompt hook. Stream each chunk as
            # it arrives so the terminal paints immediately.
            initial_started = None
            initial_deadline = time.monotonic() + 2.0
            while self._alive and time.monotonic() < initial_deadline:
                if self.channel.recv_ready():
                    data = self.channel.recv(65536)
                    if not data:
                        break
                    self.data.emit(data.decode("utf-8", errors="replace"))
                    initial_started = time.monotonic()
                    continue
                if initial_started is not None and time.monotonic() - initial_started >= 0.06:
                    break
                time.sleep(self.ssh_poll_interval)
            self._shell_ready = True
            self.shell_ready.emit()

            while self._alive and not self.channel.closed:
                try:
                    # Paramiko's blocking recv wakes as soon as channel data is
                    # available. Polling added up to one poll interval to every
                    # remote keystroke echo and burned CPU while idle.
                    data = self.channel.recv(65536)
                    if not data:
                        break
                    self.data.emit(data.decode("utf-8", errors="replace"))
                except socket.timeout:
                    pass
        except Exception as exc:
            self.error.emit(f"SSH connection failed: {exc}")
        finally:
            try:
                if self.channel:
                    self.channel.close()
                if self.client:
                    self.client.close()
            except Exception:
                pass
            self.closed.emit()

    def write(self, text):
        if self.channel and not self.channel.closed:
            try:
                self.channel.sendall(text)
            except Exception as exc:
                self.error.emit(str(exc))

    def resize(self, cols, rows):
        self._pty_size = (max(20, int(cols)), max(5, int(rows)))
        if self.channel and not self.channel.closed:
            try:
                self.channel.resize_pty(width=self._pty_size[0], height=self._pty_size[1])
            except Exception:
                pass

    def run_command(self, command, timeout=5):
        if not self.client:
            raise RuntimeError("SSH session is not connected")
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        status_code = stdout.channel.recv_exit_status()
        return status_code, out, err

    def run_command_lines(self, command, on_line, timeout=5):
        """Execute a command and deliver newline records as they arrive."""
        if not self.client:
            raise RuntimeError("SSH session is not connected")
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        for raw in stdout:
            on_line(raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw)
        error = stderr.read().decode("utf-8", errors="replace")
        status_code = stdout.channel.recv_exit_status()
        return status_code, error

    def close(self):
        self._alive = False
        try:
            if self.channel:
                self.channel.close()
            if self.client:
                self.client.close()
        except Exception:
            pass


# ----------------------------- Terminal widget -------------------------------

ANSI = {
    "black": "#000000", "red": "#cc0000", "green": "#4e9a06",
    "brown": "#c4a000", "blue": "#3465a4", "magenta": "#75507b",
    "cyan": "#06989a", "white": "#d3d7cf", "brightblack": "#555753",
    "brightred": "#ef2929", "brightgreen": "#8ae234", "brightbrown": "#fce94f",
    "brightblue": "#729fcf", "brightmagenta": "#ad7fa8",
    "brightcyan": "#34e2e2", "brightwhite": "#eeeeec",
}


def colour(value, default):
    if value in (None, "default"):
        return QColor(default)
    if value in ANSI:
        return QColor(ANSI[value])
    if isinstance(value, str):
        candidate = QColor(value if value.startswith("#") else "#" + value)
        if candidate.isValid():
            return candidate
    return QColor(default)



class TerminalFindDialog(QDialog):
    """Modeless find panel with live result refresh and per-occurrence highlighting."""
    def __init__(self, terminal, parent=None):
        super().__init__(parent or terminal)
        self.terminal = terminal
        self.matches = []
        self.current_index = -1
        self._last_search = None
        self._dirty = True
        self.setWindowTitle("Find in Terminal")
        self.resize(820, 460)

        self.query = QLineEdit(terminal._find_text)
        self.query.setClearButtonEnabled(True)
        self.query.setPlaceholderText("Find text…")
        self.match_case = QCheckBox("Match case")
        self.previous = QPushButton("Previous")
        self.next = QPushButton("Next")
        self.find_all = QPushButton("Find All")
        self.count_label = QLabel("0 matches")

        top = QHBoxLayout()
        top.addWidget(QLabel("Find:"))
        top.addWidget(self.query, 1)
        top.addWidget(self.match_case)
        top.addWidget(self.previous)
        top.addWidget(self.next)
        top.addWidget(self.find_all)

        self.results = QTreeWidget()
        self.results.setHeaderLabels(["Line", "Content"])
        self.results.setColumnWidth(0, 70)
        self.results.setAlternatingRowColors(True)
        self.results.itemDoubleClicked.connect(self._activate_item)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.results, 1)
        bottom = QHBoxLayout()
        bottom.addWidget(self.count_label)
        bottom.addStretch(1)
        bottom.addWidget(close)
        layout.addLayout(bottom)

        self.find_all.clicked.connect(self.refresh_results)
        self.next.clicked.connect(lambda: self.cycle(1))
        self.previous.clicked.connect(lambda: self.cycle(-1))
        self.query.returnPressed.connect(lambda: self.cycle(1))
        self.query.textChanged.connect(self._mark_dirty)
        self.match_case.toggled.connect(self._mark_dirty)
        QTimer.singleShot(0, self.refresh_results)

    def _search_key(self):
        return (self.query.text(), self.match_case.isChecked())

    def _mark_dirty(self, *_args):
        self._dirty = True
        self.count_label.setText("Search changed — Next/Previous will refresh")

    def _highlighted_html(self, line, start, end):
        before = html.escape(line[:start])
        matched = html.escape(line[start:end])
        after = html.escape(line[end:])
        return (
            "<span style='font-family: monospace; white-space: pre;'>"
            f"{before}<span style='background:#8a6d1d; color:#fff3b0; font-weight:700; "
            f"border:1px solid #c89b28;'>{matched}</span>{after}</span>"
        )

    def refresh_results(self, preserve_position=False):
        needle = self.query.text()
        self.terminal._find_text = needle
        old_index = self.current_index
        self.matches = []
        self.current_index = -1
        self.results.clear()
        self._last_search = self._search_key()
        self._dirty = False
        if not needle:
            self.count_label.setText("0 matches")
            return

        text = self.terminal.toPlainText()
        flags = 0 if self.match_case.isChecked() else re.IGNORECASE
        pattern = re.compile(re.escape(needle), flags)
        offset = 0
        match_number = 0
        for line_number, line in enumerate(text.splitlines(True), 1):
            visible_line = line.rstrip("\r\n")
            for match in pattern.finditer(visible_line):
                start = offset + match.start()
                length = match.end() - match.start()
                self.matches.append((start, length, line_number, visible_line, match.start(), match.end()))
                item = QTreeWidgetItem([str(line_number), ""])
                item.setData(0, Qt.ItemDataRole.UserRole, match_number)
                self.results.addTopLevelItem(item)
                label = QLabel(self._highlighted_html(visible_line, match.start(), match.end()))
                label.setTextFormat(Qt.TextFormat.RichText)
                label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
                label.setContentsMargins(4, 1, 4, 1)
                self.results.setItemWidget(item, 1, label)
                match_number += 1
            offset += len(line)

        total = len(self.matches)
        self.count_label.setText(f"{total} match{'es' if total != 1 else ''}")
        if self.matches:
            self.jump_to(min(old_index, total - 1) if preserve_position and old_index >= 0 else 0)

    def _ensure_current_search(self):
        if self._dirty or self._last_search != self._search_key():
            self.refresh_results()

    def cycle(self, delta):
        self._ensure_current_search()
        if not self.matches:
            return
        self.jump_to((self.current_index + delta) % len(self.matches))

    def jump_to(self, index):
        if not (0 <= index < len(self.matches)):
            return
        self.current_index = index
        start, length, line_number, _line, _line_start, _line_end = self.matches[index]
        cursor = self.terminal.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
        self.terminal.setTextCursor(cursor)
        self.terminal.ensureCursorVisible()
        item = self.results.topLevelItem(index)
        if item:
            self.results.setCurrentItem(item)
            self.results.scrollToItem(item)
        self.count_label.setText(f"Match {index + 1} of {len(self.matches)} — line {line_number}")

    def _activate_item(self, item, _column):
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(index, int):
            self.jump_to(index)


class TerminalLineNumberArea(QWidget):
    def __init__(self, terminal):
        super().__init__(terminal)
        self.terminal = terminal

    def sizeHint(self):
        return QSize(self.terminal.line_number_area_width(), 0)

    def _position_close_buttons(self):
        """Place tab close widgets after Qt has completed its own layout pass."""
        if not self.tabsClosable():
            return

        for index in range(self.count()):
            button = self.tabButton(index, QTabBar.ButtonPosition.RightSide)
            if button is None:
                button = self.tabButton(index, QTabBar.ButtonPosition.LeftSide)
            if button is None:
                continue

            rect = self.tabRect(index)
            x = rect.right() - button.width() - self.close_button_inset
            y = rect.top() + (rect.height() - button.height()) // 2

            button.move(x, y)
            button.raise_()

    def tabLayoutChange(self):
        super().tabLayoutChange()
        # Qt may reposition the close widgets after this callback. Queue our
        # placement for the next event-loop turn so our geometry wins last.
        QTimer.singleShot(0, self._position_close_buttons)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._position_close_buttons)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._position_close_buttons)

    def paintEvent(self, event):
        self.terminal.paint_line_number_area(event)


class TerminalWidget(QPlainTextEdit):
    cwd_changed = Signal(str)
    session_closed = Signal()
    close_requested = Signal()
    restart_requested = Signal()
    save_output_requested = Signal()
    focus_activated = Signal()
    OSC7_RE = re.compile(r"\x1b]7;([^\x07\x1b]*)(?:\x07|\x1b\\)")
    REDRAW_ERASE_RE = re.compile(r"\x1b\[[0-9;?]*[JKPX]")
    DELETE_CHARACTER_RE = re.compile(r"\x1b\[[0-9;?]*P")
    READLINE_REPOSITION_RE = re.compile(
        r"^(?P<up>(?:\x1b\[(?:\d+)?A)+)\r(?P<right>(?:\x1b\[(?:\d+)?C)+)"
    )
    READLINE_SAME_ROW_RE = re.compile(r"^\r(?P<right>(?:\x1b\[(?:\d+)?C)+)")
    SEMANTIC_TOKEN_RE = re.compile(
        r"(https?://\S+|(?:[A-Za-z]:\\|/)[^\s'\"<>|]+|"
        r"--?[A-Za-z][\w-]*|\b(?:error|failed|failure|fatal|denied)\b|"
        r"\b(?:warning|warn)\b|\b(?:success|successful|passed|ok)\b|"
        r"\b(?:0x[0-9A-Fa-f]+|\d+(?:\.\d+)?%?)\b)",
        re.IGNORECASE,
    )
    interactive_render_delay_ms = 0

    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFont("Monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(11)
        self.setFont(font)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabChangesFocus(False)
        # The document contains the complete terminal scrollback. Keeping its
        # native scrollbar lets Qt retain mouse selections as their text moves
        # through the viewport, rather than reusing one screen-high canvas.
        # Keep it visible even with little output so its width never changes
        # the PTY geometry midway through a session.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QPlainTextEdit { background:#101010; color:#eeeeec; border:0; padding:6px; }")
        self.scrollback_limit = 10000
        self.screen = self._new_screen(100, 30)
        self.stream = pyte.Stream(self.screen)
        self.backend = None
        self._session_active = False
        self._render_pending = False
        self._input_pending = False
        self._input_buffer = []
        self._history_scrolled = False
        self._history_offset = 0
        self._deferred_output = []
        self._deferred_output_chars = 0
        self._setting_scroll_position = False

        # Keep a bounded raw output journal. Width changes rebuild the emulator
        # from this journal instead of destructively clipping pyte's sparse
        # screen buffer.
        self._replay_chunks = deque()
        self._replay_chars = 0
        self._rebuilding_screen = False
        self._find_text = ""
        self._find_dialog = None
        self.cursor_style = "ibeam"
        self.cursor_blink = True
        self.syntax_highlighting = True
        self._format_cache = {}
        self._hidden_input_fragments = []
        self._private_startup_buffer = None
        self._private_startup_generation = 0
        self._rendered_rows = None
        self._rendered_history_count = 0
        self._rendered_history_first = None
        self._requires_full_document_rebuild = False
        self._redraw_control_tail = ""
        # pyte models DCH per physical row, while readline uses it to redraw
        # wrapped logical input. Keep any compatibility correction out of the
        # emulator and apply it only to a copied render snapshot.
        self._readline_overlay = {}
        self._pending_readline_prompt = None
        self._virtual_readline_prompt = None
        self._last_bell_time = 0.0
        self._cursor_on = True
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setInterval(500)
        self._cursor_timer.timeout.connect(self._toggle_cursor)
        self._cursor_timer.start()

        # PTYs redraw aggressively on resize (especially PowerShell and remote
        # interactive shells). Debounce Qt resize events so rendering the
        # document cannot create a resize/redraw feedback loop.
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(40)
        self._resize_timer.timeout.connect(self._apply_terminal_resize)
        # Keep the emulator geometry separate from the geometry last sent to
        # a backend. A terminal can be laid out before its backend is attached
        # (notably when a window opens maximized), so matching emulator
        # geometry must not suppress that backend's first PTY resize.
        self._last_terminal_size = None
        self._last_backend_size = None
        self._backend_geometry_needs_sync = False

        self.line_numbers_visible = False
        self.line_number_area = TerminalLineNumberArea(self)
        self.line_number_area.hide()
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.verticalScrollBar().valueChanged.connect(self._scroll_position_changed)
        self.update_line_number_area_width()

    def begin_private_startup_transaction(self, timeout_ms=2500):
        """Hide the one synthetic shell submission used to install hooks."""
        self._private_startup_buffer = ""
        self._private_startup_generation += 1
        generation = self._private_startup_generation
        QTimer.singleShot(
            timeout_ms,
            lambda g=generation: self._finish_private_startup_transaction(g, flush=True),
        )

    def _finish_private_startup_transaction(self, generation, flush=False):
        if generation != self._private_startup_generation or self._private_startup_buffer is None:
            return
        buffered = self._private_startup_buffer
        self._private_startup_buffer = None
        if flush and buffered:
            self.feed(buffered)

    def line_number_area_width(self):
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_line_number_area_width(self, *_args):
        width = self.line_number_area_width() if self.line_numbers_visible else 0
        self.setViewportMargins(width, 0, 0, 0)
        self.line_number_area.setVisible(self.line_numbers_visible)

    def set_line_numbers_visible(self, visible):
        self.line_numbers_visible = bool(visible)
        self.update_line_number_area_width()
        self.line_number_area.update()

    def update_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width()

    def paint_line_number_area(self, event):
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor("#151b22"))
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        painter.setFont(self.font())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#71808d"))
                painter.drawText(0, top, self.line_number_area.width() - 6, self.fontMetrics().height(),
                                 Qt.AlignmentFlag.AlignRight, str(block_number + 1))
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1
        painter.end()

    def _new_screen(self, columns, lines):
        history_cls = getattr(pyte, "HistoryScreen", None)
        if history_cls is not None:
            try:
                return history_cls(
                    columns, lines,
                    history=max(100, int(self.scrollback_limit)),
                    ratio=0.15
                )
            except TypeError:
                return history_cls(
                    columns, lines,
                    history=max(100, int(self.scrollback_limit))
                )
        return pyte.Screen(columns, lines)

    def _replay_limit_chars(self):
        # Roughly four bytes/characters per cell across the configured history,
        # bounded so resizing remains responsive even after very noisy sessions.
        estimate = int(self.scrollback_limit) * max(80, int(getattr(self.screen, "columns", 80))) * 4
        return max(2 * 1024 * 1024, min(16 * 1024 * 1024, estimate))

    def _record_replay(self, text):
        if not text or self._rebuilding_screen:
            return
        self._replay_chunks.append(text)
        self._replay_chars += len(text)
        limit = self._replay_limit_chars()
        while self._replay_chars > limit and len(self._replay_chunks) > 1:
            removed = self._replay_chunks.popleft()
            self._replay_chars -= len(removed)

    def _rebuild_screen(self, columns, lines, preserve_cursor=True):
        """Recreate terminal state at a new geometry without pyte.resize clipping.

        Replaying output is useful for reconstructing visible cells, but the cursor
        produced by replay is *not* authoritative after a width change because old
        wrapping decisions are being reinterpreted at the new width. Preserve the
        live cursor relative to the bottom edge instead.
        """
        columns = max(20, int(columns))
        lines = max(5, int(lines))
        raw = "".join(self._replay_chunks)

        old_columns = int(getattr(self.screen, "columns", columns))
        old_lines = int(getattr(self.screen, "lines", lines))
        old_cursor_x = int(getattr(self.screen.cursor, "x", 0))
        old_cursor_y = int(getattr(self.screen.cursor, "y", 0))

        self._rebuilding_screen = True
        try:
            new_screen = self._new_screen(columns, lines)
            new_stream = pyte.Stream(new_screen)
            if raw:
                new_stream.feed(raw)

            # Before the first backend output there is no terminal content to
            # anchor. Keeping the blank 100x30 screen's cursor relative to the
            # bottom would put the initial prompt partway down a larger window.
            if preserve_cursor and (raw or (old_cursor_x, old_cursor_y) != (0, 0)):
                # Interactive shells keep the active prompt relative to the
                # bottom edge. Preserving an absolute row while maximizing
                # leaves the cursor stranded around the middle of the canvas.
                bottom_offset = max(0, old_lines - 1 - old_cursor_y)
                mapped_y = max(0, min(lines - 1, lines - 1 - bottom_offset))
                mapped_x = max(0, min(columns - 1, old_cursor_x))
                try:
                    new_screen.cursor.y = mapped_y
                    new_screen.cursor.x = mapped_x
                except Exception:
                    pass

            self.screen = new_screen
            self.stream = new_stream
            self._rendered_rows = None
            self._rendered_history_count = 0
            self._rendered_history_first = None
            self._history_offset = 0
            self._history_scrolled = False
            self._clear_readline_overlay()
        finally:
            self._rebuilding_screen = False

    @staticmethod
    def _copy_terminal_line(line):
        try:
            return dict(line)
        except Exception:
            return {}

    def _history_snapshot(self):
        """Return immutable copies of scrollback + current screen lines."""
        result = []
        history = getattr(self.screen, "history", None)
        if history is not None:
            try:
                result.extend(self._copy_terminal_line(line) for line in list(history.top))
            except Exception:
                pass
        for y in range(self.screen.lines):
            try:
                result.append(self._copy_terminal_line(self.screen.buffer[y]))
            except Exception:
                result.append({})
        return result

    def _clear_readline_overlay(self):
        self._readline_overlay = {}
        self._pending_readline_prompt = None
        self._virtual_readline_prompt = None

    def _apply_readline_overlay(self, visible_lines):
        """Apply readline's wrapped-redraw correction to copied screen cells.

        This deliberately never writes to ``screen.buffer`` or its history.
        Changing pyte's state made old scrollback appear to lose rows when a
        history entry was redrawn several times.
        """
        if not self._readline_overlay:
            return visible_lines
        for (row, x), cell in self._readline_overlay.items():
            if 0 <= row < len(visible_lines) and 0 <= x < self.screen.columns:
                visible_lines[row][x] = cell
        return visible_lines

    def _scroll_position_changed(self, value):
        """Track whether Qt is showing history or the live terminal bottom."""
        if self._setting_scroll_position:
            return
        scrollbar = self.verticalScrollBar()
        self._history_scrolled = value < scrollbar.maximum()
        self._history_offset = max(0, scrollbar.maximum() - value)
        if not self._history_scrolled and self._deferred_output:
            self._return_to_live()

    def apply_settings(self, cursor_style="ibeam", cursor_blink=True, font_family="Monospace", font_size=11, scrollback_limit=10000, syntax_highlighting=True):
        self.cursor_style = cursor_style if cursor_style in ("ibeam", "block", "underline") else "ibeam"
        self.cursor_blink = bool(cursor_blink)
        self.syntax_highlighting = bool(syntax_highlighting)
        new_scrollback = max(100, int(scrollback_limit or 10000))
        if new_scrollback != self.scrollback_limit:
            self.scrollback_limit = new_scrollback
            if self.backend is not None:
                self._rebuild_screen(self.screen.columns, self.screen.lines)
        font = QFont(font_family or "Monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(max(7, int(font_size or 11)))
        self.setFont(font)
        self.document().setDefaultFont(font)
        self._last_terminal_size = None
        self._cursor_on = True
        # Font metrics directly determine terminal rows/columns. Recalculate
        # after Qt applies the font; the resize path rebuilds terminal state
        # rather than clipping the existing pyte buffer.
        QTimer.singleShot(0, self._send_resize)
        QTimer.singleShot(0, self.render_screen)
        if self.cursor_blink:
            self._cursor_timer.start()
        else:
            self._cursor_timer.stop()
        self.viewport().update()

    def _toggle_cursor(self):
        if self.cursor_blink:
            self._cursor_on = not self._cursor_on
            self.viewport().update()

    def reset_terminal(self):
        self.screen = self._new_screen(100, 30)
        self.stream = pyte.Stream(self.screen)
        self._input_buffer.clear()
        self._input_pending = False
        self._history_scrolled = False
        self._history_offset = 0
        self._deferred_output.clear()
        self._deferred_output_chars = 0
        self._replay_chunks.clear()
        self._replay_chars = 0
        self._redraw_control_tail = ""
        self._clear_readline_overlay()
        self._last_terminal_size = None
        self._last_backend_size = None
        self._backend_geometry_needs_sync = False
        self.clear()
        self._rendered_rows = None
        self._rendered_history_count = 0
        self._rendered_history_first = None
        self._send_resize()

    def set_backend(self, backend, reset=True):
        if self.backend:
            self.backend.close()
        if reset:
            self.reset_terminal()
        self.backend = backend
        # This is a newly attached process/channel even when its terminal
        # widget kept its current screen during a reconnect.
        self._last_backend_size = None
        self._backend_geometry_needs_sync = True
        self._session_active = True
        backend.data.connect(self.feed)
        backend.error.connect(lambda m: QMessageBox.warning(self, "Terminal", m))
        backend.closed.connect(self._backend_closed)
        self._send_resize()
        self.setFocus()

    def _backend_closed(self):
        if not self._session_active:
            return
        if self._private_startup_buffer is not None:
            self._finish_private_startup_transaction(
                self._private_startup_generation, flush=True
            )
        self._session_active = False
        self._cursor_on = False
        self._cursor_timer.stop()
        self.feed(
            "\r\n\r\n\x1b[0;31;1mSession stopped\x1b[0m\r\n"
            "   - Press \x1b[36;1m<return>\x1b[0m to exit tab\r\n"
            "   - Press \x1b[36;1mR\x1b[0m to restart session\r\n"
            "   - Press \x1b[36;1mS\x1b[0m to save terminal output to file\r\n"
        )
        self.viewport().update()
        self.session_closed.emit()

    def feed(self, text):
        for fragment in tuple(self._hidden_input_fragments):
            if fragment:
                text = text.replace(fragment, "")
        if self._private_startup_buffer is not None:
            self._private_startup_buffer += text
            match = self.OSC7_RE.search(self._private_startup_buffer)
            if not match:
                return
            # Everything before the first cwd report belongs to shell startup
            # and the synthetic installer submission. Keep the report itself
            # (for browser synchronisation) and the real prompt that follows.
            text = "\r\x1b[2K" + self._private_startup_buffer[match.start():]
            self._private_startup_buffer = None
            self._private_startup_generation += 1
        # While the user is looking back through history, keep that viewport stable.
        # Output is queued briefly and applied when they return to the live page.
        # An active mouse selection can move Qt's viewport by one row merely
        # to expose its endpoint. Do not treat that as a request to freeze
        # output: render it and preserve the selection/document position.
        # Deliberate history browsing without a selection still defers output.
        if self._history_scrolled and not self.textCursor().hasSelection():
            self._deferred_output.append(text)
            self._deferred_output_chars += len(text)
            # Do not allow unbounded buffering during a very noisy command.
            if self._deferred_output_chars >= 8 * 1024 * 1024:
                self._return_to_live()
            return
        self._input_buffer.append(text)
        # A remote echo/readline redraw is usually only a few bytes. Process
        # it in this GUI turn instead of imposing the bulk-output batching
        # delay on every keystroke. Larger output remains coalesced below.
        if len(text) <= 512:
            self._process_input_batch()
            return
        if not self._input_pending:
            self._input_pending = True
            QTimer.singleShot(12, self._process_input_batch)

    def _process_input_batch(self):
        self._input_pending = False
        if not self._input_buffer:
            return
        text = "".join(self._input_buffer)
        self._input_buffer.clear()
        previous_cursor = (self.screen.cursor.x, self.screen.cursor.y)
        prompt_snapshot = self._capture_readline_prompt(text, previous_cursor)
        # A local PTY can produce its initial prompt before the debounced Qt
        # layout timer runs. Resolve the displayed geometry here too, before
        # that prompt is interpreted, so a terminal opened in an already
        # maximized window cannot leave readline using the provisional size.
        if self._backend_geometry_needs_sync:
            self._apply_terminal_resize()
        # BEL (0x07) may also terminate OSC sequences, so strip OSC controls
        # before deciding whether the terminal actually rang its bell.
        bell_probe = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
        if "\x07" in bell_probe:
            now = time.monotonic()
            if now - self._last_bell_time >= 0.15:
                self._last_bell_time = now
                QApplication.beep()

        for match in self.OSC7_RE.finditer(text):
            try:
                parsed = urlparse(match.group(1))
                path = unquote(parsed.path or "")
                if re.match(r"^/[A-Za-z]:/", path):
                    path = path[1:]
                if path:
                    self.cwd_changed.emit(path)
            except Exception:
                pass
        redraw_probe = self._redraw_control_tail + text
        has_delete = bool(self.DELETE_CHARACTER_RE.search(redraw_probe))
        redraw_target = self._readline_redraw_target(text, previous_cursor)
        if prompt_snapshot is not None:
            # A PTY can split CSI n P across reads. Retain the prompt seen
            # before the incomplete sequence until its final byte arrives.
            self._pending_readline_prompt = prompt_snapshot
        elif not has_delete and "\n" in text:
            # Normal terminal output establishes real rows again, so an old
            # editable prompt must not be reused for a later redraw. Existing
            # tail patches stay at their document rows as the command enters
            # scrollback.
            self._pending_readline_prompt = None
            self._virtual_readline_prompt = None
        self._record_replay(text)
        self.stream.feed(text)
        # Readline/PSReadLine compactly redraw history entries using cursor
        # movement followed by erase/delete controls. The logical screen is
        # correct in pyte, but explicitly invalidate Qt's document after these
        # controls so removed tail glyphs cannot survive a partial repaint.
        # PTY/SSH reads may split a sequence such as CSI 5 P into "ESC[5"
        # and "P". Keep a short tail so destructive redraw controls are
        # recognised even when their bytes arrive in separate callbacks.
        self._requires_full_document_rebuild |= bool(self.REDRAW_ERASE_RE.search(redraw_probe))
        if has_delete:
            self._clear_shortened_readline_tail(previous_cursor)
            self._set_readline_overlay(
                previous_cursor, text,
                prompt_snapshot or self._pending_readline_prompt,
            )
            self._pending_readline_prompt = None
        elif "\n" in text and redraw_target is None:
            # Command output has made the current input part of the terminal
            # transcript, so an old prompt snapshot cannot be reused later.
            self._virtual_readline_prompt = None
        self._redraw_control_tail = redraw_probe[-32:]
        # Readline redraws are latency-sensitive: Ctrl-R, completion cycling,
        # and cursor movement arrive as control sequences and must be visible
        # in the next GUI turn. Ordinary output remains coalesced briefly.
        interactive_redraw = (
            bool(self.REDRAW_ERASE_RE.search(redraw_probe))
            or redraw_target is not None
            or "\x12" in text
        )
        if interactive_redraw:
            self.render_screen()
        elif not self._render_pending:
            self._render_pending = True
            # Coalesce ordinary command output. Rendering every small SSH
            # packet makes a busy terminal monopolise the Qt event loop.
            QTimer.singleShot(16, self.render_screen)

    @staticmethod
    def _readline_motion_count(sequence, direction):
        return sum(int(value or 1) for value in re.findall(rf"\x1b\[(\d*){direction}", sequence))

    def _capture_readline_prompt(self, text, previous_cursor):
        """Save a prompt pyte has placed one row below readline's target."""
        target = self._readline_redraw_target(text, previous_cursor)
        if target is None:
            return None
        target_y, right = target
        source_y = target_y + 1
        columns = int(self.screen.columns)
        if not (0 <= target_y < self.screen.lines and 0 <= source_y < self.screen.lines):
            return None
        width = max(1, min(columns, right))
        default_cell = getattr(self.screen, "default_char", None)
        target = self.screen.buffer[target_y]
        source = self.screen.buffer[source_y]
        target_cells = [target.get(x, default_cell) for x in range(width)]
        source_cells = [source.get(x, default_cell) for x in range(width)]
        if any(cell is not None and (cell.data or "").strip() for cell in target_cells):
            return None
        if not any(cell is not None and (cell.data or "").strip() for cell in source_cells):
            return None
        # A continuation of the command can also begin on the next row. Only
        # move a real, styled shell prompt; ordinary command text must remain
        # where pyte placed it.
        if not any(
            cell is not None and (cell.data or "").strip()
            and not self._cell_uses_default_colours(cell)
            for cell in source_cells
        ):
            return None
        return target_y, source_cells

    def _readline_redraw_target(self, text, previous_cursor):
        """Return readline's intended row and prompt width for a redraw."""
        match = self.READLINE_REPOSITION_RE.match(text)
        if match is not None:
            up = self._readline_motion_count(match.group("up"), "A")
            right = self._readline_motion_count(match.group("right"), "C")
            return int(previous_cursor[1]) - up, right
        match = self.READLINE_SAME_ROW_RE.match(text)
        if match is not None:
            return int(previous_cursor[1]), self._readline_motion_count(match.group("right"), "C")
        return None

    def _set_readline_overlay(self, previous_cursor, text, prompt_snapshot):
        """Keep a styled prompt at readline's intended display row.

        Tail cleanup is applied to pyte itself. Keeping it out of this
        renderer overlay prevents one recalled command from masking text in a
        later recalled command.
        """
        columns, lines = int(self.screen.columns), int(self.screen.lines)
        history_rows = max(0, len(self._history_snapshot()) - lines)
        overlay = {}
        if prompt_snapshot is None and self._virtual_readline_prompt is not None:
            target = self._readline_redraw_target(text, previous_cursor)
            if target is not None:
                prompt_snapshot = (target[0], self._virtual_readline_prompt[1])
        if prompt_snapshot is not None:
            prompt_y, prompt_cells = prompt_snapshot
            if 0 <= prompt_y < lines:
                for x, cell in enumerate(prompt_cells[:columns]):
                    overlay[(history_rows + prompt_y, x)] = cell
                self._virtual_readline_prompt = (prompt_y, tuple(prompt_cells))

        self._readline_overlay = overlay
        self._requires_full_document_rebuild = True

    def _clear_shortened_readline_tail(self, previous_cursor):
        """Clear pyte cells that readline has removed across wrapped rows."""
        old_x, old_y = previous_cursor
        new_x, new_y = int(self.screen.cursor.x), int(self.screen.cursor.y)
        columns, lines = int(self.screen.columns), int(self.screen.lines)
        old_x, new_x = max(0, min(columns, int(old_x))), max(0, min(columns, new_x))
        old_y, new_y = max(0, min(lines - 1, int(old_y))), max(0, min(lines - 1, int(new_y)))
        if (new_y, new_x) >= (old_y, old_x):
            return
        default_cell = getattr(self.screen, "default_char", None)
        if default_cell is None:
            return
        for y in range(new_y, old_y + 1):
            start = new_x if y == new_y else 0
            end = old_x if y == old_y else columns
            line = self.screen.buffer[y]
            for x in range(start, end):
                line[x] = default_cell
        self._requires_full_document_rebuild = True

    @staticmethod
    def _cell_style_key(cell):
        return (
            cell.fg, cell.bg,
            bool(getattr(cell, "reverse", False)),
            bool(getattr(cell, "bold", False)),
            bool(getattr(cell, "italics", False)),
            bool(getattr(cell, "underscore", False)),
            bool(getattr(cell, "strikethrough", False)),
        )

    @staticmethod
    def _format_for_cell(cell):
        fmt = QTextCharFormat()
        fg = colour(cell.fg, "#eeeeec")
        bg = colour(cell.bg, "#101010")
        if getattr(cell, "reverse", False):
            fg, bg = bg, fg
        fmt.setForeground(fg)
        fmt.setBackground(bg)
        fmt.setFontWeight(QFont.Weight.Bold if getattr(cell, "bold", False) else QFont.Weight.Normal)
        fmt.setFontItalic(bool(getattr(cell, "italics", False)))
        fmt.setFontUnderline(bool(getattr(cell, "underscore", False)))
        fmt.setFontStrikeOut(bool(getattr(cell, "strikethrough", False)))
        return fmt

    def _cached_format_for_cell(self, cell):
        key = self._cell_style_key(cell) + (getattr(cell, "fg", "default"), getattr(cell, "bg", "default"))
        fmt = self._format_cache.get(key)
        if fmt is None:
            fmt = self._format_for_cell(cell)
            self._format_cache[key] = fmt
        return fmt

    @staticmethod
    def _cell_uses_default_colours(cell):
        return (
            getattr(cell, "fg", "default") in (None, "default")
            and getattr(cell, "bg", "default") in (None, "default")
            and not bool(getattr(cell, "reverse", False))
        )

    def _insert_terminal_run(self, cursor, text, cell):
        """Insert one ANSI run, optionally adding conservative semantic colour."""
        base = self._cached_format_for_cell(cell)
        if not self.syntax_highlighting or not self._cell_uses_default_colours(cell) or not text.strip():
            cursor.insertText(text, base)
            return

        # Highlight only text that the application did not explicitly colour
        # with ANSI. This preserves ls/git/compiler themes and shell prompts.
        token_re = self.SEMANTIC_TOKEN_RE
        pos = 0
        for match in token_re.finditer(text):
            if match.start() > pos:
                cursor.insertText(text[pos:match.start()], base)
            token = match.group(0)
            fmt = QTextCharFormat(base)
            low = token.lower()
            if low.startswith(("http://", "https://")):
                fmt.setForeground(QColor("#61afef"))
                fmt.setFontUnderline(True)
            elif token.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:\\\\", token):
                fmt.setForeground(QColor("#56b6c2"))
            elif token.startswith("-"):
                fmt.setForeground(QColor("#c678dd"))
            elif low in ("error", "failed", "failure", "fatal", "denied"):
                fmt.setForeground(QColor("#e06c75"))
                fmt.setFontWeight(QFont.Weight.Bold)
            elif low in ("warning", "warn"):
                fmt.setForeground(QColor("#e5c07b"))
            elif low in ("success", "successful", "passed", "ok"):
                fmt.setForeground(QColor("#98c379"))
            else:
                fmt.setForeground(QColor("#d19a66"))
            cursor.insertText(token, fmt)
            pos = match.end()
        if pos < len(text):
            cursor.insertText(text[pos:], base)

    def render_screen(self):
        self._render_pending = False
        live_view = self._is_live_view()
        # setPlainText() is needed when scrollback gains or loses rows, but it
        # resets Qt's current selection. Preserve its document endpoints so a
        # completed multi-line selection remains available for copying after
        # ordinary terminal output adds a new line.
        active_cursor = self.textCursor()
        selection = (active_cursor.anchor(), active_cursor.position()) if active_cursor.hasSelection() else None
        # Render the entire immutable scrollback snapshot into the document.
        # Unlike a rolling one-page canvas, this gives every displayed line a
        # stable document position, so Qt can preserve a multi-line selection
        # while the user scrolls through it.
        history = getattr(self.screen, "history", None)
        history_lines = list(history.top) if history is not None else []
        history_count = len(history_lines)
        history_first = history_lines[0] if history_lines else None
        lines = int(self.screen.lines)
        columns = int(self.screen.columns)
        # When a terminal scrolls, its previous top screen row becomes the
        # next history row and every other document row keeps its position.
        # Update only pyte's dirty live rows instead of rebuilding and styling
        # all 10,000 history rows for each output packet.
        incremental = (
            self._rendered_rows is not None
            and not self._requires_full_document_rebuild
            and not self._readline_overlay
            and history_count >= self._rendered_history_count
            and (self._rendered_history_count == 0 or history_first is self._rendered_history_first)
            and len(self._rendered_rows) == self._rendered_history_count + lines
        )
        visible_lines = None
        if incremental:
            rows = list(self._rendered_rows)
            added_history = history_count - self._rendered_history_count
            if added_history:
                rows.extend([None] * added_history)
                cursor = QTextCursor(self.document())
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText("\n" + "\n".join(" " * columns for _ in range(added_history)))
            dirty_rows = set(getattr(self.screen, "dirty", set()))
            if added_history:
                dirty_rows.update(range(max(0, lines - added_history), lines))
            changed = []
            for live_y in dirty_rows:
                if not 0 <= live_y < lines:
                    continue
                line = self._copy_terminal_line(self.screen.buffer[live_y])
                default_cell = getattr(self.screen, "default_char", None)
                row = tuple((cell.data if cell is not None else " ",
                             self._cell_style_key(cell) if cell is not None else None)
                            for cell in (line.get(x, default_cell) for x in range(columns)))
                index = history_count + live_y
                if rows[index] != row:
                    rows[index] = row
                    changed.append(index)
        else:
            visible_lines = self._apply_readline_overlay(self._history_snapshot())
            rows = None

        self.setUpdatesEnabled(False)
        try:
            default_cell = getattr(self.screen, "default_char", None)
            if not incremental:
                rows = []
                for line in visible_lines:
                    cells = [line.get(x, default_cell) for x in range(columns)]
                    rows.append(tuple((cell.data if cell is not None else " ",
                                       self._cell_style_key(cell) if cell is not None else None)
                                      for cell in cells))

            # Establish a fixed-size document once. Subsequent redraws update
            # only changed blocks, which is dramatically cheaper for readline
            # echo and cursor movement than clearing/rebuilding the canvas.
            # A destructive readline redraw can combine DCH/ECH/EL operations
            # across wrapped rows. Rebuild the Qt document from pyte's final
            # state in that case: it is more reliable than several in-place
            # block replacements, which can leave a stale suffix painted after
            # a shorter history entry replaces a longer one.
            if (not incremental and (self._requires_full_document_rebuild or self._rendered_rows is None
                    or len(self._rendered_rows) != len(rows))):
                plain = "\n".join("".join(cell[0] or " " for cell in row) for row in rows)
                self.setPlainText(plain)
                changed = range(len(rows))
                # Applying a QTextCharFormat run-by-run to thousands of old
                # history rows blocks the Qt event loop. Keep the transcript
                # immediately readable, then reserve rich ANSI/semantic
                # formatting for the live terminal and recent scrollback.
                if len(rows) > 500:
                    changed = range(max(0, len(rows) - max(200, self.screen.lines * 3)), len(rows))
            elif not incremental:
                changed = [i for i, row in enumerate(rows) if row != self._rendered_rows[i]]

            for y in changed:
                line = (visible_lines[y] if visible_lines is not None
                        else self._copy_terminal_line(self.screen.buffer[y - history_count]))
                block = self.document().findBlockByNumber(y)
                if not block.isValid():
                    continue
                cursor = QTextCursor(self.document())
                start = block.position()
                length = max(0, block.length() - 1)
                cursor.setPosition(start)
                cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
                cursor.beginEditBlock()
                cursor.removeSelectedText()
                run_text = []
                run_cell = None
                run_key = None
                for x in range(columns):
                    cell = line.get(x, default_cell)
                    if cell is None:
                        continue
                    key = self._cell_style_key(cell)
                    if run_key is None:
                        run_key, run_cell = key, cell
                    elif key != run_key:
                        self._insert_terminal_run(cursor, "".join(run_text), run_cell)
                        run_text = []
                        run_key, run_cell = key, cell
                    run_text.append(cell.data or " ")
                if run_text:
                    self._insert_terminal_run(cursor, "".join(run_text), run_cell)
                cursor.endEditBlock()

            self._rendered_rows = rows
            self._rendered_history_count = history_count
            self._rendered_history_first = history_first
            try:
                self.screen.dirty.clear()
            except Exception:
                pass
        finally:
            self.setUpdatesEnabled(True)

        if selection:
            anchor, position = selection
            document_end = max(0, self.document().characterCount() - 1)
            restored = QTextCursor(self.document())
            restored.setPosition(min(anchor, document_end))
            restored.setPosition(min(position, document_end), QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(restored)

        if self._requires_full_document_rebuild:
            self._requires_full_document_rebuild = False

        # Keep live terminal output at the bottom. When browsing history leave
        # Qt's document position alone, preserving mouse selections and the
        # visible text instead of pinning a replacement page to the top.
        if live_view:
            scrollbar = self.verticalScrollBar()
            self._setting_scroll_position = True
            try:
                scrollbar.setValue(scrollbar.maximum())
                self._history_scrolled = False
                self._history_offset = 0
            finally:
                self._setting_scroll_position = False
            self.horizontalScrollBar().setValue(0)

        self._cursor_on = self._session_active
        self.viewport().update()

    def _is_live_view(self):
        return not self._history_scrolled

    def _live_cursor_document_position(self):
        """Map pyte's live cursor to the fixed-width Qt terminal document."""
        columns = max(1, int(getattr(self.screen, "columns", 1)))
        rows = max(1, int(getattr(self.screen, "lines", 1)))
        x = max(0, min(columns - 1, int(getattr(self.screen.cursor, "x", 0))))
        y = max(0, min(rows - 1, int(getattr(self.screen.cursor, "y", 0))))
        # History rows precede the live screen in the document.
        history_rows = max(0, len(self._history_snapshot()) - rows)
        return (history_rows + y) * (columns + 1) + x

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.backend or not self._session_active or self._history_scrolled or (self.cursor_blink and not self._cursor_on):
            return
        try:
            pos = self._live_cursor_document_position()
            tc = QTextCursor(self.document())
            tc.setPosition(max(0, min(pos, self.document().characterCount() - 1)))
            rect = self.cursorRect(tc)
            painter = QPainter(self.viewport())
            pen = QPen(QColor("#eeeeec")); pen.setWidth(2); painter.setPen(pen)
            width = max(5, self.fontMetrics().horizontalAdvance("M"))
            if self.cursor_style == "block":
                painter.fillRect(rect.left(), rect.top() + 1, width, max(2, rect.height() - 2), QColor(238, 238, 236, 120))
            elif self.cursor_style == "underline":
                painter.drawLine(rect.left(), rect.bottom() - 1, rect.left() + width, rect.bottom() - 1)
            else:
                x = rect.left()
                painter.drawLine(x, rect.top() + 1, x, rect.bottom() - 1)
            painter.end()
        except Exception:
            pass

    def paste_to_terminal(self):
        if self.backend:
            text = QApplication.clipboard().text()
            if text:
                # Bracketed paste tells Bash/readline to insert multiline text
                # into the editable buffer without treating embedded newlines
                # as separate Enter presses. A later user Enter executes it.
                self.backend.write("\x1b[200~" + text + "\x1b[201~")

    def find_terminal(self, backwards=False):
        if self._find_dialog is None:
            self._find_dialog = TerminalFindDialog(self, self)
            def _find_closed(_result):
                self.set_line_numbers_visible(False)
                self._find_dialog = None
            self._find_dialog.finished.connect(_find_closed)
        self.set_line_numbers_visible(True)
        self._find_dialog.show()
        self._find_dialog.raise_()
        self._find_dialog.activateWindow()
        self._find_dialog.query.setFocus()
        if backwards and self._find_dialog.matches:
            self._find_dialog.cycle(-1)

    def find_next(self, backwards=False):
        if self._find_dialog and self._find_dialog.matches:
            self._find_dialog.cycle(-1 if backwards else 1)
        else:
            self.find_terminal(backwards)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        copy_action = menu.addAction("Copy")
        copy_action.setEnabled(self.textCursor().hasSelection())
        copy_action.triggered.connect(self.copy)
        paste_action = menu.addAction("Paste")
        paste_action.setEnabled(bool(self.backend and QApplication.clipboard().text()))
        paste_action.triggered.connect(self.paste_to_terminal)
        menu.addSeparator()
        find_action = menu.addAction("Find…")
        find_action.triggered.connect(self.find_terminal)
        menu.exec(event.globalPos())

    def focusNextPrevChild(self, next):
        return False

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.focus_activated.emit()

    def keyPressEvent(self, event):
        if not self._session_active:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.close_requested.emit()
            elif event.key() == Qt.Key.Key_R and not event.modifiers():
                self.restart_requested.emit()
            elif event.key() == Qt.Key.Key_S and not event.modifiers():
                self.save_output_requested.emit()
            return
        if not self.backend:
            return
        if self._history_scrolled:
            self._return_to_live()
        key = event.key(); mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_F:
            self.find_terminal(); return
        if key == Qt.Key.Key_F3:
            self.find_next(bool(mods & Qt.KeyboardModifier.ShiftModifier)); return
        if mods & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_C:
            if self.textCursor().hasSelection(): self.copy()
            else: self.backend.write("\x03")
            return
        if mods & Qt.KeyboardModifier.ControlModifier and mods & Qt.KeyboardModifier.ShiftModifier and key == Qt.Key.Key_V:
            self.paste_to_terminal(); return
        special = {
            Qt.Key.Key_Return: "\r", Qt.Key.Key_Enter: "\r", Qt.Key.Key_Backspace: "\x7f",
            Qt.Key.Key_Tab: "\t", Qt.Key.Key_Backtab: "\t",
            Qt.Key.Key_Escape: "\x1b", Qt.Key.Key_Up: "\x1b[A", Qt.Key.Key_Down: "\x1b[B", Qt.Key.Key_Right: "\x1b[C",
            Qt.Key.Key_Left: "\x1b[D", Qt.Key.Key_Home: "\x1b[H", Qt.Key.Key_End: "\x1b[F", Qt.Key.Key_Delete: "\x1b[3~",
            Qt.Key.Key_PageUp: "\x1b[5~", Qt.Key.Key_PageDown: "\x1b[6~",
        }
        if key in special:
            self.backend.write(special[key]); return
        text = event.text()
        if text:
            if mods & Qt.KeyboardModifier.ControlModifier and len(text) == 1 and text.isalpha(): self.backend.write(chr(ord(text.lower()) - 96))
            else: self.backend.write(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        if self.line_numbers_visible:
            self.line_number_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))
        self._send_resize()

    def _send_resize(self):
        # Queue one resize after Qt has finished laying out the widget.  This
        # avoids a PTY -> redraw -> scrollbar/viewport -> PTY resize loop.
        self._resize_timer.start()

    def _apply_terminal_resize(self):
        metrics = self.fontMetrics()
        char_w = max(1, metrics.horizontalAdvance("M"))
        char_h = max(1, metrics.height())

        cols = max(20, (self.viewport().width() - 4) // char_w)
        rows = max(5, (self.viewport().height() - 4) // char_h)
        size = (cols, rows)

        if size == self._last_terminal_size:
            self._resize_backend(cols, rows)
            return
        self._last_terminal_size = size

        # Always return to the live page before geometry changes.
        if self._history_scrolled:
            self._return_to_live(flush_now=True)

        old_size = (self.screen.columns, self.screen.lines)
        if size != old_size:
            # A terminal resize changes the live screen; it does not replay an
            # entire session. Replaying the raw journal on every maximize or
            # restore reinterpreted old wrapping at the new width, produced
            # redraw artefacts, and could block the GUI for seconds.
            self._resize_backend(cols, rows)
            self.screen.resize(lines=rows, columns=cols)
            self._rendered_rows = None
            self._clear_readline_overlay()
            self.render_screen()
        else:
            self._resize_backend(cols, rows)

    def _resize_backend(self, cols, rows):
        """Send each backend the terminal geometry once after attachment."""
        size = (cols, rows)
        if self.backend and size != self._last_backend_size:
            self.backend.resize(cols, rows)
            self._last_backend_size = size
            self._backend_geometry_needs_sync = False

    def _history_at_bottom(self):
        return not self._history_scrolled

    def _return_to_live(self, flush_now=False):
        self._history_offset = 0
        self._history_scrolled = False

        if self._deferred_output:
            pending = "".join(self._deferred_output)
            self._deferred_output.clear()
            self._deferred_output_chars = 0
            self._input_buffer.append(pending)
            if flush_now:
                self._process_input_batch()
            elif not self._input_pending:
                self._input_pending = True
                QTimer.singleShot(0, self._process_input_batch)
        else:
            self.render_screen()
        scrollbar = self.verticalScrollBar()
        self._setting_scroll_position = True
        try:
            scrollbar.setValue(scrollbar.maximum())
        finally:
            self._setting_scroll_position = False

    def wheelEvent(self, event):
        # Native QPlainTextEdit scrolling keeps selections anchored to document
        # text. The scrollbar callback records whether this is history view.
        super().wheelEvent(event)

    def close_backend(self):
        if self.backend:
            self._session_active = False
            self._cursor_timer.stop()
            self.backend.close(); self.backend = None
            self._last_backend_size = None
            self._backend_geometry_needs_sync = False


# ----------------------------- Host profiles ---------------------------------

class HostStore:
    DEFAULT_SETTINGS = {"terminal": {"cursor_style": "ibeam", "cursor_blink": True, "follow_file_browser_in_terminal": False, "follow_terminal_in_file_browser": False, "follow_terminal_directory": False, "scrollback_limit": 10000, "syntax_highlighting": True, "windows_powershell_predictions": True, "auto_connect_saved_password": False, "font_family": "Monospace", "font_size": 11, "info_font_family": "Monospace", "info_font_size": 10}}

    def __init__(self):
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
        self.path = Path(base) / "config.json"
        self.legacy_path = Path(base) / "hosts.json"

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load_all()

    def _load_all(self):
        data = {"version": 1, "hosts": [], "terminal": dict(self.DEFAULT_SETTINGS["terminal"]), "file_browser": {"show_hidden": False}, "command_groups": []}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict): data.update(loaded)
        except FileNotFoundError:
            try:
                hosts = json.loads(self.legacy_path.read_text(encoding="utf-8"))
                if isinstance(hosts, list): data["hosts"] = hosts
            except Exception: pass
        except Exception: pass
        data.setdefault("hosts", []); data.setdefault("terminal", {}); data.setdefault("file_browser", {"show_hidden": False}); data.setdefault("command_groups", [])
        for key, value in self.DEFAULT_SETTINGS["terminal"].items(): data["terminal"].setdefault(key, value)
        changed = False
        for profile in data["hosts"]:
            if "id" not in profile:
                profile["id"] = str(uuid.uuid4()); changed = True
        self.data = data
        if changed or not self.path.exists(): self._write()
        return data

    def _write(self):
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def load(self): return self.data.get("hosts", [])
    def save(self, profiles): self.data["hosts"] = profiles; self._write()
    def terminal_settings(self): return dict(self.data.get("terminal", {}))
    def save_terminal_settings(self, settings): self.data["terminal"] = dict(settings); self._write()
    def file_browser_settings(self): return dict(self.data.get("file_browser", {"show_hidden": False}))
    def save_file_browser_settings(self, settings): self.data["file_browser"] = dict(settings); self._write()
    def command_groups(self): return list(self.data.get("command_groups", []))
    def save_command_groups(self, groups): self.data["command_groups"] = groups; self._write()


class CredentialStore:
    SERVICE = "PowerTerm SSH"
    def get(self, profile):
        try: return keyring.get_password(self.SERVICE, profile["id"]) or ""
        except Exception: return ""
    def set(self, profile, password):
        if password: keyring.set_password(self.SERVICE, profile["id"], password)
    def delete(self, profile):
        try: keyring.delete_password(self.SERVICE, profile["id"])
        except Exception: pass


class HostProfileDialog(QDialog):
    def __init__(self, profile=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Host Profile")
        profile = profile or {}
        self.profile_id = profile.get("id") or str(uuid.uuid4())
        self.name = QLineEdit(profile.get("name", ""))
        self.host = QLineEdit(profile.get("host", ""))
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(int(profile.get("port", 22)))
        self.username = QLineEdit(profile.get("username", ""))
        self.start_dir = QLineEdit(profile.get("start_dir", ""))
        self.start_dir.setPlaceholderText("Optional, e.g. /var/www")

        form = QFormLayout()
        form.addRow("Name:", self.name)
        form.addRow("Host:", self.host)
        form.addRow("Port:", self.port)
        form.addRow("Username:", self.username)
        form.addRow("Start folder:", self.start_dir)

        note = QLabel("Host settings are stored in config.json. Remembered passwords are stored separately in the operating system credential store.")
        note.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def profile(self):
        host = self.host.text().strip()
        return {
            "id": self.profile_id,
            "name": self.name.text().strip() or host,
            "host": host,
            "port": self.port.value(),
            "username": self.username.text().strip(),
            "start_dir": self.start_dir.text().strip(),
        }


class PasswordDialog(QDialog):
    def __init__(self, profile, stored_password="", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Connect to {profile['name']}")
        self.password = QLineEdit(stored_password)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Leave blank for SSH key / agent")
        self.remember = QCheckBox("Remember password securely")
        self.remember.setChecked(bool(stored_password))
        form = QFormLayout()
        form.addRow("Host:", QLabel(profile["host"]))
        form.addRow("Username:", QLabel(profile["username"]))
        form.addRow("Password:", self.password)
        form.addRow("", self.remember)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self); layout.addLayout(form); layout.addWidget(buttons)


class QuickSshDialog(HostProfileDialog):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setWindowTitle("Quick SSH")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Leave blank for SSH key / agent")
        form = self.layout().itemAt(0).layout()
        form.addRow("Password:", self.password)


class TerminalSettingsDialog(QDialog):
    def __init__(self, settings, config_path=None, parent=None):
        super().__init__(parent)
        self.config_path = Path(config_path) if config_path else None
        self.setWindowTitle("Appearance & Terminal Settings")
        self.cursor_style = QComboBox()
        self.cursor_style.addItem("I-beam", "ibeam")
        self.cursor_style.addItem("Block", "block")
        self.cursor_style.addItem("Underline", "underline")
        self.cursor_style.setCurrentIndex(max(0, self.cursor_style.findData(settings.get("cursor_style", "ibeam"))))
        self.cursor_blink = QCheckBox("Blink cursor")
        self.cursor_blink.setChecked(bool(settings.get("cursor_blink", True)))
        legacy_follow = bool(settings.get("follow_terminal_directory", False))
        self._follow_file_browser_in_terminal = bool(settings.get("follow_file_browser_in_terminal", legacy_follow))
        self._follow_terminal_in_file_browser = bool(settings.get("follow_terminal_in_file_browser", False))
        self.scrollback_limit = QSpinBox()
        self.scrollback_limit.setRange(100, 1000000)
        self.scrollback_limit.setSingleStep(1000)
        self.scrollback_limit.setValue(int(settings.get("scrollback_limit", 10000)))
        self.scrollback_limit.setSuffix(" lines")
        self.scrollback_limit.setToolTip("Maximum terminal history retained per session.")
        self.syntax_highlighting = QCheckBox("Semantic syntax highlighting in terminal output")
        self.syntax_highlighting.setChecked(bool(settings.get("syntax_highlighting", True)))
        self.syntax_highlighting.setToolTip("Adds lightweight colours only to otherwise uncoloured terminal text; ANSI colours from programs take priority.")
        self.windows_predictions = QCheckBox("PowerShell predictive suggestions (Windows only)")
        self.windows_predictions.setChecked(bool(settings.get("windows_powershell_predictions", True)))
        self.windows_predictions.setToolTip("When enabled, PowerShell/PSReadLine inline predictions are shown in subdued grey. Has no effect on Linux, macOS, SSH, or cmd.exe.")
        self.auto_connect_saved_password = QCheckBox("Connect immediately when a saved SSH password is available")
        self.auto_connect_saved_password.setChecked(bool(settings.get("auto_connect_saved_password", False)))
        self.auto_connect_saved_password.setToolTip("Skip the SSH password dialog for saved hosts only when a password is already stored securely in the operating system credential store.")

        self.terminal_font = QFont(settings.get("font_family", "Monospace"), int(settings.get("font_size", 11)))
        self.terminal_font.setStyleHint(QFont.StyleHint.Monospace)
        self.info_font = QFont(settings.get("info_font_family", "Monospace"), int(settings.get("info_font_size", 10)))
        self.info_font.setStyleHint(QFont.StyleHint.Monospace)
        self.terminal_font_button = QPushButton()
        self.info_font_button = QPushButton()
        self._update_font_buttons()
        self.terminal_font_button.clicked.connect(self._choose_terminal_font)
        self.info_font_button.clicked.connect(self._choose_info_font)

        form = QFormLayout()
        form.addRow("Cursor:", self.cursor_style)
        form.addRow("", self.cursor_blink)
        form.addRow("Terminal font:", self.terminal_font_button)
        form.addRow("System-info font:", self.info_font_button)
        form.addRow("Scrollback limit:", self.scrollback_limit)
        form.addRow("", self.syntax_highlighting)
        form.addRow("", self.windows_predictions)
        form.addRow("", self.auto_connect_saved_password)

        self.config_path_edit = QLineEdit(str(self.config_path) if self.config_path else "")
        self.config_path_edit.setReadOnly(True)
        self.config_path_edit.setToolTip("PowerTerm's per-user JSON configuration file")
        self.open_config_button = QPushButton("Open Config Folder")
        self.open_config_button.clicked.connect(self._open_config_folder)
        config_row = QHBoxLayout()
        config_row.setContentsMargins(0, 0, 0, 0)
        config_row.addWidget(self.config_path_edit, 1)
        config_row.addWidget(self.open_config_button)
        form.addRow("Configuration:", config_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _open_config_folder(self):
        if not self.config_path:
            return
        folder = self.config_path.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            QMessageBox.warning(self, "Open Config Folder", str(exc))

    def _font_text(self, font):
        return f"{font.family()} — {font.pointSize()} pt"

    def _update_font_buttons(self):
        self.terminal_font_button.setText(self._font_text(self.terminal_font))
        self.terminal_font_button.setFont(self.terminal_font)
        self.info_font_button.setText(self._font_text(self.info_font))
        self.info_font_button.setFont(self.info_font)

    def _choose_font(self, current_font, title):
        # Avoid the static getFont() tuple entirely. PySide6 bindings/platforms
        # have differed in how that tuple is exposed; an instance dialog gives
        # us an unambiguous selectedFont().
        dialog = QFontDialog(current_font, self)
        dialog.setWindowTitle(title)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.selectedFont()
        return current_font

    def _choose_terminal_font(self):
        self.terminal_font = self._choose_font(self.terminal_font, "Choose terminal font")
        self._update_font_buttons()

    def _choose_info_font(self):
        self.info_font = self._choose_font(self.info_font, "Choose system-information font")
        self._update_font_buttons()

    def settings(self):
        return {
            "cursor_style": self.cursor_style.currentData(),
            "cursor_blink": self.cursor_blink.isChecked(),
            "follow_file_browser_in_terminal": self._follow_file_browser_in_terminal,
            "follow_terminal_in_file_browser": self._follow_terminal_in_file_browser,
            "scrollback_limit": self.scrollback_limit.value(),
            "syntax_highlighting": self.syntax_highlighting.isChecked(),
            "windows_powershell_predictions": self.windows_predictions.isChecked(),
            "auto_connect_saved_password": self.auto_connect_saved_password.isChecked(),
            "font_family": self.terminal_font.family(),
            "font_size": self.terminal_font.pointSize(),
            "info_font_family": self.info_font.family(),
            "info_font_size": self.info_font.pointSize(),
        }


# ----------------------------- Permissions dialog ----------------------------

class PermissionsDialog(QDialog):
    FLAGS = [
        ("Owner read", stat.S_IRUSR), ("Owner write", stat.S_IWUSR), ("Owner execute", stat.S_IXUSR),
        ("Group read", stat.S_IRGRP), ("Group write", stat.S_IWGRP), ("Group execute", stat.S_IXGRP),
        ("Other read", stat.S_IROTH), ("Other write", stat.S_IWOTH), ("Other execute", stat.S_IXOTH),
    ]

    def __init__(self, path, mode, size=None, metadata=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Properties")
        self.resize(620, 500)
        self.path = path
        self.checks = []
        self._source_mode = mode
        self.original_permissions = stat.S_IMODE(mode)

        metadata = metadata or {}
        name = posixpath.basename(path.rstrip("/")) if "/" in path else os.path.basename(path.rstrip(os.sep))
        name = name or path
        title = QLabel(f"<b>{html.escape(name)}</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        general = []
        item_type = metadata.get("Type") or ("Folder" if stat.S_ISDIR(mode) else "File")
        location = metadata.get("Location") or os.path.dirname(path.rstrip("/\\")) or posixpath.dirname(path.rstrip("/")) or "/"
        general.append(f"Type: {item_type}")
        general.append(f"Location: {location}")
        if size is not None:
            general.append(f"Size: {human_size(size)} ({int(size):,} bytes)")
        for label in ("Owner", "Group", "Link target", "Inode", "Hard links"):
            value = metadata.get(label)
            if value not in (None, ""):
                general.append(f"{label}: {value}")
        self.general_details = QLabel("\n".join(general))
        self.general_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        general_group = QGroupBox("General")
        general_layout = QVBoxLayout(general_group)
        general_layout.addWidget(self.general_details)

        dates = []
        for label in ("Created", "Last modified", "Last accessed"):
            value = metadata.get(label)
            dates.append(f"{label}: {value or 'Unavailable'}")
        self.date_details = QLabel("\n".join(dates))
        self.date_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        dates_group = QGroupBox("Dates")
        dates_layout = QVBoxLayout(dates_group)
        dates_layout.addWidget(self.date_details)

        grid = QGridLayout()
        headers = ["", "Read", "Write", "Execute"]
        for col, text in enumerate(headers):
            grid.addWidget(QLabel(text), 0, col)

        labels = ["Owner", "Group", "Other"]
        for row, label in enumerate(labels, start=1):
            grid.addWidget(QLabel(label), row, 0)
            for col in range(1, 4):
                flag = self.FLAGS[(row - 1) * 3 + (col - 1)][1]
                cb = QCheckBox()
                cb.setChecked(bool(mode & flag))
                cb.setProperty("flag", flag)
                grid.addWidget(cb, row, col)
                self.checks.append(cb)

        special_row = QHBoxLayout()
        self.special_checks = []
        for text, flag in (("Set user ID", stat.S_ISUID), ("Set group ID", stat.S_ISGID), ("Sticky", stat.S_ISVTX)):
            cb = QCheckBox(text)
            cb.setChecked(bool(mode & flag))
            cb.setProperty("flag", flag)
            special_row.addWidget(cb)
            self.special_checks.append(cb)
        special_row.addStretch(1)

        self.octal = QLabel()
        self.octal.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for cb in self.checks + self.special_checks:
            cb.toggled.connect(self._update_octal)
        self._update_octal()

        permissions_group = QGroupBox("Permissions")
        permissions_layout = QVBoxLayout(permissions_group)
        permissions_layout.addLayout(grid)
        permissions_layout.addLayout(special_row)
        permissions_layout.addWidget(self.octal)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply permissions")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        copy_path = QPushButton("Copy path")
        copy_path.clicked.connect(lambda: QApplication.clipboard().setText(self.path))
        button_row = QHBoxLayout()
        button_row.addWidget(copy_path)
        button_row.addStretch(1)
        button_row.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(general_group)
        layout.addWidget(dates_group)
        layout.addWidget(permissions_group)
        layout.addStretch(1)
        layout.addLayout(button_row)

    def permission_mode(self):
        value = 0
        for cb in self.checks + self.special_checks:
            if cb.isChecked():
                value |= int(cb.property("flag"))
        return value

    def _update_octal(self):
        mode = self.permission_mode()
        file_type = stat.S_IFMT(self._source_mode) or (stat.S_IFDIR if stat.S_ISDIR(self._source_mode) else stat.S_IFREG)
        display_mode = file_type | mode
        self.octal.setText(f"Symbolic: {stat.filemode(display_mode)}    Octal: {mode:04o}")


# ----------------------------- Remote file browser ----------------------------

class RemoteFileLoader(QObject):
    loaded = Signal(str, object)
    failed = Signal(str, str)
    root_ready = Signal(str)

    def __init__(self, backend, start_dir="", parent=None):
        super().__init__(parent)
        self.backend = backend
        self.start_dir = start_dir
        self._directory_cache = {}
        # A preload must still be useful by the time the user reaches it.
        # Explicit refreshes and mutations use force=True, so a longer TTL
        # improves navigation without hiding user-requested changes.
        self.directory_cache_ttl = 120.0
        self._load_queue = queue.PriorityQueue()
        self._load_sequence = 0
        self._generation = 0
        self._stopped = False
        self._foreground_sftp = None
        self._background_sftp = None
        self._foreground_sftp_lock = threading.Lock()
        self._background_sftp_lock = threading.Lock()
        threading.Thread(target=self._load_worker, daemon=True).start()

    def stop(self):
        self._stopped = True
        self._generation += 1
        self._load_sequence += 1
        self._load_queue.put((-1, self._load_sequence, self._generation, None))
        for attr in ("_foreground_sftp", "_background_sftp"):
            client = getattr(self, attr)
            setattr(self, attr, None)
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def prioritize(self):
        """Invalidate passive work queued for a directory the user left."""
        self._generation += 1
        return self._generation

    def discover_root(self):
        threading.Thread(target=self._discover_root, daemon=True).start()

    def _discover_root(self):
        try:
            root = self.start_dir
            if not root:
                # Discover the home/current directory and prime its visible
                # listing in one process and one SSH channel.
                script = """import json, os
p = os.getcwd()
rows = []
for e in os.scandir(p):
    if e.name in ('.', '..'):
        continue
    is_dir = e.is_dir(follow_symlinks=False)
    rows.append([e.name, is_dir, 0 if is_dir else e.stat(follow_symlinks=False).st_size])
print(json.dumps({'path': p, 'entries': rows}, separators=(',', ':')))
"""
                command = f"python3 -c {shlex.quote(script)}"
                try:
                    status_code, output, _ = self.backend.run_command(command, timeout=10)
                    if status_code == 0 and output.strip():
                        payload = json.loads(output)
                        root = str(payload.get("path") or "")
                        entries = [
                            (str(e[0]), bool(e[1]), int(e[2]))
                            for e in payload.get("entries", [])
                        ]
                        entries.sort(key=lambda e: (not e[1], e[0].lower()))
                        if root:
                            self._directory_cache[root] = (time.monotonic(), entries)
                except Exception:
                    root = ""
            if not root:
                sftp = self.backend.client.open_sftp()
                try:
                    root = sftp.normalize(".")
                finally:
                    sftp.close()
            if not self._stopped:
                self.root_ready.emit(root)
        except Exception as exc:
            if not self._stopped:
                self.failed.emit(self.start_dir or ".", str(exc))

    def load(self, path, force=False, generation=None):
        if self._stopped:
            return
        generation = self._generation if generation is None else generation
        if generation != self._generation:
            return
        cached = self._directory_cache.get(path)
        if not force and cached and time.monotonic() - cached[0] < self.directory_cache_ttl:
            QTimer.singleShot(0, lambda p=path, e=cached[1], g=generation: self._emit_cached(p, e, g))
            return
        if force:
            # Never make an explicit user action wait behind background work.
            # It gets its own direct listing request while the passive worker
            # continues independently.
            threading.Thread(target=self._load_direct, args=(path, generation), daemon=True).start()
            return
        self._load_sequence += 1
        # User-triggered requests always outrank passive preloading.
        self._load_queue.put((1, self._load_sequence, generation, (path,)))

    def has_fresh_cache(self, path):
        cached = self._directory_cache.get(path)
        return bool(cached and time.monotonic() - cached[0] < self.directory_cache_ttl)

    def invalidate(self, path):
        """Discard a listing changed by a file operation."""
        self._directory_cache.pop(posixpath.normpath(path or "/"), None)

    def _emit_cached(self, path, entries, generation):
        if not self._stopped and generation == self._generation:
            self.loaded.emit(path, entries)

    def preload(self, paths, generation=None, urgent=False):
        """Warm one visible layer with one remote process/channel."""
        if self._stopped:
            return
        generation = self._generation if generation is None else generation
        if generation != self._generation:
            return
        missing = []
        now = time.monotonic()
        for path in dict.fromkeys(paths):
            cached = self._directory_cache.get(path)
            if cached and now - cached[0] < self.directory_cache_ttl:
                QTimer.singleShot(0, lambda p=path, e=cached[1], g=generation: self._emit_cached(p, e, g))
            else:
                missing.append(path)
        if not missing:
            return
        if urgent:
            self._start_urgent_preload(missing, generation)
            return
        self._load_sequence += 1
        self._load_queue.put((1, self._load_sequence, generation, tuple(missing)))

    def _start_urgent_preload(self, paths, generation):
        # A user entering a directory must not wait for an unrelated batch
        # that is already blocked on the passive worker's network request.
        threading.Thread(
            target=self._load_many,
            args=(tuple(paths), generation),
            daemon=True,
        ).start()

    def _load_direct(self, path, generation=None):
        try:
            entries = self._list_entries_persistent(path, foreground=True)
            if self._stopped or (generation is not None and generation != self._generation):
                return
            self._directory_cache[path] = (time.monotonic(), entries)
            self.loaded.emit(path, entries)
        except Exception as exc:
            if not self._stopped and (generation is None or generation == self._generation):
                self.failed.emit(path, str(exc))

    def _list_entries(self, path):
        """Prefer one fast shell listing, with SFTP as compatibility fallback."""
        script = """import json, os, sys
p = sys.argv[1]
out = []
for e in os.scandir(p):
    if e.name in ('.', '..'):
        continue
    is_dir = e.is_dir(follow_symlinks=False)
    out.append([e.name, is_dir, 0 if is_dir else e.stat(follow_symlinks=False).st_size])
print(json.dumps(out, separators=(',', ':')))
"""
        command = f"python3 -c {shlex.quote(script)} {shlex.quote(path)}"
        try:
            status_code, output, error = self.backend.run_command(command, timeout=15)
            if status_code != 0:
                raise RuntimeError(error.strip() or f"listing command exited {status_code}")
            raw = json.loads(output)
            entries = [(str(e[0]), bool(e[1]), int(e[2])) for e in raw]
        except Exception:
            sftp = self.backend.client.open_sftp()
            try:
                entries = self._list_entries_sftp(sftp, path)
            finally:
                sftp.close()
        entries.sort(key=lambda e: (not e[1], e[0].lower()))
        return entries

    @staticmethod
    def _list_entries_sftp(sftp, path):
        entries = []
        for attrs in sftp.listdir_attr(path):
            if attrs.filename in (".", ".."):
                continue
            is_dir = stat.S_ISDIR(attrs.st_mode)
            entries.append((attrs.filename, is_dir, 0 if is_dir else int(attrs.st_size or 0)))
        return entries

    def _list_entries_persistent(self, path, foreground=False):
        """Read a directory over one reusable SFTP channel.

        Foreground and passive work have separate channels so a user request
        never queues behind a long background read. Each channel is protected
        because Paramiko SFTP clients are not thread-safe.
        """
        attr = "_foreground_sftp" if foreground else "_background_sftp"
        lock = self._foreground_sftp_lock if foreground else self._background_sftp_lock
        with lock:
            sftp = getattr(self, attr)
            if sftp is None:
                sftp = self.backend.client.open_sftp()
                setattr(self, attr, sftp)
            try:
                entries = self._list_entries_sftp(sftp, path)
            except Exception:
                try:
                    sftp.close()
                except Exception:
                    pass
                setattr(self, attr, None)
                raise
        entries.sort(key=lambda e: (not e[1], e[0].lower()))
        return entries

    def _list_entries_many(self, paths):
        """List several directories in one SSH exec for preload efficiency."""
        paths = list(dict.fromkeys(paths))
        if not paths:
            return {}
        script = """import json, os, sys
paths = json.loads(sys.argv[1])
result = {}
for p in paths:
    try:
        rows = []
        for e in os.scandir(p):
            if e.name in ('.', '..'):
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            rows.append([e.name, is_dir, 0 if is_dir else e.stat(follow_symlinks=False).st_size])
        result[p] = {'entries': rows}
    except Exception as exc:
        result[p] = {'error': str(exc)}
print(json.dumps(result, separators=(',', ':')))
"""
        command = f"python3 -c {shlex.quote(script)} {shlex.quote(json.dumps(paths, separators=(',', ':')))}"
        status_code, output, error = self.backend.run_command(command, timeout=20)
        if status_code != 0:
            raise RuntimeError(error.strip() or f"batch listing command exited {status_code}")
        raw = json.loads(output)
        result = {}
        for path in paths:
            value = raw.get(path, {})
            if "error" in value:
                result[path] = RuntimeError(value["error"])
                continue
            entries = [
                (str(e[0]), bool(e[1]), int(e[2]))
                for e in value.get("entries", [])
            ]
            entries.sort(key=lambda e: (not e[1], e[0].lower()))
            result[path] = entries
        return result

    def _stream_entries_many(self, paths, generation):
        """Publish each batched preload result without waiting for siblings."""
        if not hasattr(self.backend, "run_command_lines"):
            return False
        script = """import json, os, sys
paths = json.loads(sys.argv[1])
for p in paths:
    try:
        rows = []
        for e in os.scandir(p):
            if e.name in ('.', '..'):
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            rows.append([e.name, is_dir, 0 if is_dir else e.stat(follow_symlinks=False).st_size])
        record = {'path': p, 'entries': rows}
    except Exception as exc:
        record = {'path': p, 'error': str(exc)}
    print(json.dumps(record, separators=(',', ':')), flush=True)
"""
        command = f"python3 -c {shlex.quote(script)} {shlex.quote(json.dumps(list(paths), separators=(',', ':')))}"

        def receive(line):
            if self._stopped or generation != self._generation or not line.strip():
                return
            record = json.loads(line)
            path = str(record.get("path") or "")
            if not path:
                return
            if "error" in record:
                self.failed.emit(path, str(record["error"]))
                return
            entries = [(str(e[0]), bool(e[1]), int(e[2])) for e in record.get("entries", [])]
            entries.sort(key=lambda e: (not e[1], e[0].lower()))
            self._directory_cache[path] = (time.monotonic(), entries)
            self.loaded.emit(path, entries)

        status_code, error = self.backend.run_command_lines(command, receive, timeout=20)
        if status_code != 0:
            raise RuntimeError(error.strip() or f"streaming batch listing exited {status_code}")
        return True

    def _load_worker(self):
        while True:
            _, _, generation, paths = self._load_queue.get()
            try:
                if paths is None:
                    return
                if generation == self._generation:
                    if len(paths) == 1:
                        self._load(paths[0], generation)
                    else:
                        self._load_many(paths, generation)
            finally:
                self._load_queue.task_done()

    def _load_many(self, paths, generation=None):
        if generation is not None and generation != self._generation:
            return
        try:
            if generation is not None and self._stream_entries_many(paths, generation):
                return
            results = self._list_entries_many(paths)
        except Exception:
            # Servers without Python retain full compatibility. Keep fallback
            # serial and reuse one SFTP channel to avoid recreating the
            # previous channel burst or repeating SFTP negotiation.
            results = {}
            sftp = None
            try:
                sftp = self.backend.client.open_sftp()
                for path in paths:
                    try:
                        entries = self._list_entries_sftp(sftp, path)
                        entries.sort(key=lambda e: (not e[1], e[0].lower()))
                        results[path] = entries
                    except Exception as exc:
                        results[path] = exc
            except Exception as exc:
                results = {path: exc for path in paths}
            finally:
                if sftp is not None:
                    try:
                        sftp.close()
                    except Exception:
                        pass
        for path in paths:
            if self._stopped or (generation is not None and generation != self._generation):
                return
            result = results.get(path, RuntimeError("Directory was not returned"))
            if isinstance(result, Exception):
                self.failed.emit(path, str(result))
            else:
                self._directory_cache[path] = (time.monotonic(), result)
                self.loaded.emit(path, result)

    def _load(self, path, generation=None):
        try:
            entries = self._list_entries_persistent(path, foreground=False)
            if self._stopped or (generation is not None and generation != self._generation):
                return
            self._directory_cache[path] = (time.monotonic(), entries)
            self.loaded.emit(path, entries)
        except Exception as exc:
            if not self._stopped and (generation is None or generation == self._generation):
                self.failed.emit(path, str(exc))


class RemoteFileTree(QTreeWidget):
    path_changed = Signal(str)
    directory_activated = Signal(str)

    PATH_ROLE = Qt.ItemDataRole.UserRole
    LOADED_ROLE = Qt.ItemDataRole.UserRole + 1
    IS_DIR_ROLE = Qt.ItemDataRole.UserRole + 2
    SIZE_ROLE = Qt.ItemDataRole.UserRole + 3
    PRELOAD_ROLE = Qt.ItemDataRole.UserRole + 4
    PRELOAD_DEPTH_ROLE = Qt.ItemDataRole.UserRole + 5
    DIRECT_LOAD_ROLE = Qt.ItemDataRole.UserRole + 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Name", "Size"])
        header = self.header()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(32)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.setColumnWidth(1, 64)
        self.loader = None
        self.backend = None
        self.root_path = "/"
        self.show_hidden = False
        style = QApplication.style()
        self._directory_icon = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self._file_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self.itemExpanded.connect(self._expanded)
        self.itemDoubleClicked.connect(self._double_clicked)
        self.itemClicked.connect(self._clicked)

    def set_backend(self, backend, start_dir=""):
        if self.loader is not None:
            self.loader.stop()
        self.clear()
        self.backend = backend
        self.loader = RemoteFileLoader(backend, start_dir, self)
        self.loader.loaded.connect(self._loaded)
        self.loader.failed.connect(self._failed)
        self.loader.root_ready.connect(self.show_root)
        self.loader.discover_root()

    def show_root(self, path):
        path = posixpath.normpath(path or "/")
        if not path.startswith("/"):
            path = "/" + path
        self.root_path = path
        generation = self.loader.prioritize() if self.loader else 0
        self.clear()

        if path != "/":
            parent_path = posixpath.dirname(path.rstrip("/")) or "/"
            parent = QTreeWidgetItem(["..", ""])
            pf = parent.font(0); pf.setBold(True); parent.setFont(0, pf)
            parent.setIcon(0, self._directory_icon)
            parent.setData(0, self.PATH_ROLE, parent_path)
            parent.setData(0, self.LOADED_ROLE, True)
            parent.setData(0, self.IS_DIR_ROLE, True)
            parent.setToolTip(0, f"Parent directory: {parent_path}")
            self.addTopLevelItem(parent)

        root = QTreeWidgetItem([path, ""])
        root_font = root.font(0); root_font.setBold(True); root.setFont(0, root_font)
        root.setIcon(0, self._directory_icon)
        root.setData(0, self.PATH_ROLE, path)
        root.setData(0, self.LOADED_ROLE, False)
        root.setData(0, self.IS_DIR_ROLE, True)
        root.setData(0, self.PRELOAD_DEPTH_ROLE, 0)
        root.setData(0, self.PRELOAD_ROLE, True)
        root.setData(0, self.DIRECT_LOAD_ROLE, True)
        root.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        self.addTopLevelItem(root)
        self.setCurrentItem(root)
        root.setExpanded(True)
        if self.loader:
            # Navigation is the payoff for preloading: consume a fresh result
            # immediately. On a real miss, bypass the passive queue and load
            # directly so the user never waits behind background work.
            self.loader.load(
                path,
                force=not self.loader.has_fresh_cache(path),
                generation=generation,
            )
        self.path_changed.emit(path)

    def selected_path(self):
        item = self.currentItem()
        return item.data(0, self.PATH_ROLE) if item else self.root_path

    def selected_is_dir(self):
        item = self.currentItem()
        return bool(item and item.data(0, self.IS_DIR_ROLE))

    def selected_size(self):
        item = self.currentItem()
        value = item.data(0, self.SIZE_ROLE) if item else None
        return int(value) if value is not None else None

    def current_directory(self):
        path = self.selected_path()
        if not path:
            return self.root_path
        return path if self.selected_is_dir() else posixpath.dirname(path)

    def refresh(self):
        item = self.currentItem() or self.topLevelItem(0)
        if not item:
            return
        if not item.data(0, self.IS_DIR_ROLE):
            item = item.parent()
        if item:
            self._reload_item(item)

    def go_home(self):
        if self.loader:
            self.loader.start_dir = ""
            self.loader.discover_root()

    def go_to(self, path):
        path = posixpath.normpath(path or "/")
        if not path.startswith("/"):
            path = posixpath.normpath(posixpath.join(self.root_path, path))
        self.show_root(path)

    def _reload_item(self, item):
        path = item.data(0, self.PATH_ROLE)
        if not path:
            return
        item.setData(0, self.LOADED_ROLE, False)
        item.setData(0, self.PRELOAD_ROLE, False)
        item.setData(0, self.DIRECT_LOAD_ROLE, True)
        item.takeChildren()
        item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        item.setExpanded(True)
        generation = self.loader.prioritize()
        self.loader.load(path, force=True, generation=generation)

    def _expanded(self, item):
        path = item.data(0, self.PATH_ROLE)
        if not path or not item.data(0, self.IS_DIR_ROLE):
            return
        if item.data(0, self.LOADED_ROLE):
            self._prioritize_expanded_contents(item)
        elif not item.data(0, self.DIRECT_LOAD_ROLE):
            generation = self.loader.prioritize()
            # An expanded folder is a new preload centre even though the
            # browser's cwd/root has not changed.
            item.setData(0, self.PRELOAD_DEPTH_ROLE, 0)
            item.setData(0, self.PRELOAD_ROLE, True)
            item.setData(0, self.DIRECT_LOAD_ROLE, True)
            item.setText(1, "…")
            self.loader.load(
                path,
                force=not self.loader.has_fresh_cache(path),
                generation=generation,
            )

    def _prioritize_expanded_contents(self, item):
        generation = self.loader.prioritize()
        visible = []
        hidden = []
        for index in range(item.childCount()):
            child = item.child(index)
            if not child.data(0, self.IS_DIR_ROLE):
                continue
            destination = hidden if child.text(0).startswith(".") else visible
            destination.append(child)
        preload_paths = []
        for child in visible + hidden:
            child.setData(0, self.PRELOAD_ROLE, True)
            child.setData(0, self.DIRECT_LOAD_ROLE, False)
            preload_paths.append(child.data(0, self.PATH_ROLE))
            if len(preload_paths) >= 8:
                break
        self.loader.preload(preload_paths, generation=generation, urgent=True)

    def _loaded(self, path, entries):
        item = self._find_by_path(path)
        if not item:
            return
        item.takeChildren()
        item.setText(1, "")
        item.setData(0, self.LOADED_ROLE, True)
        item.setData(0, self.PRELOAD_ROLE, False)
        item.setData(0, self.DIRECT_LOAD_ROLE, False)
        depth = int(item.data(0, self.PRELOAD_DEPTH_ROLE) or 0)
        # Passive preloading is deliberately limited to one layer. Deeper
        # folders are loaded only when the user requests them.
        visible_preload = [] if depth < 1 else None
        hidden_preload = [] if depth < 1 else None
        for name, is_dir, size in entries:
            if not self.show_hidden and name.startswith("."):
                continue
            full = posixpath.join(path.rstrip("/"), name) if path != "/" else "/" + name
            child = QTreeWidgetItem([name, "" if is_dir else human_size(size)])
            font = child.font(0)
            font.setBold(is_dir)
            hidden = name.startswith(".")
            font.setItalic(hidden)
            child.setFont(0, font)
            if hidden:
                child.setForeground(0, QColor("#7f8a96"))
            child.setIcon(0, self._directory_icon if is_dir else self._file_icon)
            child.setData(0, self.PATH_ROLE, full)
            child.setData(0, self.LOADED_ROLE, not is_dir)
            child.setData(0, self.IS_DIR_ROLE, is_dir)
            child.setData(0, self.SIZE_ROLE, size)
            child.setData(0, self.PRELOAD_DEPTH_ROLE, depth + 1)
            if is_dir:
                child.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                if visible_preload is not None:
                    (hidden_preload if hidden else visible_preload).append(child)
            item.addChild(child)
        if visible_preload is not None:
            # One remote scan warms the useful visible layer. This avoids a
            # Python process and SSH channel round trip for every child.
            preload_paths = []
            for child in (visible_preload + hidden_preload)[:8]:
                child.setData(0, self.PRELOAD_ROLE, True)
                child.setData(0, self.DIRECT_LOAD_ROLE, False)
                preload_paths.append(child.data(0, self.PATH_ROLE))
            self.loader.preload(
                preload_paths,
                generation=self.loader._generation,
                urgent=True,
            )

    def _failed(self, path, message):
        item = self._find_by_path(path)
        if item:
            item.setData(0, self.PRELOAD_ROLE, False)
            item.setData(0, self.DIRECT_LOAD_ROLE, False)
            item.takeChildren()
            item.addChild(QTreeWidgetItem(["<load failed — click to retry>", ""]))
            item.setToolTip(0, message)

    def _find_by_path(self, path):
        def walk(item):
            if item.data(0, self.PATH_ROLE) == path:
                return item
            for i in range(item.childCount()):
                found = walk(item.child(i))
                if found:
                    return found
            return None

        for i in range(self.topLevelItemCount()):
            found = walk(self.topLevelItem(i))
            if found:
                return found
        return None

    def _double_clicked(self, item, column):
        path = item.data(0, self.PATH_ROLE)
        if path and item.data(0, self.IS_DIR_ROLE):
            # Enter the directory in the browser, then optionally point the
            # active terminal there via the existing directory_activated hook.
            self.show_root(path)
            self.directory_activated.emit(path)

    def _clicked(self, item, column):
        # Selection is not navigation. In particular, selecting a file must not
        # replace the browser's current directory/address-bar path.
        # A single click on the disclosure area can otherwise leave the
        # placeholder collapsed without reliably triggering the lazy load.
        # Force the intended state and request the listing immediately.
        if not item.data(0, self.IS_DIR_ROLE):
            return
        if item.data(0, self.LOADED_ROLE):
            return
        if not item.data(0, self.LOADED_ROLE):
            if item.data(0, self.DIRECT_LOAD_ROLE):
                return
            # Recover from a request that failed or was interrupted while its
            # in-flight guard was still set.
            item.setData(0, self.PRELOAD_ROLE, False)
            item.setData(0, self.DIRECT_LOAD_ROLE, True)
            item.setData(0, self.PRELOAD_DEPTH_ROLE, 0)
            item.setExpanded(True)
            path = item.data(0, self.PATH_ROLE)
            item.setText(1, "…")
            # Explicit navigation gets its own immediate request, rather than
            # relying on a passive preload request that may be queued.
            generation = self.loader.prioritize()
            self.loader.load(path, force=True, generation=generation)




class TrapezoidTabBar(QTabBar):
    """Custom trapezoid tabs with fully custom close-button painting."""
    split_requested = Signal(int)
    single_pane_requested = Signal()

    def __init__(self, parent=None, shift_text_left=False):
        super().__init__(parent)
        self.shift_text_left = bool(shift_text_left)
        self.pane_active = False

        # Independent terminal-tab layout controls.
        self.title_shift_x = 0

        # Distance from tab's right edge to the CENTRE of the custom close ×.
        # Increase this to move the × farther LEFT.
        self.close_button_inset = 15

        # Extra room reserved for the custom close × so title text never collapses
        # just because the × moves inward.
        self.close_button_reserved_width = 24
        self.close_hit_radius = 9

        self.setDrawBase(False)
        self.setElideMode(Qt.TextElideMode.ElideRight)

    def tabSizeHint(self, index):
        size = super().tabSizeHint(index)
        size.setWidth(size.width() + 16)
        if self.tabsClosable():
            size.setWidth(size.width() + self.close_button_reserved_width)
        size.setHeight(max(size.height(), 30))
        return size

    def _close_center(self, index):
        rect = self.tabRect(index)
        return (
            rect.right() - self.close_button_inset,
            rect.center().y()
        )

    def _remove_native_close_widgets(self):
        """Qt may create native close widgets when tabsClosable=True; hide them."""
        for index in range(self.count()):
            for side in (QTabBar.ButtonPosition.RightSide, QTabBar.ButtonPosition.LeftSide):
                button = self.tabButton(index, side)
                if button is not None:
                    button.hide()
                    self.setTabButton(index, side, None)

    def tabInserted(self, index):
        super().tabInserted(index)
        QTimer.singleShot(0, self._remove_native_close_widgets)

    def _close_rect(self, index):
        cx, cy = self._close_center(index)
        r = self.close_hit_radius
        return QRect(cx - r, cy - r, r * 2, r * 2)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        mouse_pos = self.mapFromGlobal(self.cursor().pos())
        hovered_tab = self.tabAt(mouse_pos)

        for index in range(self.count()):
            rect = self.tabRect(index)
            if not rect.intersects(event.rect()):
                continue

            selected = index == self.currentIndex()
            hovered = index == hovered_tab
            inset = 7
            radius = 3.5

            path = QPainterPath()
            path.moveTo(rect.left() + inset + radius, rect.top())
            path.lineTo(rect.right() - inset - radius, rect.top())
            path.quadTo(
                rect.right() - inset, rect.top(),
                rect.right() - inset + radius * 0.65, rect.top() + radius
            )
            path.lineTo(rect.right() - radius * 0.65, rect.bottom() - radius)
            path.quadTo(
                rect.right(), rect.bottom(),
                rect.right() - radius, rect.bottom()
            )
            path.lineTo(rect.left() + radius, rect.bottom())
            path.quadTo(
                rect.left(), rect.bottom(),
                rect.left() + radius * 0.65, rect.bottom() - radius
            )
            path.lineTo(rect.left() + inset - radius * 0.65, rect.top() + radius)
            path.quadTo(
                rect.left() + inset, rect.top(),
                rect.left() + inset + radius, rect.top()
            )
            path.closeSubpath()

            if selected and self.pane_active:
                fill = QColor("#315675")
                border = QColor("#8fc8ee")
            elif selected:
                fill = QColor("#2c3f52")
                border = QColor("#5f7588")
            elif hovered:
                fill = QColor("#293746")
                border = QColor("#536b80")
            else:
                fill = QColor("#202832")
                border = QColor("#35404d")

            painter.fillPath(path, fill)
            painter.setPen(QPen(border, 1))
            painter.drawPath(path)

            # Text has its own independent rectangle.
            text_rect = rect.adjusted(10, 0, -10, 0)
            if self.shift_text_left:
                text_rect.translate(self.title_shift_x, 0)

            if self.tabsClosable():
                # Reserve a fixed close-button lane. Moving the × itself no longer
                # changes the title width.
                text_rect.setRight(rect.right() - self.close_button_reserved_width)

            painter.setPen(QColor("#ffffff" if selected else "#cfd7df"))
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine,
                self.fontMetrics().elidedText(
                    self.tabText(index),
                    Qt.TextElideMode.ElideRight,
                    max(0, text_rect.width())
                )
            )

            if self.tabsClosable():
                close_rect = self._close_rect(index)
                close_hover = close_rect.contains(mouse_pos)

                if close_hover:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(255, 255, 255, 28))
                    painter.drawRoundedRect(close_rect.adjusted(1, 1, -1, -1), 3, 3)

                cx, cy = self._close_center(index)
                painter.setPen(QPen(
                    QColor("#f0f3f6" if close_hover or selected else "#aeb8c2"),
                    1.35
                ))
                arm = 4
                painter.drawLine(cx - arm, cy - arm, cx + arm, cy + arm)
                painter.drawLine(cx + arm, cy - arm, cx - arm, cy + arm)

        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            index = self.tabAt(event.position().toPoint())
            if index >= 0:
                menu = QMenu(self)
                split = menu.addAction("Show Side by Side")
                single = menu.addAction("Move All Tabs to One Pane")
                chosen = menu.exec(event.globalPosition().toPoint())
                if chosen is split:
                    self.split_requested.emit(index)
                elif chosen is single:
                    self.single_pane_requested.emit()
            event.accept()
            return
        if self.tabsClosable() and event.button() == Qt.MouseButton.LeftButton:
            index = self.tabAt(event.position().toPoint())
            if index >= 0 and self._close_rect(index).contains(event.position().toPoint()):
                self.tabCloseRequested.emit(index)
                event.accept()
                return
        super().mousePressEvent(event)

# ----------------------------- Built-in file editor ---------------------------

class EditorLineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_line_number_area(event)


class FileTextEditor(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.line_numbers_enabled = True
        self.line_number_area = EditorLineNumberArea(self)
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.update_line_number_area_width()

    def line_number_area_width(self):
        if not self.line_numbers_enabled:
            return 0
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def set_line_numbers_enabled(self, enabled):
        self.line_numbers_enabled = bool(enabled)
        self.line_number_area.setVisible(self.line_numbers_enabled)
        self.update_line_number_area_width()
        self.viewport().update()

    def update_line_number_area_width(self, *_args):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect, dy):
        if not self.line_numbers_enabled:
            return
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.line_numbers_enabled:
            cr = self.contentsRect()
            self.line_number_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))

    def paint_line_number_area(self, event):
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor("#151b22"))
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        painter.setFont(self.font())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#71808d"))
                painter.drawText(
                    0, top, self.line_number_area.width() - 6, self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight, str(block_number + 1)
                )
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1
        painter.end()


class FileSyntaxHighlighter(QSyntaxHighlighter):
    """Small extension-aware highlighter for common admin/config/code files."""
    def __init__(self, document, filename=""):
        super().__init__(document)
        self.filename = filename.lower()
        self.rules = []
        self._build_rules()

    def _fmt(self, colour_value, bold=False, italic=False):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(colour_value))
        fmt.setFontWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
        fmt.setFontItalic(italic)
        return fmt

    def _add(self, pattern, fmt, flags=0):
        self.rules.append((re.compile(pattern, flags), fmt))

    def _build_rules(self):
        string_fmt = self._fmt("#98c379")
        number_fmt = self._fmt("#d19a66")
        comment_fmt = self._fmt("#6f7a86", italic=True)
        keyword_fmt = self._fmt("#c678dd", bold=True)
        key_fmt = self._fmt("#61afef")
        bool_fmt = self._fmt("#56b6c2", bold=True)
        path_fmt = self._fmt("#56b6c2")

        self._add(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', string_fmt)
        self._add(r'\b(?:0x[0-9A-Fa-f]+|\d+(?:\.\d+)?)\b', number_fmt)
        self._add(r'\b(?:true|false|null|none|yes|no|on|off)\b', bool_fmt, re.IGNORECASE)
        self._add(r'(?<!\w)(?:/[A-Za-z0-9._~+@%:/-]+)', path_fmt)

        ext = Path(self.filename).suffix
        base = Path(self.filename).name
        if ext in (".py", ".pyw"):
            self._add(r'\b(?:and|as|assert|async|await|break|class|continue|def|del|elif|else|except|False|finally|for|from|global|if|import|in|is|lambda|None|nonlocal|not|or|pass|raise|return|True|try|while|with|yield)\b', keyword_fmt)
            self._add(r'#.*$', comment_fmt)
        elif ext in (".sh", ".bash", ".zsh", ".fish") or base in (".bashrc", ".zshrc", ".profile"):
            self._add(r'\b(?:if|then|else|elif|fi|for|while|do|done|case|esac|function|in|export|local|readonly|return)\b', keyword_fmt)
            self._add(r'#.*$', comment_fmt)
            self._add(r'\$\{?[A-Za-z_][A-Za-z0-9_]*\}?', key_fmt)
        elif ext in (".json",):
            self._add(r'(?=")[^"\n]+(?="\s*:)', key_fmt)
        elif ext in (".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".service", ".env"):
            self._add(r'^\s*[A-Za-z0-9_.-]+(?=\s*[:=])', key_fmt)
            self._add(r'^\s*[#;].*$', comment_fmt)
            self._add(r'^\s*\[[^\]]+\]\s*$', keyword_fmt)
        elif ext in (".js", ".ts", ".c", ".h", ".cpp", ".hpp", ".java", ".cs", ".go", ".rs"):
            self._add(r'\b(?:if|else|for|while|switch|case|break|continue|return|class|struct|enum|interface|public|private|protected|static|const|let|var|fn|func|package|import|using|namespace|new|try|catch|throw)\b', keyword_fmt)
            self._add(r'//.*$', comment_fmt)
        else:
            self._add(r'^\s*[#;].*$', comment_fmt)
            self._add(r'^[A-Za-z0-9_.-]+(?=\s*[:=])', key_fmt)

    def highlightBlock(self, text):
        for regex, fmt in self.rules:
            for match in regex.finditer(text):
                self.setFormat(match.start(), match.end() - match.start(), fmt)


class FileEditorDialog(QDialog):
    MAX_EDIT_BYTES = 5 * 1024 * 1024

    def __init__(self, path, text, save_callback, metadata=None, parent=None):
        super().__init__(parent)
        self.path = path
        self.save_callback = save_callback
        self.metadata = metadata or {}
        self._saved_text = text
        self.setWindowTitle(f"Edit — {path}")
        self.resize(980, 720)

        self.editor = FileTextEditor()
        self.editor.setPlainText(text)
        self.highlighter = FileSyntaxHighlighter(self.editor.document(), path)

        self.save_button = QPushButton("Save")
        self.save_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find…")
        self.find_edit.setClearButtonEnabled(True)
        self.find_prev = QPushButton("Previous")
        self.find_next = QPushButton("Next")
        self.find_count = QLabel("")
        self.line_numbers = QCheckBox("Line numbers")
        self.line_numbers.setChecked(True)
        self.wrap = QCheckBox("Wrap lines")
        self.wrap.setChecked(False)
        self.highlight = QCheckBox("Highlight")
        self.highlight.setChecked(True)

        top = QHBoxLayout()
        top.setContentsMargins(4, 4, 4, 2)
        top.setSpacing(5)
        top.addWidget(self.save_button)
        top.addSpacing(8)
        top.addWidget(QLabel("Find:"))
        top.addWidget(self.find_edit, 1)
        top.addWidget(self.find_prev)
        top.addWidget(self.find_next)
        top.addWidget(self.find_count)
        top.addSpacing(8)
        top.addWidget(self.line_numbers)
        top.addWidget(self.wrap)
        top.addWidget(self.highlight)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.close)

        self.position_status = QLabel()
        self.count_status = QLabel()
        self.size_status = QLabel()
        self.encoding_status = QLabel()
        self.permissions_status = QLabel()
        self.modified_status = QLabel()

        status = QHBoxLayout()
        status.setContentsMargins(4, 2, 4, 2)
        status.setSpacing(12)
        for label in (
            self.position_status, self.count_status, self.size_status,
            self.encoding_status, self.permissions_status, self.modified_status
        ):
            status.addWidget(label)
        status.addStretch(1)
        status.addWidget(close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(4)
        layout.addLayout(top)
        layout.addWidget(self.editor, 1)
        layout.addLayout(status)

        self.save_button.clicked.connect(self.save)
        self.find_next.clicked.connect(lambda: self.find_text(False))
        self.find_prev.clicked.connect(lambda: self.find_text(True))
        self.find_edit.returnPressed.connect(lambda: self.find_text(False))
        self.find_edit.textChanged.connect(self._update_find_count)
        self.line_numbers.toggled.connect(self.editor.set_line_numbers_enabled)
        self.wrap.toggled.connect(self._toggle_wrap)
        self.highlight.toggled.connect(self._toggle_highlighting)
        self.editor.textChanged.connect(self._update_status)
        self.editor.cursorPositionChanged.connect(self._update_status)

        QTimer.singleShot(0, self._update_find_count)
        QTimer.singleShot(0, self._update_status)

    def _update_status(self):
        text = self.editor.toPlainText()
        cursor = self.editor.textCursor()
        line = cursor.blockNumber() + 1
        column = cursor.positionInBlock() + 1
        lines = max(1, self.editor.document().blockCount())
        chars = len(text)
        codec = self.metadata.get("encoding_codec", "utf-8")
        try:
            byte_size = len(text.encode(codec))
        except UnicodeEncodeError:
            byte_size = len(text.encode("utf-8"))
        permissions = self.metadata.get("permissions", "—")
        encoding = self.metadata.get("encoding", "UTF-8")
        line_ending = self.metadata.get("line_ending", "LF")
        modified = text != self._saved_text

        self.position_status.setText(f"Ln {line}, Col {column}")
        self.count_status.setText(f"{lines} lines  |  {chars} chars")
        self.size_status.setText(f"{human_size(byte_size)}")
        self.encoding_status.setText(f"{encoding}  |  {line_ending}")
        self.permissions_status.setText(f"Permissions {permissions}")
        self.modified_status.setText("Modified" if modified else "Saved")
        self.modified_status.setStyleSheet(
            "QLabel { color:#e5c07b; font-weight:700; }" if modified
            else "QLabel { color:#98c379; }"
        )

    def _toggle_wrap(self, enabled):
        self.editor.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if enabled else QPlainTextEdit.LineWrapMode.NoWrap
        )

    def _toggle_highlighting(self, enabled):
        self.highlighter.setDocument(self.editor.document() if enabled else None)
        if not enabled:
            # Removing a highlighter does not always immediately clear formats.
            self.editor.document().markContentsDirty(0, self.editor.document().characterCount())
        else:
            self.highlighter.rehighlight()

    def _update_find_count(self, *_args):
        query = self.find_edit.text()
        if not query:
            self.find_count.setText("")
            return
        flags = 0
        try:
            count = len(re.findall(re.escape(query), self.editor.toPlainText(), flags))
        except Exception:
            count = 0
        self.find_count.setText(f"{count} match{'es' if count != 1 else ''}")

    def find_text(self, backwards=False):
        query = self.find_edit.text()
        if not query:
            self.find_edit.setFocus()
            return
        flags = QTextDocument.FindFlag.FindBackward if backwards else QTextDocument.FindFlag(0)
        if self.editor.find(query, flags):
            return
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End if backwards else QTextCursor.MoveOperation.Start)
        self.editor.setTextCursor(cursor)
        self.editor.find(query, flags)

    def save(self):
        text = self.editor.toPlainText()
        try:
            self.save_callback(text)
            self._saved_text = text
            self.metadata["size"] = len(text.encode("utf-8"))
            self.editor.document().setModified(False)
            self.setWindowTitle(f"Edit — {self.path}")
            self._update_status()
        except Exception as exc:
            QMessageBox.warning(self, "Save", str(exc))

    def closeEvent(self, event):
        if self.editor.toPlainText() != self._saved_text:
            choice = QMessageBox.question(
                self, "Unsaved Changes",
                "Save changes before closing?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel
            )
            if choice == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if choice == QMessageBox.StandardButton.Save:
                try:
                    self.save()
                except Exception:
                    event.ignore()
                    return
        event.accept()


# ----------------------------- Main window ------------------------------------

class ExternalEditSession(QObject):
    downloaded = Signal(object)
    failed = Signal(object, str)
    local_changed = Signal(object)
    remote_checked = Signal(object, bool)
    upload_progress = Signal(str, int, int)
    uploaded = Signal(object)

    def __init__(self, backend, remote_path, cache_root, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.remote_path = remote_path
        key = hashlib.sha256(remote_path.encode("utf-8", errors="replace")).hexdigest()[:16]
        folder = Path(cache_root) / key
        folder.mkdir(parents=True, exist_ok=True)
        self.local_path = folder / (posixpath.basename(remote_path) or "remote-file")
        self.remote_version = None
        self.remote_mode = None
        self.baseline = None
        self._prompt_pending = False
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._file_changed)
        self._change_timer = QTimer(self)
        self._change_timer.setSingleShot(True)
        self._change_timer.setInterval(700)
        self._change_timer.timeout.connect(self._verify_local_change)

    @staticmethod
    def _version(attrs):
        return int(attrs.st_size or 0), float(attrs.st_mtime or 0)

    def start(self):
        threading.Thread(target=self._download, daemon=True).start()

    def _download(self):
        try:
            sftp = self.backend.client.open_sftp()
            try:
                attrs = sftp.stat(self.remote_path)
                self.remote_version = self._version(attrs)
                self.remote_mode = stat.S_IMODE(attrs.st_mode)
                sftp.get(
                    self.remote_path,
                    str(self.local_path),
                    callback=lambda done, total: self.upload_progress.emit(self.local_path.name, done, total),
                )
            finally:
                sftp.close()
            self.baseline = file_fingerprint(self.local_path)
            self.downloaded.emit(self)
        except Exception as exc:
            self.failed.emit(self, f"Could not open remote file: {exc}")

    def open_local_copy(self):
        self._ensure_watched()
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.local_path))):
            self.failed.emit(self, "The operating system could not open this file type.")

    def _ensure_watched(self):
        path = str(self.local_path)
        if self.local_path.exists() and path not in self.watcher.files():
            self.watcher.addPath(path)

    def _file_changed(self, _path):
        # Many editors save by replacing the file. Reattach after their atomic
        # rename has settled, then compare actual content rather than mtime.
        self._change_timer.start()

    def _verify_local_change(self):
        self._ensure_watched()
        if self._prompt_pending or not self.local_path.exists():
            return
        try:
            current = file_fingerprint(self.local_path)
        except OSError:
            return
        if current != self.baseline:
            self._prompt_pending = True
            self.local_changed.emit(self)

    def check_remote(self):
        threading.Thread(target=self._check_remote, daemon=True).start()

    def _check_remote(self):
        try:
            sftp = self.backend.client.open_sftp()
            try:
                current = self._version(sftp.stat(self.remote_path))
            finally:
                sftp.close()
            self.remote_checked.emit(self, current != self.remote_version)
        except Exception as exc:
            self._prompt_pending = False
            self.failed.emit(self, f"Could not check the remote file before upload: {exc}")

    def decline_upload(self):
        try:
            self.baseline = file_fingerprint(self.local_path)
        except OSError:
            pass
        self._prompt_pending = False
        self._ensure_watched()

    def upload(self):
        threading.Thread(target=self._upload, daemon=True).start()

    def _upload(self):
        temporary = posixpath.join(
            posixpath.dirname(self.remote_path),
            f".{posixpath.basename(self.remote_path)}.powerterm-{uuid.uuid4().hex}.tmp",
        )
        sftp = None
        try:
            sftp = self.backend.client.open_sftp()
            sftp.put(
                str(self.local_path),
                temporary,
                callback=lambda done, total: self.upload_progress.emit(self.local_path.name, done, total),
            )
            if self.remote_mode is not None:
                sftp.chmod(temporary, self.remote_mode)
            try:
                sftp.posix_rename(temporary, self.remote_path)
            except Exception:
                # Compatibility path for servers without the OpenSSH atomic
                # rename extension. SFTP put overwrites the existing file.
                sftp.put(
                    str(self.local_path),
                    self.remote_path,
                    callback=lambda done, total: self.upload_progress.emit(self.local_path.name, done, total),
                )
                try:
                    sftp.remove(temporary)
                except Exception:
                    pass
            attrs = sftp.stat(self.remote_path)
            self.remote_version = self._version(attrs)
            self.baseline = file_fingerprint(self.local_path)
            self._prompt_pending = False
            self.uploaded.emit(self)
        except Exception as exc:
            self._prompt_pending = False
            if sftp is not None:
                try:
                    sftp.remove(temporary)
                except Exception:
                    pass
            self.failed.emit(self, f"Could not replace the remote file: {exc}")
        finally:
            if sftp is not None:
                sftp.close()


class TransferWorker(QObject):
    finished = Signal(str)
    failed = Signal(str)
    progress = Signal(str, int, int)

    def __init__(self, backend, parent=None):
        super().__init__(parent)
        self.backend = backend

    def upload(self, local_path, remote_dir):
        threading.Thread(target=self._upload_worker, args=(local_path, remote_dir), daemon=True).start()

    def download(self, remote_path, local_dir):
        threading.Thread(target=self._download_worker, args=(remote_path, local_dir), daemon=True).start()

    def _upload_worker(self, local_path, remote_dir):
        try:
            sftp = self.backend.client.open_sftp()
            try:
                src = Path(local_path)
                dest = posixpath.join(remote_dir.rstrip("/"), src.name) if remote_dir != "/" else "/" + src.name
                self._put_recursive(sftp, src, dest)
            finally:
                sftp.close()
            self.finished.emit(f"Uploaded {local_path} → {remote_dir}")
        except Exception as exc:
            self.failed.emit(f"Upload failed: {exc}")

    def _put_recursive(self, sftp, src, dest):
        src = Path(src)
        if src.is_dir():
            try:
                sftp.mkdir(dest)
            except IOError:
                # Existing directories are valid merge targets. Do not swallow
                # permission, missing-parent, or file-at-destination errors.
                attrs = sftp.stat(dest)
                if not stat.S_ISDIR(attrs.st_mode):
                    raise RuntimeError(f"Remote destination exists and is not a folder: {dest}")
            for child in src.iterdir():
                self._put_recursive(sftp, child, posixpath.join(dest, child.name))
        else:
            sftp.put(str(src), dest, callback=lambda done, total: self.progress.emit(src.name, done, total))

    def _download_worker(self, remote_path, local_dir):
        try:
            sftp = self.backend.client.open_sftp()
            try:
                name = posixpath.basename(remote_path.rstrip("/")) or "download"
                self._get_recursive(sftp, remote_path, Path(local_dir) / name)
            finally:
                sftp.close()
            self.finished.emit(f"Downloaded {remote_path} → {local_dir}")
        except Exception as exc:
            self.failed.emit(f"Download failed: {exc}")

    def _get_recursive(self, sftp, remote_path, dest):
        attrs = sftp.stat(remote_path)
        if stat.S_ISDIR(attrs.st_mode):
            dest.mkdir(parents=True, exist_ok=True)
            for child in sftp.listdir_attr(remote_path):
                if child.filename in (".", ".."):
                    continue
                child_remote = posixpath.join(remote_path.rstrip("/"), child.filename) if remote_path != "/" else "/" + child.filename
                self._get_recursive(sftp, child_remote, dest / child.filename)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote_path, str(dest), callback=lambda done, total: self.progress.emit(dest.name, done, total))


class SystemInfoHighlighter(QSyntaxHighlighter):
    """Lightweight generic highlighting for sysadmin command output."""
    def __init__(self, document):
        super().__init__(document)
        self.header_fmt = QTextCharFormat(); self.header_fmt.setForeground(QColor("#8ecbff")); self.header_fmt.setFontWeight(QFont.Weight.Bold)
        self.device_fmt = QTextCharFormat(); self.device_fmt.setForeground(QColor("#8fe3a1")); self.device_fmt.setFontWeight(QFont.Weight.Bold)
        self.number_fmt = QTextCharFormat(); self.number_fmt.setForeground(QColor("#f0c674"))
        self.ip_fmt = QTextCharFormat(); self.ip_fmt.setForeground(QColor("#c7a0ff")); self.ip_fmt.setFontWeight(QFont.Weight.Bold)
        self.warn_fmt = QTextCharFormat(); self.warn_fmt.setForeground(QColor("#ff8f8f")); self.warn_fmt.setFontWeight(QFont.Weight.Bold)
        self.key_fmt = QTextCharFormat(); self.key_fmt.setForeground(QColor("#7fd5d5"))

    def highlightBlock(self, text):
        if not text:
            return
        stripped = text.strip()
        if stripped.endswith(":") or stripped.startswith("===") or stripped in ("Block devices:", "Addresses:", "Routes:", "Meminfo:"):
            self.setFormat(0, len(text), self.header_fmt)
        for m in re.finditer(r"(?:/dev/\S+|\\\\\.\\\S+|\bsd[a-z]\d*\b|\bnvme\d+n\d+(?:p\d+)?\b|\btty\S*\b)", text):
            self.setFormat(m.start(), m.end()-m.start(), self.device_fmt)
        for m in re.finditer(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b|\b[0-9a-fA-F:]{3,}:[0-9a-fA-F:]+\b", text):
            self.setFormat(m.start(), m.end()-m.start(), self.ip_fmt)
        for m in re.finditer(r"(?<!\w)\d+(?:\.\d+)?\s*(?:%|KiB|MiB|GiB|TiB|KB|MB|GB|TB|MHz|GHz|kB|B/s|KB/s|MB/s|GB/s)?\b", text):
            self.setFormat(m.start(), m.end()-m.start(), self.number_fmt)
        for m in re.finditer(r"\b(?:error|failed|warning|not installed|unavailable|down|disabled)\b", text, re.IGNORECASE):
            self.setFormat(m.start(), m.end()-m.start(), self.warn_fmt)
        m = re.match(r"^\s*([A-Za-z][A-Za-z0-9 _./()-]{1,28}:)", text)
        if m:
            self.setFormat(m.start(1), len(m.group(1)), self.key_fmt)


class InfoDialog(QDialog):
    def __init__(self, title, text, settings=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 560)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        settings = settings or {}
        font = QFont(settings.get("info_font_family", "Monospace"))
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(max(7, int(settings.get("info_font_size", 10))))
        view.setFont(font)
        view.setStyleSheet("QPlainTextEdit { background:#0f141a; color:#dce3ea; border:1px solid #33404d; padding:8px; }")
        view.setPlainText(text.rstrip() + "\n")
        self.highlighter = SystemInfoHighlighter(view.document())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.clicked.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(view, 1)
        layout.addWidget(buttons)


class StyledFileSystemModel(QFileSystemModel):
    """Make directories and hidden entries visually distinct without replacing native icons."""
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and index.column() == 0:
            path = self.filePath(index)
            name = os.path.basename(path.rstrip(os.sep)) or path
            hidden = name.startswith(".") and name not in (".", "..")
            is_dir = self.isDir(index)
            if role == Qt.ItemDataRole.FontRole:
                font = QFont(QApplication.font())
                font.setBold(is_dir)
                font.setItalic(hidden)
                return font
            if role == Qt.ItemDataRole.ForegroundRole and hidden:
                return QColor("#7f8a96")
        return super().data(index, role)


class SavedCommandDialog(QDialog):
    def __init__(self, command=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Saved Command")
        command = command or {}
        self.name = QLineEdit(command.get("name", ""))
        self.command = QPlainTextEdit(command.get("command", ""))
        self.command.setMaximumHeight(110)
        self.confirm = QCheckBox("Ask for confirmation before running")
        self.confirm.setChecked(bool(command.get("confirm", False)))
        self.may_prompt = QCheckBox("This command may request a password / elevation")
        self.may_prompt.setChecked(bool(command.get("may_prompt", False)))
        self.may_prompt.setToolTip("Credentials are entered directly into the terminal. PowerTerm never stores a sudo/elevation password.")
        form = QFormLayout()
        form.addRow("Name:", self.name)
        form.addRow("Command:", self.command)
        form.addRow("", self.confirm)
        form.addRow("", self.may_prompt)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self); layout.addLayout(form); layout.addWidget(buttons)

    def value(self):
        return {"id": str(uuid.uuid4()), "name": self.name.text().strip(),
                "command": self.command.toPlainText().strip(), "confirm": self.confirm.isChecked(),
                "may_prompt": self.may_prompt.isChecked()}


class DeleteConfirmationDialog(QDialog):
    def __init__(self, path, is_dir, entries, parent=None, selection_count=1):
        super().__init__(parent)
        self.setWindowTitle("Confirm deletion")
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        kind = (f"{selection_count} selected items" if selection_count > 1
                else "folder and everything inside it" if is_dir else "file")
        layout.addWidget(QLabel(f"Permanently delete this {kind}?\n{path}"))
        tree = QTreeWidget()
        tree.setHeaderLabels(["Contents", "Type"])
        tree.setRootIsDecorated(True)
        root = QTreeWidgetItem([path, "Folder" if is_dir else "File"])
        tree.addTopLevelItem(root)
        for name, child_dir, children in entries:
            item = QTreeWidgetItem([name, "Folder" if child_dir else "File"])
            root.addChild(item)
            stack = [(item, children)]
            while stack:
                parent_item, child_entries = stack.pop()
                for child_name, child_is_dir, grand_children in child_entries:
                    child = QTreeWidgetItem([child_name, "Folder" if child_is_dir else "File"])
                    parent_item.addChild(child)
                    if child_is_dir:
                        stack.append((child, grand_children))
        root.setExpanded(True)
        tree.expandToDepth(1)
        # QTreeWidget's default interactive sizing leaves the first column at
        # a very small width.  Size both columns from the actual preview after
        # all items have been inserted so paths and names are immediately
        # readable when the dialog opens.
        tree.resizeColumnToContents(0)
        tree.resizeColumnToContents(1)
        tree.setColumnWidth(0, max(tree.columnWidth(0), 260))
        tree.setColumnWidth(1, max(tree.columnWidth(1), 70))
        layout.addWidget(tree, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Yes).setText("Delete")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class DeletionLogDialog(QDialog):
    def __init__(self, records, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Deletion Log")
        self.resize(900, 520)
        layout = QVBoxLayout(self)
        note = QLabel("Records files and folders removed through PowerTerm. This is an audit log, not a recycle bin.")
        note.setWordWrap(True)
        layout.addWidget(note)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Time", "Source", "Result", "Type", "Path"])
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        for record in reversed(records):
            tree.addTopLevelItem(QTreeWidgetItem([
                str(record.get("time", "")), str(record.get("source", "")),
                str(record.get("result", "")), str(record.get("type", "")),
                str(record.get("path", "")),
            ]))
        for column in range(4):
            tree.resizeColumnToContents(column)
        tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(tree, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.clicked.connect(self.accept)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    remote_stats_ready = Signal(object)
    info_ready = Signal(object)
    update_check_finished = Signal(object)

    def __init__(self, show_early=False):
        super().__init__()
        self.update_check_finished.connect(self._apply_update_check_result)
        self.setWindowTitle("PowerTerm")
        self.resize(1320, 820)
        if show_early:
            self._show_startup_frame()
        self.mode = "none"
        self.session_generation = 0
        self.remote_backend = None
        self.remote_stats_busy = False
        self.remote_prev_cpu = None
        self.remote_prev_net = None
        self.current_profile = None
        self._transfer_workers = []
        self._delete_preview_cache = {}
        self._delete_preview_jobs = set()
        self._external_cache_root = Path(tempfile.mkdtemp(prefix="powerterm-external-"))
        self._external_edit_sessions = set()
        self._update_check_in_progress = False
        self._about_dialog = None
        self._about_update_label = None
        self._about_update_button = None
        self.host_store = HostStore()
        self.deletion_log_path = self.host_store.path.with_name("deletions.log")
        self.credential_store = CredentialStore()
        self.host_profiles = self.host_store.load()
        self.terminal_settings = self.host_store.terminal_settings()
        self.file_browser_settings = self.host_store.file_browser_settings()
        self.terminal = None

        self.local_model = StyledFileSystemModel(self)
        local_filter = QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.System
        if self.file_browser_settings.get("show_hidden", False):
            local_filter |= QDir.Filter.Hidden
        self.local_model.setFilter(local_filter)
        # Index the directory actually shown at startup. QFileSystemModel can
        # still navigate elsewhere later without scanning every filesystem
        # root before the first window paint.
        self.local_model.setRootPath(str(Path.home()))
        self.local_tree = QTreeView()
        self.local_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.local_tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.local_tree.setModel(self.local_model)
        self.local_tree.setRootIndex(self.local_model.index(str(Path.home())))
        local_header = self.local_tree.header()
        local_header.setStretchLastSection(False)
        local_header.setMinimumSectionSize(32)
        local_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for section in range(1, self.local_model.columnCount()):
            local_header.setSectionResizeMode(section, QHeaderView.ResizeMode.Interactive)
        self.local_tree.setColumnWidth(1, 64)
        self.local_tree.doubleClicked.connect(self.local_file_double_clicked)
        self.local_tree.clicked.connect(self.local_file_clicked)
        self.local_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.local_tree.customContextMenuRequested.connect(self.local_context_menu)

        self.remote_tree = RemoteFileTree()
        self.remote_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.remote_tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.remote_tree.show_hidden = bool(self.file_browser_settings.get("show_hidden", False))
        self.remote_tree.directory_activated.connect(self.remote_directory_activated)
        self.remote_tree.itemSelectionChanged.connect(self.update_file_selection_display)
        self.remote_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.remote_tree.customContextMenuRequested.connect(self.remote_context_menu)

        self.local_path_label = QLineEdit(str(Path.home()))
        self.remote_path_label = QLineEdit("/")
        for edit in (self.local_path_label, self.remote_path_label):
            edit.setClearButtonEnabled(True)
        self.local_path_label.returnPressed.connect(lambda: self.navigate_path(False))
        self.remote_path_label.returnPressed.connect(lambda: self.navigate_path(True))
        self.remote_tree.path_changed.connect(self.on_remote_browser_path_changed)

        # File-page controls use these actions, so create the actions before
        # constructing the pages that install their address-bar buttons.
        self._build_menu()

        self.file_stack = QStackedWidget()
        self.local_file_page = self._make_file_page(False)
        self.remote_file_page = self._make_file_page(True)
        self.file_stack.addWidget(self.local_file_page)
        self.file_stack.addWidget(self.remote_file_page)
        files_tab = QWidget(); fl = QVBoxLayout(files_tab); fl.setContentsMargins(0,0,0,0); fl.addWidget(self.file_stack)

        self.host_tree = QTreeWidget()
        self.host_tree.setHeaderLabels(["Name", "Host", "User", "Port"])
        self.host_tree.setColumnWidth(0, 140)
        self.host_tree.itemDoubleClicked.connect(lambda item, col: self.connect_selected_host())
        self.host_add = QPushButton(self._std_icon(QStyle.StandardPixmap.SP_FileDialogNewFolder), "Add")
        self.host_edit = QPushButton(self._std_icon(QStyle.StandardPixmap.SP_FileDialogDetailedView), "Edit")
        self.host_remove = QPushButton(self._std_icon(QStyle.StandardPixmap.SP_TrashIcon), "Remove")
        self.host_connect = QPushButton(self._std_icon(QStyle.StandardPixmap.SP_DialogOpenButton), "Open")
        for button in (self.host_add, self.host_edit, self.host_remove, self.host_connect):
            button.setObjectName("PaneActionButton")
            button.setIconSize(QSize(16, 16))
            button.setFixedHeight(30)
        self.host_add.clicked.connect(self.add_host); self.host_edit.clicked.connect(self.edit_host)
        self.host_remove.clicked.connect(self.remove_host); self.host_connect.clicked.connect(self.connect_selected_host)
        hb=QHBoxLayout(); hb.setContentsMargins(3,3,3,3); hb.setSpacing(4); hb.addWidget(self.host_add); hb.addWidget(self.host_edit); hb.addWidget(self.host_remove); hb.addStretch(1); hb.addWidget(self.host_connect)
        hosts_tab=QWidget(); hl=QVBoxLayout(hosts_tab); hl.setContentsMargins(0,0,0,0); hl.setSpacing(3); hl.addLayout(hb); hl.addWidget(self.host_tree,1)

        self.command_tree = QTreeWidget()
        self.command_tree.setHeaderLabels(["Saved Commands"])
        self.command_tree.setAlternatingRowColors(True)
        self.command_tree.itemDoubleClicked.connect(lambda item, col: self.run_saved_command(item))
        self.command_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.command_tree.customContextMenuRequested.connect(self.saved_command_context_menu)
        self.command_add_group = QPushButton(self._std_icon(QStyle.StandardPixmap.SP_DirIcon), "+ Group")
        self.command_add_group.setToolTip("Add a top-level group, or a subgroup beneath the selected group")
        self.command_add = QPushButton(self._std_icon(QStyle.StandardPixmap.SP_FileIcon), "+ Command")
        self.command_edit = QPushButton(self._std_icon(QStyle.StandardPixmap.SP_FileDialogDetailedView), "Edit")
        self.command_remove = QPushButton(self._std_icon(QStyle.StandardPixmap.SP_TrashIcon), "Remove")
        self.command_up = QPushButton(self._std_icon(QStyle.StandardPixmap.SP_ArrowUp), "Up")
        self.command_down = QPushButton(self._std_icon(QStyle.StandardPixmap.SP_ArrowDown), "Down")
        self.command_run = QPushButton(self._std_icon(QStyle.StandardPixmap.SP_MediaPlay), "Run")
        for button in (self.command_add_group, self.command_add, self.command_edit, self.command_remove,
                       self.command_up, self.command_down, self.command_run):
            button.setObjectName("PaneActionButton")
            button.setIconSize(QSize(16, 16))
            button.setFixedHeight(30)
        self.command_add_group.clicked.connect(self.add_command_group)
        self.command_add.clicked.connect(self.add_saved_command)
        self.command_edit.clicked.connect(self.edit_saved_command)
        self.command_remove.clicked.connect(self.remove_saved_command)
        self.command_up.clicked.connect(lambda: self.move_saved_command(-1))
        self.command_down.clicked.connect(lambda: self.move_saved_command(1))
        self.command_run.clicked.connect(lambda: self.run_saved_command(self.command_tree.currentItem()))
        # Saved Commands actions: creation/run on the first row; operations
        # on the selected item on the second row.
        cb = QVBoxLayout()
        cb.setContentsMargins(3, 3, 3, 3)
        cb.setSpacing(4)

        command_row1 = QHBoxLayout()
        command_row1.setContentsMargins(0, 0, 0, 0)
        command_row1.setSpacing(4)
        command_row1.addWidget(self.command_add_group)
        command_row1.addWidget(self.command_add)
        command_row1.addStretch(1)
        command_row1.addWidget(self.command_run)

        command_row2 = QHBoxLayout()
        command_row2.setContentsMargins(0, 0, 0, 0)
        command_row2.setSpacing(4)
        command_row2.addWidget(self.command_edit)
        command_row2.addWidget(self.command_remove)
        command_row2.addWidget(self.command_up)
        command_row2.addWidget(self.command_down)
        command_row2.addStretch(1)

        cb.addLayout(command_row1)
        cb.addLayout(command_row2)

        commands_tab = QWidget()
        cl = QVBoxLayout(commands_tab)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(3)
        cl.addLayout(cb)
        cl.addWidget(self.command_tree, 1)

        self.left_tabs=QTabWidget(); self.left_tabs.setTabBar(TrapezoidTabBar(self.left_tabs, shift_text_left=False)); self.left_tabs.addTab(hosts_tab,"Hosts"); self.left_tabs.addTab(files_tab,"Files"); self.left_tabs.addTab(commands_tab,"Saved Commands")
        self.reload_hosts_ui(); self.reload_saved_commands_ui()

        self.session_panes = []
        self._active_session_pane = None
        self._focused_terminal = None
        self.session_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.session_splitter.setChildrenCollapsible(False)
        self.session_splitter.setHandleWidth(5)
        self.session_tabs = self._create_session_pane()
        self.session_splitter.addWidget(self.session_tabs)
        self._set_active_session_pane(self.session_tabs)

        self.canvas=QStackedWidget(); self.home_page=self._build_home_page(); self.canvas.addWidget(self.home_page); self.canvas.addWidget(self.session_splitter); self.canvas.setCurrentWidget(self.home_page)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.left_tabs)
        splitter.addWidget(self.canvas)

        # The navigation pane may be made narrow, but never collapsed to zero.
        self.left_tabs.setMinimumWidth(220)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setHandleWidth(5)
        splitter.setSizes([380, 940])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self.main_splitter = splitter
        self.setCentralWidget(splitter)

        self.source_status=QLabel("No active session"); self.source_status.setStyleSheet("QLabel { padding: 0 8px; }"); self.statusBar().addWidget(self.source_status,1)
        self.connection_progress = QProgressBar()
        self.connection_progress.setObjectName("ConnectionProgress")
        self.connection_progress.setRange(0, 0)
        self.connection_progress.setTextVisible(False)
        self.connection_progress.setFixedWidth(150)
        self.connection_progress.setFixedHeight(12)
        self.connection_progress.hide()
        self.statusBar().addPermanentWidget(self.connection_progress)
        self.transfer_progress = QProgressBar()
        self.transfer_progress.setObjectName("TransferProgress")
        self.transfer_progress.setRange(0, 100)
        self.transfer_progress.setTextVisible(True)
        self.transfer_progress.setFixedWidth(210)
        self.transfer_progress.hide()
        self.statusBar().addPermanentWidget(self.transfer_progress)
        self.cpu_status=self._status_button("CPU —","cpu"); self.ram_status=self._status_button("RAM —","ram"); self.disk_status=self._status_button("Disc —","disk"); self.net_status=self._status_button("Network —","network"); self.usb_status=self._status_button("USB","usb")
        for b in (self.cpu_status,self.ram_status,self.disk_status,self.net_status,self.usb_status):
            b.setContentsMargins(0,0,0,0)
            self.statusBar().addPermanentWidget(b)
            b.setEnabled(False)

        self.last_net=None; self.last_net_time=time.monotonic()
        self.status_timer=QTimer(self); self.status_timer.timeout.connect(self.update_status); self.status_timer.start(1500)
        self.cwd_timer=QTimer(self); self.cwd_timer.timeout.connect(self.poll_terminal_cwd); self.cwd_timer.start(700)
        self.remote_stats_ready.connect(self.apply_remote_stats); self.info_ready.connect(self._show_info_result); self._apply_theme(); self._build_file_toolbars(); self._build_ribbon(); self._update_ribbon_state()
        # Let Qt paint the window before warming optional status counters.
        QTimer.singleShot(0, self._warm_status_counters)

    def _show_startup_frame(self):
        """Paint the native window frame before constructing heavy content."""
        startup = QWidget()
        startup.setObjectName("StartupSurface")
        startup.setStyleSheet(
            "QWidget#StartupSurface { background:qlineargradient(x1:0,y1:0,x2:1,y2:1, "
            "stop:0 #21343a, stop:1 #242b35); color:#e6edf4; }"
        )
        layout = QVBoxLayout(startup)
        layout.addStretch(1)
        label = QLabel("Starting PowerTerm…")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = label.font()
        font.setPointSize(max(12, font.pointSize() + 3))
        font.setBold(True)
        label.setFont(font)
        layout.addWidget(label)
        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress.setFixedSize(240, 10)
        layout.addWidget(progress, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        self.setCentralWidget(startup)
        self.show()
        # Excluding input prevents interaction with a deliberately incomplete
        # UI while still allowing Qt and the window manager to paint it.
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    def _warm_status_counters(self):
        try:
            self.last_net = psutil.net_io_counters()
            self.last_net_time = time.monotonic()
            psutil.cpu_percent(None)
        except Exception:
            self.last_net = None

    def _make_file_page(self, remote):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(3,3,3,5); layout.setSpacing(5)
        path_edit=self.remote_path_label if remote else self.local_path_label
        tree=self.remote_tree if remote else self.local_tree

        pathbar=QHBoxLayout()
        up_button = QToolButton(page)
        up_button.setDefaultAction(self.action_file_up)
        up_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        up_button.setToolTip("Go to parent folder")
        up_button.setFixedSize(30, 28)
        pathbar.addWidget(up_button)
        pathbar.addWidget(path_edit,1)
        go=QPushButton("Go")
        go.setToolTip("Navigate to the path in the address bar")
        go.clicked.connect(lambda: self.navigate_path(remote))
        pathbar.addWidget(go)
        layout.addLayout(pathbar)

        selection_label = QLabel("Selection: (none)", page)
        selection_label.setObjectName("FileSelectionLabel")
        selection_label.setToolTip("The exact selected file or folder path")
        selection_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(selection_label)
        if not hasattr(self, "file_selection_labels"): self.file_selection_labels = {}
        self.file_selection_labels[remote] = selection_label

        file_toolbar = QToolBar("Remote Files" if remote else "Local Files", page)
        file_toolbar.setObjectName("FilePaneToolbar")
        file_toolbar.setMovable(False)
        file_toolbar.setFloatable(False)
        file_toolbar.setIconSize(QSize(16, 16))
        file_toolbar.setFixedHeight(38)
        if remote:
            self.remote_file_toolbar = file_toolbar
        else:
            self.local_file_toolbar = file_toolbar

        options_panel=QWidget(page)
        options=QVBoxLayout(options_panel)
        options.setContentsMargins(2, 0, 2, 0)
        options.setSpacing(0)
        follow_browser=QCheckBox("Terminal - follow the File browser")
        follow_browser.setChecked(bool(self.terminal_settings.get("follow_file_browser_in_terminal", self.terminal_settings.get("follow_terminal_directory", False))))
        follow_browser.toggled.connect(self.set_follow_file_browser)
        follow_terminal=QCheckBox("File browser - follow the terminal")
        follow_terminal.setChecked(bool(self.terminal_settings.get("follow_terminal_in_file_browser", False)))
        follow_terminal.toggled.connect(self.set_follow_terminal_browser)
        if not hasattr(self,"follow_boxes"): self.follow_boxes=[]
        self.follow_boxes.extend((follow_browser, follow_terminal))
        hidden=QCheckBox("Show hidden files and folders")
        hidden.setChecked(bool(self.file_browser_settings.get("show_hidden", False)))
        hidden.toggled.connect(self.set_show_hidden_files)
        if not hasattr(self, "hidden_boxes"): self.hidden_boxes=[]
        self.hidden_boxes.append(hidden)
        options.addWidget(follow_browser)
        options.addWidget(follow_terminal)
        options.addWidget(hidden)
        layout.addWidget(file_toolbar)
        layout.addWidget(options_panel)
        layout.addWidget(tree,1)
        return page

    def navigate_path(self, remote=False):
        path=(self.remote_path_label.text() if remote else self.local_path_label.text()).strip()
        if not path: return
        if remote:
            if self.mode != "ssh": return
            try:
                sftp = self.remote_backend.client.open_sftp()
                try:
                    attrs = sftp.stat(path)
                finally:
                    sftp.close()
                if not stat.S_ISDIR(attrs.st_mode):
                    QMessageBox.information(
                        self, "Path",
                        f"This is a file, not a directory:\n{path}"
                    )
                    return
            except Exception as exc:
                QMessageBox.warning(self, "Path", f"Could not open directory:\n{path}\n\n{exc}")
                return

            self.remote_tree.go_to(path)
            session=self.active_session()
            if session: session.remote_path=path
            if self.terminal_settings.get("follow_file_browser_in_terminal", False):
                self.send_cd(path)
        else:
            path=os.path.abspath(os.path.expanduser(path))
            if not os.path.isdir(path): QMessageBox.warning(self,"Path",f"Directory does not exist:\n{path}"); return
            idx=self.local_model.index(path)
            if idx.isValid():
                self.local_tree.setRootIndex(idx); self.local_path_label.setText(path)
                if self.terminal_settings.get("follow_file_browser_in_terminal", False):
                    self.send_cd(path)

    def _build_home_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addStretch(1)
        title = QLabel("PowerTerm")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = title.font(); font.setPointSize(24); font.setBold(True); title.setFont(font)
        subtitle = QLabel("Choose a session to begin")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        local = QPushButton("Local Terminal")
        ssh = QPushButton("Quick SSH…")
        saved = QPushButton("Saved Hosts")
        for b in (local, ssh, saved):
            b.setMinimumWidth(240); b.setMinimumHeight(42)
        local.setIcon(self._std_icon(QStyle.StandardPixmap.SP_ComputerIcon))
        ssh.setIcon(self._std_icon(QStyle.StandardPixmap.SP_DriveNetIcon))
        saved.setIcon(self._std_icon(QStyle.StandardPixmap.SP_DirHomeIcon))
        local.clicked.connect(self.open_local_terminal)
        ssh.clicked.connect(self.quick_ssh)
        saved.clicked.connect(lambda: self.left_tabs.setCurrentIndex(0))
        centre = QVBoxLayout()
        centre.addWidget(title); centre.addWidget(subtitle); centre.addSpacing(18); centre.addWidget(local); centre.addWidget(ssh); centre.addWidget(saved)
        holder = QWidget(); holder.setLayout(centre)
        outer.addWidget(holder, 0, Qt.AlignmentFlag.AlignCenter)
        outer.addStretch(2)
        return page

    def _std_icon(self, pixmap):
        return self.style().standardIcon(pixmap)

    def _status_button(self, text, kind):
        button = QPushButton(text)
        button.setObjectName(f"Status_{kind}")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        icon_map = {
            "cpu": QStyle.StandardPixmap.SP_ComputerIcon,
            "ram": QStyle.StandardPixmap.SP_FileDialogContentsView,
            "disk": QStyle.StandardPixmap.SP_DriveHDIcon,
            "network": QStyle.StandardPixmap.SP_DriveNetIcon,
            "usb": QStyle.StandardPixmap.SP_DriveFDIcon,
        }
        button.setIcon(self._std_icon(icon_map[kind]))
        button.setToolTip(f"Show detailed {kind} information")
        button.clicked.connect(lambda: self.show_system_info(kind))
        return button

    def _apply_theme(self):
        self.setStyleSheet(r"""
            QMainWindow { background:#1b252b; }
            QMenuBar { background:#202d35; color:#dce3ea; border-bottom:1px solid #526a76; }
            QMenuBar::item:selected { background:#273342; }
            QMenu { background:#25323a; color:#e2e8ef; border:1px solid #5a707b; }
            QMenu::item:selected { background:#3b6074; }
            QLineEdit, QComboBox, QSpinBox { background:#182329; color:#e5ebf2; border:1px solid #5a707b; border-radius:4px; padding:4px; }
            QTreeView, QTreeWidget { background:#18262b; alternate-background-color:#1d3030; color:#dce3ea; border:1px solid #526a76; }
            QTabWidget::pane { border:1px solid #526a76; }
            QTabWidget#TerminalPane[activePane="true"]::pane { border:2px solid #6faed6; }
            QTabWidget#TerminalPane[activePane="false"]::pane { border:1px solid #3e4d59; }
            QTabBar::tab { background:transparent; color:transparent; padding:6px 12px; border:0; }
            QTabBar::tab:selected { background:transparent; color:transparent; }
            QPushButton { color:#edf3f8; border:1px solid #526171; border-radius:5px; padding:5px 10px;
                background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #344454, stop:1 #222d38); }
            QPushButton:hover { border-color:#7ba7cf; background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #41617d, stop:1 #2a4054); }
            QPushButton:pressed { background:#1d2b38; }
            QPushButton:disabled { color:#6d7782; border-color:#313a43; background:#1b2026; }
            QStatusBar { background:#1b292d; color:#dce3ea; border-top:1px solid #526a76; }
            QStatusBar::item { border:0px; }
            QProgressBar#TransferProgress { color:#edf3f8; border:1px solid #5b7480; border-radius:4px; background:#18262b; text-align:center; }
            QProgressBar#TransferProgress::chunk { background:#3e8668; border-radius:3px; }
            QProgressBar#ConnectionProgress { border:1px solid #5b7480; border-radius:3px; background:#18262b; }
            QProgressBar#ConnectionProgress::chunk { background:#4f91bd; border-radius:2px; }
            QPushButton#Status_cpu { background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #274c68, stop:1 #183144); border-color:#3b6c91; }
            QPushButton#Status_ram { background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #285942, stop:1 #193b2c); border-color:#3d7c5e; }
            QPushButton#Status_disk { background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #645023, stop:1 #403317); border-color:#8a6f34; }
            QPushButton#Status_network { background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #4d3767, stop:1 #302342); border-color:#725292; }
            QPushButton#Status_usb { background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #653438, stop:1 #422124); border-color:#8a4b50; }
            QPushButton#Status_cpu:hover, QPushButton#Status_ram:hover, QPushButton#Status_disk:hover,
            QPushButton#Status_network:hover, QPushButton#Status_usb:hover { border-color:#a9c9e5; }
            QToolBar#FilePaneToolbar { spacing:3px; padding:3px 3px 6px 3px; border:1px solid #526a76; border-radius:4px; background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #21343a, stop:1 #29312d); }
            QToolBar#FilePaneToolbar QToolButton { color:#e6edf4; border:1px solid #465563; border-radius:4px;
                min-height:25px; max-height:25px; padding:2px 7px;
                background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #2d3c49, stop:1 #202a33); }
            QToolBar#FilePaneToolbar QToolButton:hover { border-color:#78a9d1; background:#314b61; }
            QLabel#FileSelectionLabel { color:#9eabb8; padding:0 4px; font-size:10px; }
            QPushButton#PaneActionButton {
                color:#edf3f8; border:1px solid #536271; border-radius:4px; padding:3px 7px;
                background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #354453, stop:1 #222c36);
            }
            QPushButton#PaneActionButton:hover {
                border-color:#8db8dc;
                background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #46627a, stop:1 #2d4051);
            }
            QPushButton#PaneActionButton:pressed { background:#1d2730; }
            QPushButton#PaneActionButton:disabled { color:#68737d; border-color:#303841; background:#1a2026; }
            QToolBar#MainRibbon { spacing:0px; padding:3px 5px; border-bottom:1px solid #526a76;
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #20394a, stop:.50 #244338, stop:1 #3b2d46); }
            QFrame#RibbonGroup { border:1px solid #526a76; border-radius:5px; margin:1px; background:rgba(30,48,53,220); }
            QFrame#RibbonTransferGroup { border:1px solid #d69a45; border-radius:5px; margin:1px; background:rgba(116,76,25,190); }
            QFrame#RibbonTransferGroup QLabel#RibbonGroupLabel { color:#ffe0a3; }
            QLabel#RibbonGroupLabel { color:#9fb0c0; font-size:9px; font-weight:700; padding:1px 4px 2px 4px; }
            QToolButton { color:#edf3f8; border:1px solid #536271; border-radius:4px; padding:4px 8px;
                background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #354453, stop:1 #222c36); }
            QToolButton:hover { border-color:#8db8dc; background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #46627a, stop:1 #2d4051); }
            QToolButton:disabled { color:#68737d; border-color:#303841; background:#1a2026; }
        """)

    def _make_action(self, text, icon, slot, shortcut=None, tip=""):
        action = QAction(icon, text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.setToolTip(tip or text)
        action.triggered.connect(slot)
        return action

    @staticmethod
    def _terminal_layout_icon(side_by_side_target):
        """Draw the layout produced by pressing the ribbon toggle."""
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#dceaf5"), 1.5))
        painter.setBrush(QColor("#315675"))
        if side_by_side_target:
            painter.drawRoundedRect(QRect(2, 4, 9, 16), 2, 2)
            painter.drawRoundedRect(QRect(13, 4, 9, 16), 2, 2)
        else:
            painter.drawRoundedRect(QRect(4, 3, 16, 18), 2, 2)
        painter.end()
        return QIcon(pixmap)

    def _build_menu(self):
        self.action_local = self._make_action("Local Terminal", self._std_icon(QStyle.StandardPixmap.SP_ComputerIcon), self.open_local_terminal, "Ctrl+Shift+L")
        self.action_ssh = self._make_action("Quick SSH…", self._std_icon(QStyle.StandardPixmap.SP_DriveNetIcon), self.quick_ssh, "Ctrl+Shift+S")
        self.action_hosts = self._make_action("Saved Hosts", self._std_icon(QStyle.StandardPixmap.SP_DirHomeIcon), lambda: self.left_tabs.setCurrentIndex(0))
        self.action_commands = self._make_action("Saved Commands", self._std_icon(QStyle.StandardPixmap.SP_FileDialogListView), lambda: self.left_tabs.setCurrentIndex(2))
        self.action_paste = self._make_action("Paste", self._std_icon(QStyle.StandardPixmap.SP_DialogOpenButton), lambda: self.terminal.paste_to_terminal() if self.terminal else None, "Ctrl+Shift+V")
        self.action_find = self._make_action("Find…", self._std_icon(QStyle.StandardPixmap.SP_FileDialogContentsView), lambda: self.terminal.find_terminal() if self.terminal else None, "Ctrl+F")
        self.action_settings = self._make_action("Appearance & Terminal Settings…", self._std_icon(QStyle.StandardPixmap.SP_FileDialogDetailedView), self.edit_terminal_settings)
        self.action_file_home = self._make_action("Home", self._std_icon(QStyle.StandardPixmap.SP_DirHomeIcon), lambda: self.browser_home(self.mode == "ssh"))
        self.action_file_up = self._make_action("Up", self._std_icon(QStyle.StandardPixmap.SP_ArrowUp), self.browser_up)
        self.action_refresh = self._make_action("Refresh", self._std_icon(QStyle.StandardPixmap.SP_BrowserReload), lambda: self.browser_refresh(self.mode == "ssh"), "F5")
        self.action_show_files = self._make_action("Show Files", self._std_icon(QStyle.StandardPixmap.SP_DirIcon), lambda: self.left_tabs.setCurrentIndex(1))
        self.action_deletion_log = self._make_action("Deletion Log", self._std_icon(QStyle.StandardPixmap.SP_FileDialogInfoView), self.show_deletion_log, tip="Show files and folders deleted through PowerTerm")
        self.action_side_by_side = self._make_action("Side by Side\nSplit", self._terminal_layout_icon(True), self.toggle_side_by_side, "Ctrl+Alt+Right", "Toggle side-by-side terminal panes")
        self.action_side_by_side.setCheckable(True)
        self.action_about = self._make_action("About and\nUpdates", self._std_icon(QStyle.StandardPixmap.SP_MessageBoxInformation), self.show_about, tip="Show the PowerTerm version and check GitHub releases")

        connection = self.menuBar().addMenu("&Connection")
        connection.addActions([self.action_local, self.action_ssh])
        connection.addSeparator()
        connection.addAction(self.action_hosts)

        terminal_menu = self.menuBar().addMenu("&Terminal")
        terminal_menu.addAction(self.action_paste)
        terminal_menu.addAction(self.action_find)
        terminal_menu.addSeparator()
        terminal_menu.addAction(self.action_settings)

        file_menu = self.menuBar().addMenu("&Files")
        file_menu.addAction(self.action_file_home)
        file_menu.addAction(self.action_file_up)
        file_menu.addAction(self.action_refresh)
        file_menu.addAction(self.action_deletion_log)
        self.transfer_menu = file_menu.addMenu("Transfer")
        self.transfer_menu.setIcon(self._std_icon(QStyle.StandardPixmap.SP_ArrowUp))
        self.transfer_upload_files = self.transfer_menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_ArrowUp), "Upload Files…")
        self.transfer_upload_files.triggered.connect(self.upload_files_to_current_remote)
        self.transfer_upload_folder = self.transfer_menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_DirIcon), "Upload Folder…")
        self.transfer_upload_folder.triggered.connect(self.upload_folder_to_current_remote)
        self.transfer_menu.addSeparator()
        self.transfer_download = self.transfer_menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_ArrowDown), "Download Selected…")
        self.transfer_download.triggered.connect(self.download_selected_remote)

        view = self.menuBar().addMenu("&View")
        view.addAction(self.action_show_files)
        view.addAction(self.action_hosts)
        view.addAction(self.action_commands)
        view.addSeparator()
        view.addAction(self.action_side_by_side)

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(self.action_about)

    @staticmethod
    def _version_key(version):
        """Return comparable numeric release fields, or None for an unknown tag."""
        text = str(version or "").strip().lstrip("vV")
        match = re.match(r"^(\d+(?:\.\d+)*)", text)
        if match is None:
            return None
        return tuple(int(part) for part in match.group(1).split("."))

    @classmethod
    def _release_is_newer(cls, release_version):
        latest, current = cls._version_key(release_version), cls._version_key(APP_VERSION)
        if latest is None or current is None:
            return False
        width = max(len(latest), len(current))
        return latest + (0,) * (width - len(latest)) > current + (0,) * (width - len(current))

    def show_about(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("About PowerTerm")
        dialog.setModal(True)
        dialog.setMinimumWidth(430)
        layout = QVBoxLayout(dialog)
        title = QLabel("PowerTerm")
        font = title.font()
        font.setPointSize(font.pointSize() + 5)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        layout.addWidget(QLabel(f"Version {APP_VERSION}"))
        layout.addWidget(QLabel("PowerTerm is licensed under the GNU General Public License, version 3."))

        update_box = QGroupBox("GitHub releases")
        updates = QVBoxLayout(update_box)
        self._about_update_label = QLabel("Checking for updates…")
        self._about_update_label.setWordWrap(True)
        updates.addWidget(self._about_update_label)
        self._about_update_button = QPushButton("Check Again")
        self._about_update_button.clicked.connect(self.check_for_updates)
        updates.addWidget(self._about_update_button, 0, Qt.AlignmentFlag.AlignLeft)
        releases_button = QPushButton("Open GitHub Releases")
        releases_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(GITHUB_RELEASES_URL))
        )
        updates.addWidget(releases_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(update_box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        self._about_dialog = dialog
        dialog.finished.connect(self._close_about_dialog)
        self.check_for_updates()
        dialog.exec()

    def _close_about_dialog(self, *_args):
        self._about_dialog = None
        self._about_update_label = None
        self._about_update_button = None

    def check_for_updates(self):
        if self._update_check_in_progress:
            return
        self._update_check_in_progress = True
        if self._about_update_label is not None:
            self._about_update_label.setText("Checking the latest GitHub release…")
        if self._about_update_button is not None:
            self._about_update_button.setEnabled(False)
        threading.Thread(target=self._fetch_latest_release, daemon=True).start()

    def _fetch_latest_release(self):
        try:
            request = Request(
                GITHUB_LATEST_RELEASE_URL,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "PowerTerm-update-check"},
            )
            with urlopen(request, timeout=5) as response:
                release = json.load(response)
            tag = str(release.get("tag_name") or release.get("name") or "").strip()
            if not tag:
                raise ValueError("GitHub did not provide a release version.")
            self.update_check_finished.emit({"release": tag, "url": release.get("html_url", "")})
        except (OSError, URLError, ValueError, json.JSONDecodeError) as error:
            self.update_check_finished.emit({"error": str(error) or "The update check failed."})

    def _apply_update_check_result(self, result):
        self._update_check_in_progress = False
        if self._about_update_button is not None:
            self._about_update_button.setEnabled(True)
        if self._about_update_label is None:
            return
        if result.get("error"):
            self._about_update_label.setText(
                "Could not check GitHub releases. " + result["error"]
            )
            return
        release = result["release"]
        if self._release_is_newer(release):
            self._about_update_label.setText(
                f"Update available: {release}. Visit GitHub Releases to download it."
            )
        else:
            self._about_update_label.setText(f"You are up to date. Latest GitHub release: {release}.")

    def _build_file_toolbars(self):
        """File-specific controls live with the file browser, not in the app ribbon."""
        def add_action_button(toolbar, action):
            button = QToolButton(toolbar)
            button.setDefaultAction(action)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setAutoRaise(False)
            toolbar.addWidget(button)
            return button

        for toolbar in (self.local_file_toolbar, self.remote_file_toolbar):
            toolbar.clear()
            add_action_button(toolbar, self.action_file_home)
            add_action_button(toolbar, self.action_refresh)

        self.transfer_button = QToolButton(self.remote_file_toolbar)
        self.transfer_button.setText("Transfer")
        self.transfer_button.setIcon(self._std_icon(QStyle.StandardPixmap.SP_ArrowUp))
        self.transfer_button.setToolTip("Upload or download files for this SSH session")
        self.transfer_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.transfer_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.transfer_button.setMenu(self.transfer_menu)
        self.transfer_menu.aboutToShow.connect(self.update_transfer_menu)
        self.remote_file_toolbar.addSeparator()
        self.remote_file_toolbar.addWidget(self.transfer_button)


    def _build_ribbon(self):
        # App-level ribbon. Context-sensitive transfers remain in the remote
        # browser toolbar; persistent file activity belongs in the ribbon.
        ribbon = QToolBar("Main Ribbon", self)
        ribbon.setObjectName("MainRibbon")
        ribbon.setMovable(False)
        ribbon.setFloatable(False)
        ribbon.setMinimumHeight(86)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, ribbon)

        def action_button(action):
            button = QToolButton()
            button.setDefaultAction(action)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setAutoRaise(False)
            # One-line actions should not inherit the extra vertical space
            # needed by the two-line Appearance button.
            lines = max(1, button.text().count("\n") + 1)
            button.setMinimumHeight(48 if lines == 1 else 62)
            button.setMaximumHeight(48 if lines == 1 else 62)
            button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            return button

        def group(title, buttons):
            frame = QFrame()
            frame.setObjectName("RibbonGroup")
            # Groups should fill the ribbon's available height even when they
            # contain only short one-line buttons.
            frame.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Expanding)
            layout = QVBoxLayout(frame)
            layout.setContentsMargins(3, 2, 3, 1)
            layout.setSpacing(1)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            row.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            for button in buttons:
                row.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
            layout.addLayout(row, 1)
            label = QLabel(title.upper())
            label.setObjectName("RibbonGroupLabel")
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            layout.addWidget(label)
            return frame

        connection_buttons = [
            action_button(self.action_local),
            action_button(self.action_ssh),
            action_button(self.action_hosts),
        ]
        settings_button = action_button(self.action_settings)
        settings_button.setText("Appearance and\nTerminal Settings")
        settings_button.setMinimumHeight(62)
        settings_button.setMaximumHeight(62)
        terminal_buttons = [
            action_button(self.action_paste),
            action_button(self.action_find),
            action_button(self.action_side_by_side),
            settings_button,
        ]

        ribbon.addWidget(group("Connection", connection_buttons))
        self.ribbon_terminal_group = group("Terminal", terminal_buttons)
        ribbon.addWidget(self.ribbon_terminal_group)
        self.ribbon_file_activity_group = group(
            "File Activity",
            [action_button(self.action_deletion_log)],
        )
        ribbon.addWidget(self.ribbon_file_activity_group)
        ribbon.addWidget(group("Help", [action_button(self.action_about)]))
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        ribbon.addWidget(spacer)

        self.main_ribbon = ribbon
        self.menuBar().setVisible(False)
        self.ribbon_paste = self.action_paste
        self.ribbon_find = self.action_find
        self.ribbon_side_by_side = self.action_side_by_side

    def _update_ribbon_state(self):
        active = self.terminal is not None
        for action in (getattr(self, "ribbon_paste", None), getattr(self, "ribbon_find", None),
                       getattr(self, "action_file_home", None), getattr(self, "action_file_up", None),
                       getattr(self, "action_refresh", None)):
            if action is not None:
                action.setEnabled(active)
        if hasattr(self, "transfer_button"):
            ssh_ready = self.mode == "ssh" and self.remote_backend is not None and self.remote_backend.client is not None
            self.transfer_button.setEnabled(ssh_ready)
            self.transfer_upload_files.setEnabled(ssh_ready)
            self.transfer_upload_folder.setEnabled(ssh_ready)
            self.transfer_download.setEnabled(ssh_ready and bool(self.remote_tree.selected_path()) and not self.remote_tree.selected_is_dir())

    def update_file_selection_display(self, *_args):
        if not hasattr(self, "file_selection_labels"): return
        index = self.local_tree.currentIndex()
        local_path = self.local_model.filePath(index) if index.isValid() else ""
        remote_path = self.remote_tree.selected_path() or ""
        self.file_selection_labels[False].setText(f"Selection: {local_path or '(none)'}")
        self.file_selection_labels[True].setText(f"Selection: {remote_path or '(none)'}")
        self._update_ribbon_state()

    def update_transfer_menu(self):
        remote_dir = self.remote_tree.current_directory() or "/"
        selected = self.remote_tree.selected_path() or ""
        self.transfer_upload_files.setText(f"Upload Files to {remote_dir}…")
        self.transfer_upload_folder.setText(f"Upload Folder to {remote_dir}…")
        self.transfer_download.setText(f"Download {selected}…" if selected else "Download selected file…")

    def _command_groups(self):
        groups = self.host_store.command_groups()

        # Schema migration: older builds had only top-level groups.
        changed = False
        def normalise(group):
            nonlocal changed
            if "id" not in group:
                group["id"] = str(uuid.uuid4()); changed = True
            if "groups" not in group:
                group["groups"] = []; changed = True
            if "commands" not in group:
                group["commands"] = []; changed = True
            for child in group.get("groups", []):
                normalise(child)
        for group in groups:
            normalise(group)
        if changed:
            self.host_store.save_command_groups(groups)
        return groups

    def _find_group(self, group_id, groups=None, parent_groups=None):
        groups = self._command_groups() if groups is None else groups
        for index, group in enumerate(groups):
            if group.get("id") == group_id:
                return group, groups, index, parent_groups
            found = self._find_group(group_id, group.get("groups", []), groups)
            if found:
                return found
        return None

    def _find_command(self, command_id, groups=None):
        groups = self._command_groups() if groups is None else groups
        for group in groups:
            for index, command in enumerate(group.get("commands", [])):
                if command.get("id") == command_id:
                    return command, group, index
            found = self._find_command(command_id, group.get("groups", []))
            if found:
                return found
        return None

    def reload_saved_commands_ui(self):
        self.command_tree.clear()

        def add_group_item(group, parent_item=None):
            gitem = QTreeWidgetItem([group.get("name", "Group")])
            gitem.setData(0, Qt.ItemDataRole.UserRole, ("group", group.get("id")))
            font = gitem.font(0); font.setBold(True); gitem.setFont(0, font)
            icon = self._std_icon(QStyle.StandardPixmap.SP_DirIcon)
            gitem.setIcon(0, icon)
            if parent_item is None:
                self.command_tree.addTopLevelItem(gitem)
            else:
                parent_item.addChild(gitem)

            for child_group in group.get("groups", []):
                add_group_item(child_group, gitem)

            for command in group.get("commands", []):
                item = QTreeWidgetItem([command.get("name", command.get("command", "Command"))])
                item.setData(0, Qt.ItemDataRole.UserRole, ("command", command.get("id"), group.get("id")))
                item.setToolTip(0, command.get("command", ""))
                item.setIcon(0, self._std_icon(QStyle.StandardPixmap.SP_ArrowRight))
                gitem.addChild(item)
            gitem.setExpanded(True)

        for group in self._command_groups():
            add_group_item(group)

    def _selected_command_location(self, item=None):
        item = item or self.command_tree.currentItem()
        if not item:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        return data if isinstance(data, tuple) else None

    def _selected_group_id_for_insertion(self):
        loc = self._selected_command_location()
        if not loc:
            return None
        if loc[0] == "group":
            return loc[1]
        if loc[0] == "command":
            return loc[2]
        return None

    def add_command_group(self):
        parent_id = self._selected_group_id_for_insertion()
        prompt = "Subgroup name:" if parent_id else "Group name:"
        name, ok = QInputDialog.getText(self, "New Command Group", prompt)
        if not ok or not name.strip():
            return
        groups = self._command_groups()
        value = {"id": str(uuid.uuid4()), "name": name.strip(), "groups": [], "commands": []}
        if parent_id:
            found = self._find_group(parent_id, groups)
            if found:
                found[0].setdefault("groups", []).append(value)
        else:
            groups.append(value)
        self.host_store.save_command_groups(groups)
        self.reload_saved_commands_ui()

    def add_saved_command(self):
        group_id = self._selected_group_id_for_insertion()
        if not group_id:
            QMessageBox.information(self, "Saved Commands", "Select a command group first.")
            return
        dlg = SavedCommandDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        value = dlg.value()
        if not value["name"] or not value["command"]:
            return
        groups = self._command_groups()
        found = self._find_group(group_id, groups)
        if found:
            found[0].setdefault("commands", []).append(value)
            self.host_store.save_command_groups(groups)
            self.reload_saved_commands_ui()

    def edit_saved_command(self):
        loc = self._selected_command_location()
        if not loc:
            return
        groups = self._command_groups()
        if loc[0] == "group":
            found = self._find_group(loc[1], groups)
            if not found:
                return
            group = found[0]
            name, ok = QInputDialog.getText(self, "Rename Group", "Group name:", text=group.get("name", ""))
            if ok and name.strip():
                group["name"] = name.strip()
        else:
            found = self._find_command(loc[1], groups)
            if not found:
                return
            old, _group, _index = found
            dlg = SavedCommandDialog(old, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            value = dlg.value()
            value["id"] = old.get("id", value["id"])
            old.clear(); old.update(value)
        self.host_store.save_command_groups(groups)
        self.reload_saved_commands_ui()

    def remove_saved_command(self):
        loc = self._selected_command_location()
        if not loc:
            return
        groups = self._command_groups()
        if loc[0] == "group":
            found = self._find_group(loc[1], groups)
            if not found:
                return
            group, siblings, index, _parent = found
            name = group.get("name", "group")
            suffix = "\n\nThis also removes all subgroups and commands inside it." if (group.get("groups") or group.get("commands")) else ""
            if QMessageBox.question(self, "Remove", f"Remove {name}?{suffix}") != QMessageBox.StandardButton.Yes:
                return
            siblings.pop(index)
        else:
            found = self._find_command(loc[1], groups)
            if not found:
                return
            command, group, index = found
            if QMessageBox.question(self, "Remove", f"Remove {command.get('name','command')}?") != QMessageBox.StandardButton.Yes:
                return
            group.get("commands", []).pop(index)
        self.host_store.save_command_groups(groups)
        self.reload_saved_commands_ui()

    def move_saved_command(self, delta):
        loc = self._selected_command_location()
        if not loc:
            return
        groups = self._command_groups()
        if loc[0] == "group":
            found = self._find_group(loc[1], groups)
            if not found:
                return
            _group, siblings, index, _parent = found
            new_index = index + delta
            if 0 <= new_index < len(siblings):
                siblings[index], siblings[new_index] = siblings[new_index], siblings[index]
        else:
            found = self._find_command(loc[1], groups)
            if not found:
                return
            _command, group, index = found
            commands = group.get("commands", [])
            new_index = index + delta
            if 0 <= new_index < len(commands):
                commands[index], commands[new_index] = commands[new_index], commands[index]
        self.host_store.save_command_groups(groups)
        self.reload_saved_commands_ui()

    def sort_command_groups(self):
        groups = self._command_groups()
        loc = self._selected_command_location()
        target = groups
        if loc:
            group_id = loc[1] if loc[0] == "group" else loc[2]
            found = self._find_group(group_id, groups)
            if found:
                target = found[0].setdefault("groups", [])
        target.sort(key=lambda g: g.get("name", "").casefold())
        self.host_store.save_command_groups(groups)
        self.reload_saved_commands_ui()

    def sort_commands_in_group(self):
        group_id = self._selected_group_id_for_insertion()
        if not group_id:
            return
        groups = self._command_groups()
        found = self._find_group(group_id, groups)
        if found:
            found[0].setdefault("commands", []).sort(key=lambda c: c.get("name", "").casefold())
            self.host_store.save_command_groups(groups)
            self.reload_saved_commands_ui()

    def duplicate_saved_command(self):
        loc = self._selected_command_location()
        if not loc or loc[0] != "command":
            return
        groups = self._command_groups()
        found = self._find_command(loc[1], groups)
        if not found:
            return
        source, group, index = found
        clone = dict(source)
        clone["id"] = str(uuid.uuid4())
        clone["name"] = clone.get("name", "Command") + " Copy"
        group.setdefault("commands", []).insert(index + 1, clone)
        self.host_store.save_command_groups(groups)
        self.reload_saved_commands_ui()

    def run_saved_command(self, item=None):
        loc = self._selected_command_location(item)
        if not loc or loc[0] != "command":
            return
        if not self.terminal or not self.terminal.backend:
            QMessageBox.information(self, "Saved Commands", "Open or select a terminal session first.")
            return
        groups = self._command_groups()
        found = self._find_command(loc[1], groups)
        if not found:
            return
        command = found[0]
        if command.get("confirm"):
            if QMessageBox.question(
                self, "Run Command",
                f"Run {command.get('name','this command')}?\n\n{command.get('command','')}"
            ) != QMessageBox.StandardButton.Yes:
                return
        text = command.get("command", "")
        if not text:
            return

        # Elevation credentials deliberately stay inside the terminal/PTY.
        # sudo/su/ssh/UAC-compatible tools therefore use their normal secure prompt.
        likely_prompt = command.get("may_prompt", False) or bool(
            re.search(r"(^|[;&|]\s*|\s)(sudo|su)\b", text, re.IGNORECASE)
        )
        if likely_prompt:
            self.statusBar().showMessage(
                "If credentials are requested, enter them in the terminal. "
                "PowerTerm does not store or inject sudo/elevation passwords.", 7000
            )
        if getattr(self.terminal, "_history_scrolled", False):
            self.terminal._return_to_live()
        self.terminal.setFocus()
        self.terminal.backend.write(text + ("\r" if not text.endswith("\r") else ""))

    def saved_command_context_menu(self, pos):
        item = self.command_tree.itemAt(pos)
        if item:
            self.command_tree.setCurrentItem(item)
        menu = QMenu(self)
        loc = self._selected_command_location(item)
        if loc and loc[0] == "command":
            menu.addAction("Run", lambda: self.run_saved_command(item))
            menu.addAction("Duplicate", self.duplicate_saved_command)
            menu.addSeparator()
        if loc and loc[0] == "group":
            menu.addAction("Add Subgroup", self.add_command_group)
        else:
            menu.addAction("Add Group", self.add_command_group)
        if loc:
            menu.addAction("Add Command", self.add_saved_command)
            menu.addAction("Edit", self.edit_saved_command)
            menu.addAction("Remove", self.remove_saved_command)
        menu.addSeparator()
        menu.addAction("Sort Groups A–Z", self.sort_command_groups)
        if loc:
            menu.addAction("Sort Commands in Group A–Z", self.sort_commands_in_group)
        menu.exec(self.command_tree.viewport().mapToGlobal(pos))

    def show_session_home(self):
        self.canvas.setCurrentWidget(self.home_page)

    def _create_session_pane(self):
        pane = QTabWidget()
        pane.setObjectName("TerminalPane")
        pane.setTabBar(TrapezoidTabBar(pane, shift_text_left=True))
        pane.setTabsClosable(True)
        pane.setMovable(True)
        pane.currentChanged.connect(lambda index, p=pane: self._pane_current_changed(p, index))
        pane.tabBarClicked.connect(lambda index, p=pane: self._pane_tab_clicked(p, index))
        pane.tabCloseRequested.connect(lambda index, p=pane: self.close_session_tab(index, p))
        pane.tabBar().split_requested.connect(lambda index, p=pane: self._split_tab_right(p, index))
        pane.tabBar().single_pane_requested.connect(self.return_to_single_terminal_pane)
        self.session_panes.append(pane)
        return pane

    def _pane_current_changed(self, pane, index):
        if pane is self._active_session_pane:
            self.session_tab_changed(index, pane)

    def _pane_tab_clicked(self, pane, index):
        if pane is self._active_session_pane and pane.widget(index) is self.terminal:
            self._focus_terminal(self.terminal)
            return
        self._set_active_session_pane(pane)
        self.session_tab_changed(index, pane)

    def _set_active_session_pane(self, pane):
        if pane not in self.session_panes:
            return
        self._active_session_pane = pane
        for candidate in self.session_panes:
            active = candidate is pane
            candidate.setProperty("activePane", active)
            candidate.tabBar().pane_active = active
            candidate.tabBar().update()
            candidate.style().unpolish(candidate)
            candidate.style().polish(candidate)

    def _pane_for_terminal(self, terminal):
        for pane in self.session_panes:
            if pane.indexOf(terminal) >= 0:
                return pane
        return None

    def _all_terminals(self):
        for pane in self.session_panes:
            for index in range(pane.count()):
                terminal = pane.widget(index)
                if isinstance(terminal, TerminalWidget):
                    yield terminal

    def _total_session_count(self):
        return sum(pane.count() for pane in self.session_panes)

    def _activate_terminal_from_focus(self, terminal):
        pane = self._pane_for_terminal(terminal)
        if pane is None:
            return
        if terminal is self.terminal and pane is self._active_session_pane:
            self._focused_terminal = terminal
            self._set_active_session_pane(pane)
            return
        self._focused_terminal = terminal
        self._set_active_session_pane(pane)
        if pane.currentWidget() is not terminal:
            pane.setCurrentWidget(terminal)
        self.session_tab_changed(pane.currentIndex(), pane, focus_terminal=False)

    def active_session(self):
        pane = self._active_session_pane or self.session_tabs
        w = pane.currentWidget()
        return w if isinstance(w, TerminalWidget) else None

    def split_active_terminal_right(self):
        terminal = self.active_session()
        source = self._pane_for_terminal(terminal) if terminal else None
        if source is not None:
            return self._split_tab_right(source, source.indexOf(terminal))
        return False

    def toggle_side_by_side(self, checked):
        if checked:
            if not self.split_active_terminal_right():
                self._sync_side_by_side_action()
        else:
            self.return_to_single_terminal_pane()

    def _sync_side_by_side_action(self):
        action = getattr(self, "action_side_by_side", None)
        if action is None:
            return
        is_split = len(self.session_panes) > 1
        blocked = action.blockSignals(True)
        action.setChecked(is_split)
        action.setIcon(self._terminal_layout_icon(not is_split))
        action.setText("Side by Side\nMerge" if is_split else "Side by Side\nSplit")
        action.setToolTip(
            "Return all terminal tabs to one pane"
            if is_split else
            "Show the active terminal in a side-by-side pane"
        )
        action.blockSignals(blocked)

    def _split_tab_right(self, source, index):
        terminal = source.widget(index) if source and index >= 0 else None
        if terminal is None or source is None:
            return False
        if source.count() < 2 and len(self.session_panes) == 1:
            QMessageBox.information(self, "Split Terminal", "Open at least two terminal tabs before creating a split view.")
            self._sync_side_by_side_action()
            return False
        if len(self.session_panes) >= 2:
            destination = next(p for p in self.session_panes if p is not source)
        else:
            destination = self._create_session_pane()
            self.session_splitter.addWidget(destination)
            self.session_splitter.setSizes([1, 1])
        title = source.tabText(index)
        icon = source.tabIcon(index)
        source_was_blocked = source.blockSignals(True)
        source.removeTab(index)
        source.blockSignals(source_was_blocked)
        destination.setCurrentIndex(destination.addTab(terminal, icon, title))
        self._set_active_session_pane(destination)
        self._sync_side_by_side_action()
        self.canvas.setCurrentWidget(self.session_splitter)
        self._focus_terminal(terminal)
        return True

    def return_to_single_terminal_pane(self):
        primary = self.session_tabs
        for pane in list(self.session_panes):
            if pane is primary:
                continue
            while pane.count():
                terminal = pane.widget(0)
                title = pane.tabText(0)
                icon = pane.tabIcon(0)
                pane.removeTab(0)
                primary.addTab(terminal, icon, title)
            pane.setParent(None)
            self.session_panes.remove(pane)
            pane.deleteLater()
        self._set_active_session_pane(primary)
        self._sync_side_by_side_action()
        if primary.count():
            primary.setCurrentWidget(self._focused_terminal if primary.indexOf(self._focused_terminal) >= 0 else primary.currentWidget())
            self._focus_terminal(primary.currentWidget())

    def _new_terminal_widget(self, mode, title, backend, profile=None):
        terminal=TerminalWidget(); terminal.apply_settings(self.terminal_settings.get("cursor_style","ibeam"), self.terminal_settings.get("cursor_blink",True), self.terminal_settings.get("font_family","Monospace"), self.terminal_settings.get("font_size",11), self.terminal_settings.get("scrollback_limit",10000), self.terminal_settings.get("syntax_highlighting",True))
        terminal.mode=mode; terminal.remote_backend=backend if mode.startswith("ssh") else None; terminal.profile=profile; terminal.remote_path=(profile or {}).get("start_dir",""); terminal.stats_busy=False; terminal.remote_prev_cpu=None; terminal.remote_prev_net=None; terminal.remote_stats_not_before=0.0
        terminal.cwd_changed.connect(lambda path, t=terminal: self.on_session_cwd_changed(t,path))
        terminal.session_closed.connect(lambda t=terminal: self.on_terminal_session_closed(t))
        terminal.close_requested.connect(lambda t=terminal: self.close_terminal_widget(t))
        terminal.restart_requested.connect(lambda t=terminal: self.restart_terminal_widget(t))
        terminal.save_output_requested.connect(lambda t=terminal: self.save_terminal_output(t))
        terminal.focus_activated.connect(lambda t=terminal: self._activate_terminal_from_focus(t))
        pane = self._active_session_pane or self.session_tabs
        idx=pane.addTab(terminal,title); pane.setCurrentIndex(idx); self._set_active_session_pane(pane); self.canvas.setCurrentWidget(self.session_splitter)
        terminal.set_backend(backend)
        # The tab is now part of its final layout, so an SSH PTY can be
        # created at the real dimensions instead of the backend's fallback.
        terminal._apply_terminal_resize()
        self._focus_terminal(terminal)
        return terminal

    def close_terminal_widget(self, terminal):
        pane = self._pane_for_terminal(terminal)
        index = pane.indexOf(terminal) if pane else -1
        if index >= 0:
            self.close_session_tab(index, pane)

    def restart_terminal_widget(self, terminal):
        pane = self._pane_for_terminal(terminal)
        index = pane.indexOf(terminal) if pane else -1
        if index < 0:
            return
        profile = getattr(terminal, "profile", None)
        old_backend = getattr(terminal, "backend", None)
        if profile:
            password = getattr(old_backend, "password", "") if old_backend else ""
            backend = SshBackend(profile["host"], int(profile.get("port", 22)), profile["username"], password, self, autostart=False)
            terminal.mode = "ssh-connecting"
            terminal._connection_restarting = True
            terminal.remote_backend = backend
            terminal.set_backend(backend, reset=False)
            backend.connected.connect(lambda t=terminal, b=backend, p=profile: self.ssh_connected(t, b, p))
            backend.shell_ready.connect(lambda t=terminal, b=backend, p=profile: self.ssh_shell_ready(t, b, p))
            backend.error.connect(lambda m, t=terminal: self._ssh_connection_failed(t, m))
            self._show_connection_progress(terminal, profile, restarting=True)
            terminal._apply_terminal_resize()
            backend.start()
        else:
            backend = WindowsPtyBackend(
                self,
                self.terminal_settings.get("windows_powershell_predictions", True),
                self.terminal_settings.get("follow_terminal_in_file_browser", False),
            ) if os.name == "nt" else UnixPtyBackend(self)
            terminal.mode = "local"
            terminal.remote_backend = None
            terminal.set_backend(backend, reset=False)
        title = pane.tabText(index).removesuffix(" (closed)")
        pane.setTabText(index, title)
        pane.setCurrentIndex(index)
        self.session_tab_changed(index, pane)

    def save_terminal_output(self, terminal):
        path, _ = QFileDialog.getSaveFileName(self, "Save terminal output", "terminal-output.txt", "Text files (*.txt);;All files (*)")
        if not path:
            return
        try:
            output = strip_ansi("".join(terminal._replay_chunks))
            Path(path).write_text(output, encoding="utf-8")
            self.statusBar().showMessage(f"Terminal output saved to {path}", 6000)
        except Exception as exc:
            QMessageBox.warning(self, "Save Terminal Output", str(exc))

    def on_terminal_session_closed(self, terminal):
        pane = self._pane_for_terminal(terminal)
        index = pane.indexOf(terminal) if pane else -1
        if index >= 0:
            title = pane.tabText(index)
            if not title.endswith(" (closed)"):
                pane.setTabText(index, title + " (closed)")
        terminal.mode = "closed"
        terminal.remote_backend = None
        if terminal is self.active_session():
            self.connection_progress.hide()
            self.left_tabs.setCurrentIndex(0)
            self.mode = "closed"
            self.remote_backend = None
            self.current_profile = None
            self.source_status.setText("Session closed")
            for widget in (self.cpu_status, self.ram_status, self.disk_status, self.net_status, self.usb_status):
                widget.setEnabled(False)
            self._update_ribbon_state()

    def _focus_terminal(self, terminal=None):
        """Give keyboard focus to the active terminal after tab/layout changes."""
        terminal = terminal or self.active_session()
        if not isinstance(terminal, TerminalWidget):
            return
        terminal.setFocus(Qt.FocusReason.OtherFocusReason)
        # Qt may restore focus to the last path editor during tab activation;
        # win once more after that layout/event pass.
        QTimer.singleShot(0, lambda t=terminal: t.setFocus(Qt.FocusReason.OtherFocusReason) if t is self.active_session() else None)

    def _activate_session_ui(self):
        self.canvas.setCurrentWidget(self.session_splitter)
        for b in (self.cpu_status,self.ram_status,self.disk_status,self.net_status,self.usb_status): b.setEnabled(self.terminal is not None)

    def open_local_terminal(self):
        try:
            backend=WindowsPtyBackend(
                self,
                self.terminal_settings.get("windows_powershell_predictions", True),
                self.terminal_settings.get("follow_terminal_in_file_browser", False),
            ) if os.name=="nt" else UnixPtyBackend(self)
            terminal=self._new_terminal_widget("local","Local",backend)
            pane = self._pane_for_terminal(terminal)
            self.session_tab_changed(pane.indexOf(terminal), pane)
            self.install_cwd_reporting(); self.update_status()
        except Exception as exc: QMessageBox.warning(self,"Local Terminal",str(exc))

    def quick_ssh(self):
        dialog=QuickSshDialog(self)
        if dialog.exec()!=QDialog.DialogCode.Accepted: return
        profile=dialog.profile()
        if not profile["host"] or not profile["username"]: QMessageBox.warning(self,"SSH","Host and username are required."); return
        self.start_ssh(profile,dialog.password.text())

    def start_ssh(self, profile, password=""):
        # The terminal must measure its final viewport before SSH requests a
        # PTY. Starting at 100x30 and resizing afterwards leaves readline's
        # wrapped-history bookkeeping out of step in maximized windows.
        backend=SshBackend(profile["host"],int(profile.get("port",22)),profile["username"],password,self,autostart=False)
        title=profile.get("name") or profile["host"]
        terminal=self._new_terminal_widget("ssh-connecting",title,backend,profile)
        terminal._connection_restarting = False
        backend.connected.connect(lambda t=terminal,b=backend,p=profile: self.ssh_connected(t,b,p))
        backend.shell_ready.connect(lambda t=terminal,b=backend,p=profile: self.ssh_shell_ready(t,b,p))
        backend.error.connect(lambda m,t=terminal: self._ssh_connection_failed(t,m))
        backend.start()
        self._show_connection_progress(terminal, profile)
        pane = self._pane_for_terminal(terminal)
        self.session_tab_changed(pane.indexOf(terminal), pane)

    def _show_connection_progress(self, terminal, profile, restarting=None):
        if terminal is not self.active_session():
            return
        if restarting is None:
            restarting = bool(getattr(terminal, "_connection_restarting", False))
        action = "Reconnecting" if restarting else "Connecting"
        destination = f"{profile.get('username', '')}@{profile.get('host', '')}".strip("@")
        self.source_status.setText(f"{action} to {destination}…")
        self.connection_progress.setToolTip(f"{action} to {destination}")
        self.connection_progress.show()

    def _ssh_connection_failed(self, terminal, message):
        if terminal is self.active_session():
            self.connection_progress.hide()
        self.statusBar().showMessage(message, 8000)

    def ssh_connected(self, terminal, backend, profile):
        terminal.mode="ssh"; terminal.remote_backend=backend
        terminal.remote_stats_not_before = time.monotonic() + 3.0
        terminal._connection_restarting = False
        if self.active_session() is terminal:
            self.mode="ssh"; self.remote_backend=backend; self.current_profile=profile
            self.remote_tree.set_backend(backend, terminal.remote_path)
            self.file_stack.setCurrentWidget(self.remote_file_page); self.left_tabs.setCurrentIndex(1)
            self._reset_status_counters()
            self.source_status.setText(f"Connected — starting shell {backend.username}@{backend.host}…")
        self._update_ribbon_state()
        self.statusBar().showMessage(f"Authenticated with {backend.username}@{backend.host}", 3000)

    def ssh_shell_ready(self, terminal, backend, profile):
        if terminal.backend is not backend:
            return
        if self.active_session() is terminal:
            self.connection_progress.hide()
            self.source_status.setText(f"REMOTE {backend.host}")
            self.install_cwd_reporting()
            # Let the initial directory listing own the transport first.
            QTimer.singleShot(
                1000,
                lambda t=terminal: self.update_status() if t is self.active_session() else None,
            )
        self.statusBar().showMessage(f"Connected to {backend.username}@{backend.host}", 3000)

    def session_tab_changed(self, index, pane=None, focus_terminal=True):
        pane = pane or self._active_session_pane or self.session_tabs
        if pane not in self.session_panes:
            return
        self._set_active_session_pane(pane)
        self.session_generation += 1
        terminal=pane.currentWidget(); terminal = terminal if isinstance(terminal, TerminalWidget) else None
        self._focused_terminal = terminal
        self.terminal=terminal
        if not terminal:
            self.mode="none"; self.remote_backend=None; self.current_profile=None; self.source_status.setText("No active session"); self._update_ribbon_state(); return
        self.mode=terminal.mode; self.remote_backend=getattr(terminal,"remote_backend",None); self.current_profile=getattr(terminal,"profile",None)
        if self.mode=="local":
            self.file_stack.setCurrentWidget(self.local_file_page); self.local_path_label.setText(self.local_model.filePath(self.local_tree.rootIndex()) or str(Path.home())); self.setWindowTitle("PowerTerm — Local")
        else:
            self.file_stack.setCurrentWidget(self.remote_file_page)
            if self.mode=="ssh" and self.remote_backend:
                remembered = getattr(terminal, "remote_path", "") or (self.current_profile or {}).get("start_dir", "")
                self.remote_tree.set_backend(self.remote_backend, remembered)
                self.remote_path_label.setText(self.remote_tree.current_directory() or "/")
            else:
                self.remote_path_label.setText("Connecting…")
                self._show_connection_progress(terminal, self.current_profile or {})
            host=(self.current_profile or {}).get("host",""); self.setWindowTitle(f"PowerTerm — SSH {host}")
        self.left_tabs.setCurrentIndex(1); self._reset_status_counters(); self._activate_session_ui(); self.install_cwd_reporting(); self.update_status(); self._update_ribbon_state()
        if focus_terminal:
            self._focus_terminal(terminal)

    def close_session_tab(self,index, pane=None):
        pane = pane or self._active_session_pane or self.session_tabs
        w=pane.widget(index)
        if isinstance(w,TerminalWidget): w.close_backend()
        pane.removeTab(index); w.deleteLater()
        if pane.count() == 0 and pane is not self.session_tabs and len(self.session_panes) > 1:
            self.session_panes.remove(pane)
            pane.setParent(None)
            pane.deleteLater()
            self._set_active_session_pane(self.session_tabs)
            self._sync_side_by_side_action()
        elif pane.count() == 0 and self._total_session_count() > 0:
            replacement = next(candidate for candidate in self.session_panes if candidate.count())
            self._set_active_session_pane(replacement)
        if self._total_session_count()==0:
            self.terminal=None; self.mode="none"; self.remote_backend=None; self.current_profile=None; self.canvas.setCurrentWidget(self.home_page); self.source_status.setText("No active session")
            self.left_tabs.setCurrentIndex(0)
            for b in (self.cpu_status,self.ram_status,self.disk_status,self.net_status,self.usb_status): b.setEnabled(False)
            self._update_ribbon_state()
        else:
            active = self.active_session()
            if active:
                active_pane = self._pane_for_terminal(active)
                self.session_tab_changed(active_pane.currentIndex(), active_pane)

    def _reset_status_counters(self):
        if self.mode == "local":
            self.last_net = psutil.net_io_counters()
        self.last_net_time = time.monotonic()
        self.remote_prev_cpu = None; self.remote_prev_net = None; self.remote_stats_busy = False

    def reload_hosts_ui(self):
        self.host_tree.clear()
        local=QTreeWidgetItem(["Local","This computer","","—"])
        local.setIcon(0, self._std_icon(QStyle.StandardPixmap.SP_ComputerIcon))
        lf=local.font(0); lf.setBold(True); local.setFont(0,lf)
        local.setData(0,Qt.ItemDataRole.UserRole,"local"); self.host_tree.addTopLevelItem(local)
        for i,profile in enumerate(self.host_profiles):
            item=QTreeWidgetItem([profile.get("name",profile.get("host","")),profile.get("host",""),profile.get("username",""),str(profile.get("port",22))])
            item.setIcon(0, self._std_icon(QStyle.StandardPixmap.SP_DriveNetIcon))
            item.setData(0,Qt.ItemDataRole.UserRole,i); self.host_tree.addTopLevelItem(item)

    def selected_host_index(self):
        item=self.host_tree.currentItem(); return item.data(0,Qt.ItemDataRole.UserRole) if item else None

    def add_host(self):
        dialog=HostProfileDialog(parent=self)
        if dialog.exec()!=QDialog.DialogCode.Accepted:return
        profile=dialog.profile()
        if not profile["host"] or not profile["username"]: QMessageBox.warning(self,"Host Profile","Host and username are required."); return
        self.host_profiles.append(profile); self.host_store.save(self.host_profiles); self.reload_hosts_ui()

    def edit_host(self):
        idx=self.selected_host_index()
        if idx is None:return
        if idx=="local": QMessageBox.information(self,"Local Host","The Local entry is built in and cannot be edited."); return
        idx=int(idx); dialog=HostProfileDialog(self.host_profiles[idx],self)
        if dialog.exec()!=QDialog.DialogCode.Accepted:return
        profile=dialog.profile()
        if not profile["host"] or not profile["username"]: QMessageBox.warning(self,"Host Profile","Host and username are required."); return
        self.host_profiles[idx]=profile; self.host_store.save(self.host_profiles); self.reload_hosts_ui()

    def remove_host(self):
        idx=self.selected_host_index()
        if idx is None:return
        if idx=="local": QMessageBox.information(self,"Local Host","The Local entry is permanent and cannot be removed."); return
        idx=int(idx); profile=self.host_profiles[idx]
        if QMessageBox.question(self,"Remove Host",f"Remove saved host '{profile.get('name',profile.get('host'))}'?")!=QMessageBox.StandardButton.Yes:return
        self.credential_store.delete(profile); del self.host_profiles[idx]; self.host_store.save(self.host_profiles); self.reload_hosts_ui()

    def connect_selected_host(self):
        idx=self.selected_host_index()
        if idx is None:return
        if idx=="local": self.open_local_terminal(); return
        profile=self.host_profiles[int(idx)]; stored=self.credential_store.get(profile)
        if should_auto_connect_saved_password(self.terminal_settings.get("auto_connect_saved_password", False), stored):
            self.start_ssh(profile, stored)
            return
        dialog=PasswordDialog(profile,stored,self)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            password=dialog.password.text()
            if dialog.remember.isChecked() and password:
                try:self.credential_store.set(profile,password)
                except Exception as exc: QMessageBox.warning(self,"Password Storage",f"Could not store the password securely:\n{exc}")
            elif not dialog.remember.isChecked(): self.credential_store.delete(profile)
            self.start_ssh(profile,password)

    def local_file_clicked(self, index):
        # Keep the address bar as the directory currently being browsed.
        current = self.local_model.filePath(self.local_tree.rootIndex()) or str(Path.home())
        self.local_path_label.setText(current)
        self.update_file_selection_display()

    def on_remote_browser_path_changed(self, path):
        self.remote_path_label.setText(path)
        session = self.active_session()
        if session and self.mode == "ssh":
            session.remote_path = path

    def local_file_double_clicked(self, index):
        path = self.local_model.filePath(index)
        if self.mode == "local" and os.path.isdir(path) and self.terminal_settings.get("follow_file_browser_in_terminal", False):
            self.send_cd(path)

    def remote_directory_activated(self, path):
        if self.mode == "ssh" and self.terminal_settings.get("follow_file_browser_in_terminal", False):
            self.send_cd(path)

    def selected_local_path(self):
        index = self.local_tree.currentIndex()
        return self.local_model.filePath(index) if index.isValid() else self.local_model.filePath(self.local_tree.rootIndex())

    def selected_local_paths(self):
        paths = []
        for index in self.local_tree.selectionModel().selectedRows(0):
            path = self.local_model.filePath(index)
            if path:
                paths.append((path, os.path.isdir(path) and not os.path.islink(path)))
        return paths

    def selected_remote_paths(self):
        result = []
        for item in self.remote_tree.selectedItems():
            path = item.data(0, self.remote_tree.PATH_ROLE)
            if path and item.text(0) != "..":
                result.append((path, bool(item.data(0, self.remote_tree.IS_DIR_ROLE))))
        return result

    def selected_local_directory(self):
        path = self.selected_local_path()
        return path if path and os.path.isdir(path) else (os.path.dirname(path) if path else str(Path.home()))

    def terminal_here(self, remote=None):
        if remote is None: remote = self.mode == "ssh"
        if remote:
            if self.mode != "ssh": QMessageBox.information(self, "Terminal Here", "Open an SSH session before using a remote folder."); return
            path = self.remote_tree.current_directory()
        else:
            if self.mode != "local": QMessageBox.information(self, "Terminal Here", "The active terminal is remote. Use the Remote file tab to change its directory."); return
            path = self.selected_local_directory()
        if path: self.send_cd(path)

    def send_cd(self, path):
        if not self.terminal or not self.terminal.backend: return
        if self.mode == "ssh": self.terminal.backend.write(f"cd {shlex.quote(path)}\r")
        elif self.mode == "local" and os.name == "nt": self.terminal.backend.write(f'cd /d "{path}"\r')
        elif self.mode == "local": self.terminal.backend.write(f"cd {shlex.quote(path)}\r")

    def set_show_hidden_files(self, enabled):
        enabled = bool(enabled)
        self.file_browser_settings["show_hidden"] = enabled
        self.host_store.save_file_browser_settings(self.file_browser_settings)

        local_filter = QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.System
        if enabled:
            local_filter |= QDir.Filter.Hidden
        self.local_model.setFilter(local_filter)

        self.remote_tree.show_hidden = enabled
        if self.mode == "ssh":
            self.remote_tree.go_to(self.remote_tree.current_directory() or "/")

        for box in getattr(self, "hidden_boxes", []):
            if box.isChecked() != enabled:
                box.blockSignals(True)
                box.setChecked(enabled)
                box.blockSignals(False)

    def set_follow_file_browser(self, enabled):
        self.terminal_settings["follow_file_browser_in_terminal"] = bool(enabled)
        self.host_store.save_terminal_settings(self.terminal_settings)
        if enabled:
            # This direction is browser -> terminal. Synchronise the terminal
            # to the browser's current directory when first enabled.
            path = self.remote_tree.current_directory() if self.mode == "ssh" else self.local_model.filePath(self.local_tree.rootIndex())
            if path: self.send_cd(path)

    def set_follow_terminal_browser(self, enabled):
        self.terminal_settings["follow_terminal_in_file_browser"] = bool(enabled)
        self.host_store.save_terminal_settings(self.terminal_settings)
        if enabled:
            self.install_cwd_reporting()
            if self.mode == "local": self.poll_terminal_cwd()

    def edit_terminal_settings(self):
        dialog = TerminalSettingsDialog(self.terminal_settings, self.host_store.path, self)
        if dialog.exec() != QDialog.DialogCode.Accepted: return
        self.terminal_settings = dialog.settings()
        self.host_store.save_terminal_settings(self.terminal_settings)
        
        for t in self._all_terminals():
            t.apply_settings(self.terminal_settings["cursor_style"], self.terminal_settings["cursor_blink"], self.terminal_settings.get("font_family","Monospace"), self.terminal_settings.get("font_size",11), self.terminal_settings.get("scrollback_limit",10000), self.terminal_settings.get("syntax_highlighting",True))
        self.set_follow_file_browser(self.terminal_settings.get("follow_file_browser_in_terminal", False))
        self.set_follow_terminal_browser(self.terminal_settings.get("follow_terminal_in_file_browser", False))
        if os.name == "nt":
            self.statusBar().showMessage(
                "PowerShell prediction changes apply to newly opened local Windows sessions.",
                5000
            )

    def install_cwd_reporting(self):
        if not self.terminal or not self.terminal.backend: return
        if not self.terminal_settings.get("follow_terminal_in_file_browser"): return
        backend = self.terminal.backend
        if self.mode == "ssh" and not getattr(backend, "channel", None):
            # SSH session setup can call this while authentication is still in
            # progress. Do not mark the hook installed until a live channel
            # exists; ssh_connected() will retry at the correct time.
            return
        if self.mode == "ssh" and not getattr(backend, "_shell_ready", False):
            # The transport is authenticated, but shell startup output is
            # still arriving. Installing now would interleave with .bashrc.
            return
        if self.mode == "local" and os.name == "nt" and getattr(backend, "_powerterm_cwd_hook_version", 0) >= 3:
            return
        if getattr(backend, "_powerterm_cwd_hook", False) and getattr(backend, "_powerterm_cwd_hook_version", 0) >= 6:
            # The prompt hook reports after every command, including its own
            # installation. Reinstalling or sending another synthetic command
            # only creates blank prompts and unnecessary history activity.
            return
        backend._powerterm_cwd_hook = True
        if self.mode == "ssh" or (self.mode == "local" and os.name != "nt"):
            cmd = bash_cwd_hook_command()
            # Prevent the hook-install command itself from being echoed into
            # the visible terminal. Restore echo immediately afterwards.
            # Bash echoes the complete input line before executing `stty -echo`,
            # so echo cannot hide this setup command retroactively. Do not use
            # `clear` here: it would destroy the visible terminal scrollback.
            setup_command = bash_private_command(cmd)
            self.terminal._hidden_input_fragments.append(setup_command)
            if self.mode == "ssh":
                self.terminal.begin_private_startup_transaction()
            backend.write("\x15" + setup_command + "\r")
            backend._powerterm_cwd_hook_version = 6
            QTimer.singleShot(1500, lambda t=self.terminal: t._hidden_input_fragments.clear())
        elif self.mode == "local" and os.name == "nt":
            cmd = r'''if ($PSVersionTable) { try { Set-PSReadLineOption -AddToHistoryHandler { param($line) -not $line.Contains('__bt_old_prompt') } } catch { }; if (-not $global:__bt_old_prompt) { $global:__bt_old_prompt = $function:prompt }; function global:prompt { $e=[char]27; $b=[char]7; Write-Host -NoNewline "$e]7;file://$env:COMPUTERNAME$((Get-Location).Path)$b"; & $global:__bt_old_prompt } }'''
            self.terminal._hidden_input_fragments.append(cmd)
            backend.write(cmd + "\r")
            backend._powerterm_cwd_hook_version = 3
            QTimer.singleShot(1500, lambda t=self.terminal: t._hidden_input_fragments.clear())

    def poll_terminal_cwd(self, force=False):
        if (not force and not self.terminal_settings.get("follow_terminal_in_file_browser")) or self.mode != "local": return
        backend = self.terminal.backend
        if os.name != "nt" and isinstance(backend, UnixPtyBackend):
            try: self.on_terminal_cwd_changed(os.readlink(f"/proc/{backend.proc.pid}/cwd"))
            except Exception: pass

    def on_session_cwd_changed(self, terminal, path):
        if terminal is not self.active_session(): return
        self.on_terminal_cwd_changed(path)

    def on_terminal_cwd_changed(self, path):
        if not path: return
        session=self.active_session()
        if session and self.mode=="ssh": session.remote_path=path
        if not self.terminal_settings.get("follow_terminal_in_file_browser"): return
        if self.mode == "ssh":
            current = posixpath.normpath(self.remote_tree.current_directory() or "/")
            target = posixpath.normpath(path)
            if current == target:
                return
            self.remote_tree.go_to(path); self.remote_path_label.setText(path); self.file_stack.setCurrentWidget(self.remote_file_page)
        elif self.mode == "local":
            path = os.path.normpath(path)
            if os.path.isdir(path):
                index = self.local_model.index(path)
                if index.isValid(): self.local_tree.setRootIndex(index); self.local_path_label.setText(path); self.file_stack.setCurrentWidget(self.local_file_page)

    def browser_up(self):
        if self.mode == "ssh":
            current = self.remote_tree.current_directory() or "/"
            parent = posixpath.dirname(current.rstrip("/")) or "/"
            self.remote_tree.go_to(parent)
            session = self.active_session()
            if session:
                session.remote_path = parent
            self.remote_path_label.setText(parent)
            if self.terminal_settings.get("follow_file_browser_in_terminal"):
                self.send_cd(parent)
        elif self.mode == "local":
            current = self.local_model.filePath(self.local_tree.rootIndex()) or str(Path.home())
            parent = os.path.dirname(os.path.normpath(current)) or current
            idx = self.local_model.index(parent)
            if idx.isValid():
                self.local_tree.setRootIndex(idx)
                self.local_path_label.setText(parent)
                if self.terminal_settings.get("follow_file_browser_in_terminal"):
                    self.send_cd(parent)

    def browser_home(self, remote=False):
        if remote:
            if self.mode == "ssh":
                self.remote_tree.go_home()
                if self.terminal_settings.get("follow_file_browser_in_terminal"):
                    self.send_cd(self.remote_tree.current_directory() or "/")
        else:
            home = str(Path.home()); self.local_tree.setRootIndex(self.local_model.index(home)); self.local_path_label.setText(home)
            if self.mode == "local" and self.terminal_settings.get("follow_file_browser_in_terminal"):
                self.send_cd(home)

    def browser_refresh(self, remote=False):
        if remote:
            if self.mode == "ssh": self.remote_tree.refresh()
        else:
            current = self.local_model.filePath(self.local_tree.rootIndex()) or str(Path.home())
            self.local_tree.setRootIndex(self.local_model.index(current)); self.local_path_label.setText(self.selected_local_path() or current)

    def _start_transfer_worker(self):
        if self.mode != "ssh" or not self.remote_backend or not self.remote_backend.client:
            QMessageBox.information(self, "File Transfer", "Connect to an SSH host first."); return None
        worker = TransferWorker(self.remote_backend, self)
        worker.finished.connect(lambda m, w=worker: self._transfer_finished(m, w))
        worker.failed.connect(lambda m, w=worker: self._transfer_failed(m, w))
        worker.progress.connect(self._transfer_progress)
        self._transfer_workers.append(worker)
        self.transfer_progress.show()
        return worker

    def _start_upload(self, local_path, remote_dir):
        if not local_path:
            return
        remote_dir = posixpath.normpath(remote_dir or "")
        if not remote_dir.startswith("/"):
            QMessageBox.warning(self, "Upload", "Choose a valid remote destination folder first.")
            return
        worker = self._start_transfer_worker()
        if not worker:
            return
        worker.remote_destination = remote_dir
        self.statusBar().showMessage(f"Uploading {local_path} → {remote_dir}…")
        worker.upload(local_path, remote_dir)

    def _transfer_progress(self, name, done, total):
        if total > 0:
            self.transfer_progress.setValue(max(0, min(100, int(done * 100 / total))))
            self.transfer_progress.setFormat(f"{name}  %p%")
        else:
            self.transfer_progress.setRange(0, 0)
            self.transfer_progress.setFormat(str(name))

    def upload_selected_local(self):
        worker = self._start_transfer_worker()
        if not worker: return
        local_path = self.selected_local_path()
        if not local_path or not os.path.exists(local_path): return
        remote_dir = self.remote_tree.current_directory()
        if not remote_dir: QMessageBox.information(self, "Upload", "Choose a remote destination folder first."); return
        self.statusBar().showMessage(f"Uploading {local_path}…"); worker.upload(local_path, remote_dir)

    def upload_files_here(self, remote_dir):
        paths, _ = QFileDialog.getOpenFileNames(self, "Upload files")
        for path in paths:
            self._start_upload(path, remote_dir)

    def upload_folder_here(self, remote_dir):
        path = QFileDialog.getExistingDirectory(self, "Upload folder", self.selected_local_directory())
        if path:
            self._start_upload(path, remote_dir)

    def upload_files_to_current_remote(self):
        if self.mode != "ssh": QMessageBox.information(self, "Upload", "Connect to an SSH host first."); return
        remote_dir = self.remote_tree.current_directory()
        paths, _ = QFileDialog.getOpenFileNames(self, "Upload files")
        for path in paths:
            self._start_upload(path, remote_dir)

    def upload_folder_to_current_remote(self):
        if self.mode != "ssh": QMessageBox.information(self, "Upload", "Connect to an SSH host first."); return
        remote_dir = self.remote_tree.current_directory()
        path = QFileDialog.getExistingDirectory(self, "Upload folder", self.selected_local_directory())
        if path:
            self._start_upload(path, remote_dir)

    def download_selected_remote(self):
        worker = self._start_transfer_worker()
        if not worker: return
        remote_path = self.remote_tree.selected_path()
        if not remote_path: return
        local_dir = QFileDialog.getExistingDirectory(self, "Download to", self.selected_local_directory())
        if local_dir: self.statusBar().showMessage(f"Downloading {remote_path}…"); worker.download(remote_path, local_dir)

    def _transfer_finished(self, message, worker):
        destination = getattr(worker, "remote_destination", "")
        if destination and self.remote_tree.loader:
            self.remote_tree.loader.invalidate(destination)
        if worker in self._transfer_workers: self._transfer_workers.remove(worker)
        if not self._transfer_workers:
            self.transfer_progress.hide()
            self.transfer_progress.setRange(0, 100)
        self.statusBar().showMessage(message, 6000); self.browser_refresh(True); self.browser_refresh(False)

    def _transfer_failed(self, message, worker):
        if worker in self._transfer_workers: self._transfer_workers.remove(worker)
        if not self._transfer_workers: self.transfer_progress.hide()
        QMessageBox.warning(self, "File Transfer", message)

    def local_context_menu(self, point):
        index = self.local_tree.indexAt(point)
        if index.isValid() and not self.local_tree.selectionModel().isSelected(index):
            self.local_tree.clearSelection(); self.local_tree.setCurrentIndex(index)
        path = self.selected_local_path()
        if not path: return
        targets = prune_nested_delete_targets(self.selected_local_paths(), False)
        for target, _ in targets: self._prefetch_delete_preview(target, False)
        menu = self._multi_file_menu(targets, False) if len(targets) > 1 else self.build_file_menu(path, os.path.isdir(path), False)
        if self.mode == "ssh" and len(targets) == 1:
            menu.addSeparator(); up = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_ArrowUp), "Upload to Current Remote Folder"); up.triggered.connect(self.upload_selected_local)
        menu.exec(self.local_tree.viewport().mapToGlobal(point))

    def remote_context_menu(self, point):
        item = self.remote_tree.itemAt(point)
        if item and not item.isSelected():
            self.remote_tree.clearSelection(); self.remote_tree.setCurrentItem(item)
        path = self.remote_tree.selected_path()
        if not path: return
        targets = prune_nested_delete_targets(self.selected_remote_paths(), True)
        is_dir = self.remote_tree.selected_is_dir(); multi = len(targets) > 1; menu = self._multi_file_menu(targets, True) if multi else self.build_file_menu(path, is_dir, True)
        for target, _ in targets: self._prefetch_delete_preview(target, True)
        if not multi:
            menu.addSeparator()
            if is_dir:
                up = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_ArrowUp), "Upload Files Here…"); up.triggered.connect(lambda: self.upload_files_here(path))
                up_folder = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_DirIcon), "Upload Folder Here…"); up_folder.triggered.connect(lambda: self.upload_folder_here(path))
            down = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_ArrowDown), "Download…"); down.triggered.connect(self.download_selected_remote)
        menu.exec(self.remote_tree.viewport().mapToGlobal(point))

    def _multi_file_menu(self, targets, remote):
        menu = QMenu(self)
        paths = [path for path, _ in targets]
        copy_paths = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_FileDialogContentsView), "Copy Selected Paths")
        copy_paths.triggered.connect(lambda: QApplication.clipboard().setText("\n".join(paths)))
        delete = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_TrashIcon), f"Delete {len(paths)} Selected Items…")
        delete.triggered.connect(lambda: self.delete_paths(targets, remote))
        menu.addSeparator()
        refresh = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_BrowserReload), "Refresh")
        refresh.triggered.connect(lambda: self.browser_refresh(remote))
        return menu

    def build_file_menu(self, path, is_dir, remote):
        menu = QMenu(self)
        if not is_dir:
            external = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_DialogOpenButton), "Open in Default Application")
            external.triggered.connect(lambda: self.open_in_default_application(path, remote))
            edit = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_FileDialogDetailedView), "Open / Edit…")
            edit.triggered.connect(lambda: self.open_file_editor(path, remote))
            menu.addSeparator()
        cp = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_FileDialogContentsView), "Copy Path"); cp.triggered.connect(lambda: QApplication.clipboard().setText(path))
        menu.addSeparator()
        rn = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_FileDialogDetailedView), "Rename…"); rn.triggered.connect(lambda: self.rename_path(path, remote))
        nf = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_FileIcon), "New File…"); nf.triggered.connect(lambda: self.new_entry(path, is_dir, remote, False))
        nd = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_DirIcon), "New Folder…"); nd.triggered.connect(lambda: self.new_entry(path, is_dir, remote, True))
        de = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_TrashIcon), "Delete…"); de.triggered.connect(lambda: self.delete_paths([(path, is_dir)], remote))
        menu.addSeparator()
        rr = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_BrowserReload), "Refresh"); rr.triggered.connect(lambda: self.browser_refresh(remote))
        pr = menu.addAction(self._std_icon(QStyle.StandardPixmap.SP_FileDialogInfoView), "Properties / Permissions…"); pr.triggered.connect(lambda: self.edit_properties(path, remote))
        return menu

    def open_in_default_application(self, path, remote):
        if not remote:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
                QMessageBox.warning(self, "Open File", "The operating system could not open this file type.")
            return
        if not self.remote_backend or not self.remote_backend.client:
            QMessageBox.warning(self, "Open File", "The SSH session is no longer connected.")
            return
        session = ExternalEditSession(self.remote_backend, path, self._external_cache_root, self)
        session.downloaded.connect(self._external_edit_downloaded)
        session.failed.connect(self._external_edit_failed)
        session.local_changed.connect(lambda s: s.check_remote())
        session.remote_checked.connect(self._confirm_external_upload)
        session.upload_progress.connect(self._transfer_progress)
        session.uploaded.connect(self._external_edit_uploaded)
        self._external_edit_sessions.add(session)
        self.transfer_progress.setRange(0, 100)
        self.transfer_progress.setValue(0)
        self.transfer_progress.setFormat("Opening… %p%")
        self.transfer_progress.show()
        self.statusBar().showMessage(f"Downloading temporary copy of {path}…")
        session.start()

    def _external_edit_downloaded(self, session):
        self.transfer_progress.hide()
        self.statusBar().showMessage(
            f"Opened temporary copy of {session.remote_path}; saves will be monitored.",
            6000,
        )
        session.open_local_copy()

    def _confirm_external_upload(self, session, remote_changed):
        message = (
            f"The local working copy of this remote file was changed:\n\n{session.remote_path}\n\n"
            "Upload it and replace the remote file?"
        )
        if remote_changed:
            message += (
                "\n\nWarning: the remote file has also changed since it was downloaded. "
                "Uploading will overwrite those remote changes."
            )
        choice = QMessageBox.question(
            self,
            "Upload Changed File",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if choice == QMessageBox.StandardButton.Yes:
            self.transfer_progress.setRange(0, 100)
            self.transfer_progress.setValue(0)
            self.transfer_progress.setFormat("Uploading… %p%")
            self.transfer_progress.show()
            self.statusBar().showMessage(f"Replacing {session.remote_path}…")
            session.upload()
        else:
            session.decline_upload()

    def _external_edit_uploaded(self, session):
        self.transfer_progress.hide()
        self.statusBar().showMessage(f"Replaced remote file {session.remote_path}", 6000)
        session._ensure_watched()
        self.browser_refresh(True)

    def _external_edit_failed(self, session, message):
        self.transfer_progress.hide()
        self.statusBar().clearMessage()
        QMessageBox.warning(self, "External File", message)

    def open_file_editor(self, path, remote):
        # Show immediate feedback before a local read or SFTP transfer blocks
        # long enough for binary/encoding detection to complete.
        self.transfer_progress.setRange(0, 0)
        self.transfer_progress.setFormat("Opening file…")
        self.transfer_progress.show()
        self.statusBar().showMessage(f"Opening {path}…")
        QApplication.processEvents()
        try:
            if remote:
                if not self.remote_backend or not self.remote_backend.client:
                    raise RuntimeError("The SSH session is no longer connected.")
                sftp = self.remote_backend.client.open_sftp()
                try:
                    attrs = sftp.stat(path)
                    file_mode = attrs.st_mode
                    if attrs.st_size > FileEditorDialog.MAX_EDIT_BYTES:
                        raise RuntimeError(
                            f"File is too large for the built-in editor ({human_size(attrs.st_size)}). "
                            f"The editor limit is {human_size(FileEditorDialog.MAX_EDIT_BYTES)}."
                        )
                    with sftp.open(path, "rb") as handle:
                        data = handle.read(FileEditorDialog.MAX_EDIT_BYTES + 1)
                finally:
                    sftp.close()
            else:
                local_attrs = os.stat(path)
                file_mode = local_attrs.st_mode
                size = local_attrs.st_size
                if size > FileEditorDialog.MAX_EDIT_BYTES:
                    raise RuntimeError(
                        f"File is too large for the built-in editor ({human_size(size)}). "
                        f"The editor limit is {human_size(FileEditorDialog.MAX_EDIT_BYTES)}."
                    )
                data = Path(path).read_bytes()

            if b"\r\n" in data:
                line_ending = "CRLF"
            elif b"\r" in data:
                line_ending = "CR"
            else:
                line_ending = "LF"

            text, file_encoding, encoding_label = decode_text_bytes(data)
            metadata = {
                "encoding": encoding_label,
                "encoding_codec": file_encoding,
                "line_ending": line_ending,
                "permissions": f"{stat.filemode(file_mode)} ({stat.S_IMODE(file_mode):03o})",
                "size": len(data),
            }
        except UnicodeDecodeError:
            QMessageBox.warning(
                self,
                "Open / Edit",
                "PowerTerm could not determine this file's text encoding."
            )
            return
        except ValueError as exc:
            QMessageBox.warning(self, "Open / Edit", str(exc))
            return
        except Exception as exc:
            QMessageBox.warning(self, "Open / Edit", str(exc))
            return
        finally:
            self.transfer_progress.hide()
            self.transfer_progress.setRange(0, 100)
            self.statusBar().clearMessage()

        def save_callback(new_text):
            encoding = metadata.get("encoding_codec", "utf-8")

            # Preserve the file's original newline convention.
            serialised = new_text
            if metadata.get("line_ending") == "CRLF":
                serialised = new_text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
            elif metadata.get("line_ending") == "CR":
                serialised = new_text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r")

            try:
                payload = serialised.encode(encoding)
            except UnicodeEncodeError as exc:
                raise RuntimeError(
                    f"The edited text contains characters that cannot be represented in "
                    f"{metadata.get('encoding', encoding)}. Save was cancelled."
                ) from exc

            if remote:
                sftp = self.remote_backend.client.open_sftp()
                try:
                    old_mode = sftp.stat(path).st_mode
                    with sftp.open(path, "wb") as handle:
                        handle.write(payload)
                    try:
                        sftp.chmod(path, stat.S_IMODE(old_mode))
                    except Exception:
                        pass
                finally:
                    sftp.close()
                self.browser_refresh(True)
            else:
                Path(path).write_bytes(payload)
                self.browser_refresh(False)

        dialog = FileEditorDialog(path, text, save_callback, metadata, self)
        dialog.exec()

    def rename_path(self, path, remote):
        old_name = posixpath.basename(path) if remote else os.path.basename(path)
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=old_name)
        if not ok or not new_name.strip() or new_name == old_name: return
        try:
            if remote:
                sftp = self.remote_backend.client.open_sftp(); new_path = posixpath.join(posixpath.dirname(path), new_name.strip())
                try: sftp.rename(path, new_path)
                finally: sftp.close()
            else: os.rename(path, os.path.join(os.path.dirname(path), new_name.strip()))
            self.browser_refresh(remote)
        except Exception as exc: QMessageBox.warning(self, "Rename", str(exc))

    def new_entry(self, selected_path, selected_is_dir, remote, directory):
        kind = "Folder" if directory else "File"; name, ok = QInputDialog.getText(self, f"New {kind}", f"{kind} name:")
        if not ok or not name.strip(): return
        try:
            if remote:
                parent = selected_path if selected_is_dir else posixpath.dirname(selected_path); new_path = posixpath.join(parent, name.strip()); sftp = self.remote_backend.client.open_sftp()
                try:
                    if directory: sftp.mkdir(new_path)
                    else: h = sftp.open(new_path, "x"); h.close()
                finally: sftp.close()
            else:
                parent = selected_path if selected_is_dir else os.path.dirname(selected_path); new_path = os.path.join(parent, name.strip())
                if directory: os.mkdir(new_path)
                else: open(new_path, "x", encoding="utf-8").close()
            self.browser_refresh(remote)
        except Exception as exc: QMessageBox.warning(self, f"New {kind}", str(exc))

    def _delete_contents_preview(self, path, remote, sftp=None, depth=0):
        """Return (name, is_dir, children) tuples for the confirmation tree."""
        if depth > 64:
            return [("… (tree preview truncated)", False, [])]
        result = []
        if remote:
            for attr in sorted(sftp.listdir_attr(path), key=lambda a: (not stat.S_ISDIR(a.st_mode), a.filename.lower())):
                if attr.filename in (".", ".."):
                    continue
                child = posixpath.join(path.rstrip("/"), attr.filename) or "/" 
                child_dir = stat.S_ISDIR(attr.st_mode)
                result.append((attr.filename, child_dir, self._delete_contents_preview(child, remote, sftp, depth + 1) if child_dir else []))
        else:
            with os.scandir(path) as scan:
                for entry in sorted(scan, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower())):
                    child_dir = entry.is_dir(follow_symlinks=False)
                    result.append((entry.name, child_dir, self._delete_contents_preview(entry.path, remote, sftp, depth + 1) if child_dir else []))
        return result

    def _delete_tree(self, path, remote, sftp=None, progress=None):
        if remote:
            attrs = sftp.stat(path)
            if stat.S_ISDIR(attrs.st_mode):
                for attr in sftp.listdir_attr(path):
                    if attr.filename not in (".", ".."):
                        child = posixpath.join(path.rstrip("/"), attr.filename) or "/"
                        self._delete_tree(child, True, sftp, progress)
                sftp.rmdir(path)
                if progress:
                    progress(path, True)
            else:
                sftp.remove(path)
                if progress:
                    progress(path, False)
        elif os.path.isdir(path) and not os.path.islink(path):
            with os.scandir(path) as scan:
                for entry in scan:
                    self._delete_tree(entry.path, False, progress=progress)
            os.rmdir(path)
            if progress:
                progress(path, True)
        else:
            os.remove(path)
            if progress:
                progress(path, False)

    def _prefetch_delete_preview(self, path, remote):
        key = (remote, path)
        if key in self._delete_preview_cache or key in self._delete_preview_jobs:
            return
        self._delete_preview_jobs.add(key)
        def load():
            sftp = None
            try:
                if remote:
                    sftp = self.remote_backend.client.open_sftp()
                preview = self._delete_contents_preview(path, remote, sftp)
                self._delete_preview_cache[key] = preview
            except Exception:
                pass
            finally:
                if sftp is not None:
                    sftp.close()
                self._delete_preview_jobs.discard(key)
        threading.Thread(target=load, daemon=True).start()

    def delete_path(self, path, is_dir, remote):
        self.delete_paths([(path, is_dir)], remote)

    def delete_paths(self, targets, remote):
        targets = prune_nested_delete_targets(targets, remote)
        if not targets:
            return
        sftp = None
        deleted = []
        failures = []
        try:
            if remote:
                sftp = self.remote_backend.client.open_sftp()
            previews = []
            total = 0
            for path, is_dir in targets:
                key = (remote, path)
                children = self._delete_preview_cache.pop(key, None)
                if children is None:
                    children = self._delete_contents_preview(path, remote, sftp) if is_dir else []
                previews.append((posixpath.basename(path.rstrip("/")) if remote else os.path.basename(path.rstrip(os.sep)), is_dir, children))
                total += 1
                def count_entries(items):
                    return sum(1 + (count_entries(grandchildren) if child_dir else 0) for _, child_dir, grandchildren in items)
                if is_dir:
                    total += count_entries(children)
            summary = targets[0][0] if len(targets) == 1 else f"{len(targets)} selected files and folders"
            dialog = DeleteConfirmationDialog(summary, targets[0][1], previews if len(targets) > 1 else previews[0][2], self, selection_count=len(targets))
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            completed = [0]
            self.transfer_progress.setRange(0, 100)
            self.transfer_progress.setValue(0)
            self.transfer_progress.setFormat("Deleting… %p%")
            self.transfer_progress.show()
            def deletion_progress(deleted_path, was_dir):
                completed[0] += 1
                deleted.append((deleted_path, was_dir))
                self.transfer_progress.setValue(min(100, int(completed[0] * 100 / total)))
                self.statusBar().showMessage(f"Deleting… {completed[0]} of {total}")
                QApplication.processEvents()
            for path, is_dir in targets:
                try:
                    self._delete_tree(path, remote, sftp, deletion_progress)
                except Exception as exc:
                    failures.append((path, is_dir, str(exc)))
            self.transfer_progress.hide()
            self._record_deletions(deleted, failures, remote)
            self.browser_refresh(remote)
            if failures:
                QMessageBox.warning(
                    self, "Delete",
                    f"Deleted {len(deleted)} item(s), but {len(failures)} selected item(s) could not be completely deleted.\n\n"
                    + "\n".join(f"{path}: {message}" for path, _, message in failures[:8])
                )
        except Exception as exc:
            if targets:
                failures.append((targets[0][0], targets[0][1], str(exc)))
                self._record_deletions(deleted, failures, remote)
            QMessageBox.warning(self, "Delete", str(exc))
        finally:
            if sftp is not None:
                sftp.close()

    def _record_deletions(self, deleted, failures, remote):
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        source = f"SSH {self.remote_backend.username}@{self.remote_backend.host}" if remote and self.remote_backend else "Local"
        records = [
            {"time": timestamp, "source": source, "result": "Deleted", "type": "Folder" if is_dir else "File", "path": path}
            for path, is_dir in deleted
        ]
        records.extend(
            {"time": timestamp, "source": source, "result": f"Failed: {message}", "type": "Folder" if is_dir else "File", "path": path}
            for path, is_dir, message in failures
        )
        if not records:
            return
        try:
            self.deletion_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.deletion_log_path.open("a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception as exc:
            self.statusBar().showMessage(f"Could not write deletion log: {exc}", 8000)

    def show_deletion_log(self):
        records = []
        try:
            if self.deletion_log_path.exists():
                lines = self.deletion_log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
                for line in lines:
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict):
                            records.append(record)
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:
            QMessageBox.warning(self, "Deletion Log", str(exc))
            return
        DeletionLogDialog(records, self).exec()

    def edit_properties(self, path, remote):
        self.transfer_progress.setRange(0, 0)
        self.transfer_progress.setFormat("Loading properties…")
        self.transfer_progress.show()
        self.statusBar().showMessage(f"Loading properties for {path}…")
        QApplication.processEvents()
        try:
            link_target = None
            if remote:
                sftp = self.remote_backend.client.open_sftp()
                try:
                    attrs = sftp.lstat(path)
                    if stat.S_ISLNK(attrs.st_mode):
                        try:
                            link_target = sftp.readlink(path)
                        except Exception:
                            pass
                finally:
                    sftp.close()
            else:
                attrs = os.lstat(path)
                if stat.S_ISLNK(attrs.st_mode):
                    try:
                        link_target = os.readlink(path)
                    except OSError:
                        pass

            def timestamp(value):
                return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if value else None

            if stat.S_ISDIR(attrs.st_mode):
                item_type = "Folder"
            elif stat.S_ISLNK(attrs.st_mode):
                item_type = "Symbolic link"
            elif stat.S_ISREG(attrs.st_mode):
                item_type = "File"
            else:
                item_type = "Special file"

            uid = getattr(attrs, "st_uid", None)
            gid = getattr(attrs, "st_gid", None)
            owner = f"UID {uid}" if uid is not None else None
            group = f"GID {gid}" if gid is not None else None
            if not remote and os.name != "nt":
                try:
                    import pwd
                    owner = f"{pwd.getpwuid(uid).pw_name} (UID {uid})"
                except (ImportError, KeyError, TypeError):
                    pass
                try:
                    import grp
                    group = f"{grp.getgrgid(gid).gr_name} (GID {gid})"
                except (ImportError, KeyError, TypeError):
                    pass

            created = getattr(attrs, "st_birthtime", None)
            if created is None and not remote and os.name == "nt":
                created = getattr(attrs, "st_ctime", None)
            metadata = {
                "Type": item_type,
                "Location": posixpath.dirname(path.rstrip("/")) or "/" if remote else os.path.dirname(path.rstrip("/\\")) or str(Path(path).anchor),
                "Owner": owner,
                "Group": group,
                "Link target": link_target,
                "Inode": getattr(attrs, "st_ino", None),
                "Hard links": getattr(attrs, "st_nlink", None),
                "Created": timestamp(created),
                "Last modified": timestamp(getattr(attrs, "st_mtime", None)),
                "Last accessed": timestamp(getattr(attrs, "st_atime", None)),
            }
            self.transfer_progress.hide()
            self.transfer_progress.setRange(0, 100)
            self.statusBar().clearMessage()
            shown_size = None if stat.S_ISDIR(attrs.st_mode) else getattr(attrs, "st_size", None)
            dialog = PermissionsDialog(path, attrs.st_mode, shown_size, metadata, self)
            if dialog.exec() != QDialog.DialogCode.Accepted: return
            perms = dialog.permission_mode()
            if perms == stat.S_IMODE(attrs.st_mode):
                return
            if remote:
                sftp = self.remote_backend.client.open_sftp()
                try: sftp.chmod(path, perms)
                finally: sftp.close()
            else: os.chmod(path, perms)
            self.browser_refresh(remote)
        except Exception as exc:
            QMessageBox.warning(self, "Properties", str(exc))
        finally:
            self.transfer_progress.hide()
            self.transfer_progress.setRange(0, 100)
            self.statusBar().clearMessage()

    def update_status(self):
        if self.mode == "none": return
        if self.mode == "ssh": self.request_remote_stats(); return
        if self.mode == "ssh-connecting": return
        cpu = psutil.cpu_percent(None); mem = psutil.virtual_memory()
        try: disk_text = f"Disc {psutil.disk_usage(Path.home().anchor or '/').percent:.0f}%"
        except Exception: disk_text = "Disc ?"
        now = time.monotonic(); net = psutil.net_io_counters(); elapsed = max(0.001, now - self.last_net_time)
        if self.last_net is None:
            down = up = 0.0
        else:
            down = (net.bytes_recv - self.last_net.bytes_recv) / elapsed; up = (net.bytes_sent - self.last_net.bytes_sent) / elapsed
        self.last_net, self.last_net_time = net, now
        self.source_status.setText(f"LOCAL {socket.gethostname()}")
        self.cpu_status.setText(f"CPU {cpu:4.1f}%"); self.ram_status.setText(f"RAM {mem.percent:4.1f}%"); self.disk_status.setText(disk_text); self.net_status.setText(f"↓ {human_rate(down)}  ↑ {human_rate(up)}"); self.usb_status.setText("USB")

    def request_remote_stats(self):
        if self.remote_stats_busy or not self.remote_backend or not self.remote_backend.client: return
        terminal = self.active_session()
        if terminal and time.monotonic() < getattr(terminal, "remote_stats_not_before", 0.0):
            return
        if not getattr(self.remote_backend, "_shell_ready", False):
            return
        self.remote_stats_busy = True; backend = self.remote_backend; generation = self.session_generation
        threading.Thread(target=self._remote_stats_worker, args=(backend, generation), daemon=True).start()

    def _remote_stats_worker(self, backend, generation):
        try:
            command = "printf 'HOST '; hostname 2>/dev/null || uname -n\nprintf 'CPU '; head -n 1 /proc/stat 2>/dev/null\nprintf 'MEM '; awk '/MemTotal:/{t=$2}/MemAvailable:/{a=$2}END{print t, a}' /proc/meminfo 2>/dev/null\nprintf 'DISK '; df -Pk / 2>/dev/null | awk 'NR==2{print $2, $3, $5}'\nprintf 'NET '; awk 'NR>2 {gsub(\":\",\"\",$1); if ($1!=\"lo\") {rx+=$2; tx+=$10}} END{print rx+0,tx+0}' /proc/net/dev 2>/dev/null\nprintf 'LOAD '; cat /proc/loadavg 2>/dev/null | awk '{print $1,$2,$3}'\n"
            _, out, _ = backend.run_command(command, timeout=5)
            snapshot = parse_remote_snapshot(out); snapshot["backend"] = backend; snapshot["generation"] = generation; self.remote_stats_ready.emit(snapshot)
        except Exception as exc: self.remote_stats_ready.emit({"backend": backend, "generation": generation, "error": str(exc)})

    def apply_remote_stats(self, snapshot):
        self.remote_stats_busy = False
        if snapshot.get("backend") is not self.remote_backend or snapshot.get("generation") != self.session_generation or self.mode != "ssh": return
        host = snapshot.get("host") or self.remote_backend.host; self.source_status.setText(f"REMOTE {host}")
        if "error" in snapshot:
            self.cpu_status.setText("CPU ?"); self.ram_status.setText("RAM ?"); self.disk_status.setText("Disc ?"); self.net_status.setText("Network ?"); return
        cpu_text = "CPU ?"; cpu = snapshot.get("cpu")
        if cpu:
            total = sum(cpu); idle = cpu[3] + (cpu[4] if len(cpu) > 4 else 0)
            if self.remote_prev_cpu:
                pt, pi = self.remote_prev_cpu; dt = total-pt; di = idle-pi
                if dt > 0: cpu_text = f"CPU {(100.0*(dt-di)/dt):4.1f}%"
            self.remote_prev_cpu = (total, idle)
        mt, ma = snapshot.get("mem", (0,0)); mem_text = f"RAM {100.0*(mt-ma)/mt:4.1f}%" if mt else "RAM ?"
        _, _, dp = snapshot.get("disk", (0,0,"?")); disk_text = f"Disc {dp}" if dp != "?" else "Disc ?"
        rx, tx = snapshot.get("net", (0,0)); now = time.monotonic(); net_text = "Network ?"
        if self.remote_prev_net:
            prx, ptx, pt = self.remote_prev_net; elapsed=max(.001, now-pt); net_text=f"↓ {human_rate(max(0,rx-prx)/elapsed)}  ↑ {human_rate(max(0,tx-ptx)/elapsed)}"
        self.remote_prev_net=(rx,tx,now)
        self.cpu_status.setText(cpu_text); self.ram_status.setText(mem_text); self.disk_status.setText(disk_text); self.net_status.setText(net_text); self.usb_status.setText("USB")

    def show_system_info(self, kind):
        if self.mode not in ("local", "ssh"): QMessageBox.information(self, "System Information", "Start a local or SSH session first."); return
        source=self.mode; generation=self.session_generation; backend=self.remote_backend; self.statusBar().showMessage(f"Loading {kind} information…")
        threading.Thread(target=self._info_worker, args=(kind,source,generation,backend), daemon=True).start()

    def _info_worker(self, kind, source, generation, backend):
        try:
            if source == "ssh":
                _, out, err = backend.run_command(self._remote_info_command(kind), timeout=10); text=out + (("\n"+err) if err.strip() else ""); title=f"{kind.title()} — {backend.host}"
            else: text=self._local_info(kind); title=f"{kind.title()} — {socket.gethostname()}"
            self.info_ready.emit({"generation":generation,"title":title,"text":text or "No information returned."})
        except Exception as exc: self.info_ready.emit({"generation":generation,"title":kind.title(),"text":f"Could not retrieve information:\n{exc}"})

    def _remote_info_command(self, kind):
        return {
            "disk": "df -hT 2>&1; printf '\\nBlock devices:\\n'; lsblk -o NAME,SIZE,FSTYPE,TYPE,MOUNTPOINTS,MODEL 2>&1 || true",
            "network": "printf 'Addresses:\\n'; ip -brief address 2>&1 || ifconfig -a 2>&1; printf '\\nRoutes:\\n'; ip route 2>&1 || route -n 2>&1",
            "usb": "lsusb 2>&1 || (echo 'lsusb is not installed'; command -v usb-devices >/dev/null && usb-devices)",
            "cpu": "lscpu 2>&1 || cat /proc/cpuinfo 2>&1",
            "ram": "free -h 2>&1; printf '\\nMeminfo:\\n'; head -n 25 /proc/meminfo 2>&1",
        }[kind]

    def _local_info(self, kind):
        if os.name == "nt":
            preamble = (
                "$ProgressPreference='SilentlyContinue'; "
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                "$OutputEncoding=[Console]::OutputEncoding; "
                "if ($PSVersionTable.PSVersion.Major -ge 7) { $PSStyle.OutputRendering='PlainText' }; "
            )
            command = {
                "disk": preamble + r'''
Write-Output '=== PHYSICAL DISKS ===';
Get-CimInstance Win32_DiskDrive |
  Select-Object Index,Model,InterfaceType,MediaType,SerialNumber,@{N='SizeGB';E={[math]::Round($_.Size/1GB,1)}} |
  Format-Table -AutoSize | Out-String -Width 240;
Write-Output '=== PARTITIONS ===';
Get-Partition | Select-Object DiskNumber,PartitionNumber,DriveLetter,Type,@{N='SizeGB';E={[math]::Round($_.Size/1GB,1)}} |
  Format-Table -AutoSize | Out-String -Width 240;
Write-Output '=== VOLUMES ===';
Get-Volume | Select-Object DriveLetter,FileSystemLabel,FileSystem,HealthStatus,OperationalStatus,@{N='SizeGB';E={[math]::Round($_.Size/1GB,1)}},@{N='FreeGB';E={[math]::Round($_.SizeRemaining/1GB,1)}} |
  Format-Table -AutoSize | Out-String -Width 240
''',
                "network": preamble + r'''
Write-Output '=== NETWORK ADAPTERS ===';
Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,LinkSpeed,MacAddress | Format-Table -AutoSize | Out-String -Width 240;
Write-Output '=== IP CONFIGURATION ===';
Get-NetIPConfiguration | ForEach-Object {
  [pscustomobject]@{Interface=$_.InterfaceAlias;IPv4=($_.IPv4Address.IPAddress -join ', ');IPv6=($_.IPv6Address.IPAddress -join ', ');Gateway=($_.IPv4DefaultGateway.NextHop -join ', ');DNS=($_.DNSServer.ServerAddresses -join ', ')}
} | Format-Table -Wrap -AutoSize | Out-String -Width 240;
Write-Output '=== IPV4 ROUTES ===';
Get-NetRoute -AddressFamily IPv4 | Sort-Object RouteMetric,DestinationPrefix | Select-Object DestinationPrefix,NextHop,InterfaceAlias,RouteMetric |
  Format-Table -AutoSize | Out-String -Width 240
''',
                "usb": preamble + r'''
Write-Output '=== USB DEVICES ===';
Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB*' -or $_.Class -eq 'USB' } |
  Select-Object Status,Class,FriendlyName,InstanceId | Format-Table -Wrap -AutoSize | Out-String -Width 240
''',
                "cpu": preamble + r'''
Write-Output '=== PROCESSOR ===';
Get-CimInstance Win32_Processor |
  Select-Object Name,Manufacturer,SocketDesignation,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,CurrentClockSpeed,LoadPercentage |
  Format-List | Out-String -Width 240
''',
                "ram": preamble + r'''
Write-Output '=== MEMORY SUMMARY ===';
Get-CimInstance Win32_OperatingSystem |
  Select-Object @{N='TotalGB';E={[math]::Round($_.TotalVisibleMemorySize/1MB,2)}},@{N='FreeGB';E={[math]::Round($_.FreePhysicalMemory/1MB,2)}},@{N='UsedGB';E={[math]::Round(($_.TotalVisibleMemorySize-$_.FreePhysicalMemory)/1MB,2)}} |
  Format-List | Out-String -Width 240;
Write-Output '=== PHYSICAL MEMORY MODULES ===';
Get-CimInstance Win32_PhysicalMemory |
  Select-Object DeviceLocator,Manufacturer,PartNumber,@{N='CapacityGB';E={[math]::Round($_.Capacity/1GB,1)}},Speed,ConfiguredClockSpeed |
  Format-Table -AutoSize | Out-String -Width 240
''',
            }[kind]
            exe=shutil.which("pwsh") or shutil.which("powershell")
            if not exe: return "PowerShell is not available."
            cp=subprocess.run(
                [exe,"-NoLogo","-NoProfile","-NonInteractive","-Command",command],
                capture_output=True, encoding="utf-8", errors="replace", timeout=15
            )
            text = cp.stdout + (("\n"+cp.stderr) if cp.stderr.strip() else "")
            return strip_ansi(text).replace("\r\n", "\n").replace("\r", "\n")
        command = {
            "disk": "df -hT; printf '\\nBlock devices:\\n'; lsblk -o NAME,SIZE,FSTYPE,TYPE,MOUNTPOINTS,MODEL 2>&1 || true",
            "network": "printf 'Addresses:\\n'; ip -brief address 2>&1 || ifconfig -a 2>&1; printf '\\nRoutes:\\n'; ip route 2>&1 || route -n 2>&1",
            "usb": "lsusb 2>&1 || (echo 'lsusb is not installed'; command -v usb-devices >/dev/null && usb-devices)",
            "cpu": "lscpu 2>&1 || cat /proc/cpuinfo 2>&1",
            "ram": "free -h 2>&1; printf '\\nMeminfo:\\n'; head -n 25 /proc/meminfo 2>&1",
        }[kind]
        cp=subprocess.run(["/bin/sh","-lc",command],capture_output=True,text=True,timeout=10); return cp.stdout + (("\n"+cp.stderr) if cp.stderr.strip() else "")

    def _show_info_result(self, result):
        if result.get("generation") != self.session_generation: return
        self.statusBar().clearMessage(); InfoDialog(result["title"], result["text"], self.terminal_settings, self).exec()

    def closeEvent(self, event):
        self.session_generation += 1
        for w in self._all_terminals():
            w.close_backend()
        cache_root = self._external_cache_root.resolve()
        expected_parent = Path(tempfile.gettempdir()).resolve()
        if cache_root.parent == expected_parent and cache_root.name.startswith("powerterm-external-"):
            shutil.rmtree(cache_root, ignore_errors=True)
        super().closeEvent(event)


# ----------------------------- Helpers ----------------------------------------

def should_auto_connect_saved_password(enabled, stored_password):
    """Return whether a saved-host credential dialog can be skipped."""
    return bool(enabled and stored_password)

def parse_remote_snapshot(text):
    result = {}
    for raw in text.splitlines():
        if raw.startswith("HOST "):
            result["host"] = raw[5:].strip()
        elif raw.startswith("CPU cpu "):
            try:
                result["cpu"] = [int(x) for x in raw.split()[2:]]
            except ValueError:
                pass
        elif raw.startswith("MEM "):
            parts = raw.split()
            if len(parts) >= 3:
                try:
                    result["mem"] = (int(parts[1]), int(parts[2]))
                except ValueError:
                    pass
        elif raw.startswith("DISK "):
            parts = raw.split()
            if len(parts) >= 4:
                try:
                    result["disk"] = (int(parts[1]), int(parts[2]), parts[3])
                except ValueError:
                    pass
        elif raw.startswith("NET "):
            parts = raw.split()
            if len(parts) >= 3:
                try:
                    result["net"] = (int(parts[1]), int(parts[2]))
                except ValueError:
                    pass
        elif raw.startswith("LOAD "):
            result["load"] = " ".join(raw.split()[1:4]) or "?"
    return result


def human_rate(value):
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    value = float(value)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024


def human_size(value):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(value)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PowerTerm")
    app.setOrganizationName("PowerTerm")

    icon_path = Path(__file__).with_name("powerterm.svg")
    if icon_path.exists():
        icon = QIcon(str(icon_path))
        app.setWindowIcon(icon)
    else:
        icon = QIcon()

    # Give Windows a stable taskbar identity when launched as a Python script.
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PowerTerm.TerminalClient")
        except Exception:
            pass

    # Paint a lightweight native frame first, then build the heavier Qt models
    # and panes inside it. This materially improves perceived cold-start time.
    window = MainWindow(show_early=True)
    if not icon.isNull():
        window.setWindowIcon(icon)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
