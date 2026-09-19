import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QAbstractItemView, QTreeWidgetItem
from PySide6.QtCore import Qt

from main import APP_VERSION, MainWindow, PermissionsDialog, RemoteFileLoader, RemoteFileTree, SshBackend, TerminalWidget, bash_cwd_hook_command, bash_private_command, file_fingerprint, prune_nested_delete_targets, should_auto_connect_saved_password, ssh_auth_probe_options


class TerminalRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.terminal = TerminalWidget()
        self.terminal.resize(900, 420)
        self.terminal._apply_terminal_resize()

    def tearDown(self):
        self.terminal.deleteLater()
        self.app.processEvents()

    def test_bash_private_command_restores_history_state_and_suppresses_output(self):
        wrapped = bash_private_command("printf secret; printf error >&2")
        self.assertTrue(wrapped.startswith(" "))
        self.assertIn('history -d "$__pt_hn"', wrapped)
        self.assertIn('__powerterm_private_command__', wrapped)
        self.assertIn('case "$__pt_latest"', wrapped)
        self.assertIn("set +o history", wrapped)
        self.assertIn("ignorespace", wrapped)
        self.assertIn('shopt -qo history', wrapped)
        self.assertIn('if [ "$__pt_hist_was_on" = 1 ]', wrapped)
        self.assertIn("unset HISTCONTROL", wrapped)
        self.assertTrue(wrapped.endswith(">/dev/null 2>&1"))

    def test_bash_cwd_hook_flushes_normal_commands_for_session_restart(self):
        hook = bash_cwd_hook_command()
        self.assertIn("history -a", hook)
        self.assertIn("__powerterm_cwd_report", hook)
        # The hook installer itself is wrapped privately; only subsequently
        # entered user commands should be appended to Bash's history file.
        self.assertNotIn("set +o history", hook)

    def test_private_startup_transaction_does_not_render_a_blank_submission(self):
        self.feed_now("Welcome to Linux\r\nbash startup flag content\r\nprompt$ ")
        self.terminal.begin_private_startup_transaction(timeout_ms=5000)
        self.feed_now(
            "\r\n"
            "\x1b]7;file://host/home/user\x07"
            "prompt$ "
        )
        replay = "".join(self.terminal._replay_chunks)
        self.assertIn("bash startup flag content", replay)
        self.assertNotIn("\r\n\r\n", replay)
        self.assertIn("prompt$ ", replay)
        self.assertEqual(self.terminal.screen.display[self.terminal.screen.cursor.y].strip(), "prompt$")

    def feed_now(self, text):
        self.terminal.feed(text)
        self.terminal._process_input_batch()
        self.terminal.render_screen()
        self.app.processEvents()

    def test_readline_redraw_keeps_cursor_at_active_prompt(self):
        # Clear the line, write the search prompt, then move forward 28 cells.
        # CSI 28 C is relative to the cursor after writing the prompt.
        self.feed_now("bash$ printf 'hello\\n'")
        self.feed_now("\x1b[2K\r(reverse-i-search)`foo': bar\x1b[K\x1b[28C")

        self.assertEqual(self.terminal.screen.cursor.y, 0)
        self.assertEqual(self.terminal.screen.cursor.x, len("(reverse-i-search)`foo': bar") + 28)
        self.assertEqual(self.terminal._live_cursor_document_position(), self.terminal.screen.cursor.x)
        self.assertTrue(self.terminal._is_live_view())

    def test_live_render_is_bottom_anchored(self):
        self.feed_now("line 1\r\nline 2\r\nline 3\r\n")
        scrollbar = self.terminal.verticalScrollBar()
        self.assertEqual(scrollbar.value(), scrollbar.maximum())

    def test_history_view_is_not_forced_to_bottom(self):
        self.feed_now("one\r\ntwo\r\nthree\r\n")
        scrollbar = self.terminal.verticalScrollBar()
        scrollbar.setValue(max(scrollbar.minimum(), scrollbar.maximum() - 1))
        self.assertTrue(self.terminal._history_scrolled)
        self.assertNotEqual(scrollbar.value(), scrollbar.maximum())

    def test_resize_preserves_terminal_cursor_coordinates(self):
        self.feed_now("prompt> abc")
        old = (self.terminal.screen.cursor.x, self.terminal.screen.cursor.y)
        self.terminal._rebuild_screen(60, 12)
        self.assertEqual((self.terminal.screen.cursor.x, self.terminal.screen.cursor.y), old)

    def test_enlarging_terminal_preserves_cursor_bottom_offset(self):
        self.terminal.screen.cursor.x = 7
        self.terminal.screen.cursor.y = self.terminal.screen.lines - 2
        self.terminal._rebuild_screen(140, 60)
        self.assertEqual(self.terminal.screen.cursor.x, 7)
        self.assertEqual(self.terminal.screen.cursor.y, 58)

    def test_initial_resize_keeps_an_empty_terminal_cursor_at_the_top(self):
        self.terminal.reset_terminal()
        self.terminal._rebuild_screen(167, 46)
        self.assertEqual((self.terminal.screen.cursor.x, self.terminal.screen.cursor.y), (0, 0))

    def test_widget_resize_does_not_replay_the_terminal_journal(self):
        self.feed_now("previous terminal output\r\n")
        self.terminal.screen.resize(lines=5, columns=20)
        self.terminal._last_terminal_size = None
        self.terminal._rebuild_screen = lambda *_args, **_kwargs: self.fail("resize must not replay terminal output")
        self.terminal._apply_terminal_resize()

    def test_new_backend_receives_resize_when_terminal_was_already_laid_out(self):
        class Backend:
            def __init__(self):
                self.sizes = []

            def resize(self, cols, rows):
                self.sizes.append((cols, rows))

        # Simulate opening a terminal in a maximized window: Qt has already
        # measured and sized the emulator before the process is attached.
        metrics = self.terminal.fontMetrics()
        laid_out_size = (
            max(20, (self.terminal.viewport().width() - 4) // max(1, metrics.horizontalAdvance("M"))),
            max(5, (self.terminal.viewport().height() - 4) // max(1, metrics.height())),
        )
        backend = Backend()
        self.terminal.backend = backend
        self.terminal._last_terminal_size = laid_out_size
        self.terminal._last_backend_size = None
        self.terminal._apply_terminal_resize()

        self.assertEqual(backend.sizes, [laid_out_size])
        self.terminal._apply_terminal_resize()
        self.assertEqual(backend.sizes, [laid_out_size])

    def test_first_terminal_output_synchronises_the_backend_geometry(self):
        class Backend:
            def __init__(self):
                self.sizes = []

            def resize(self, cols, rows):
                self.sizes.append((cols, rows))

        metrics = self.terminal.fontMetrics()
        displayed_size = (
            max(20, (self.terminal.viewport().width() - 4) // max(1, metrics.horizontalAdvance("M"))),
            max(5, (self.terminal.viewport().height() - 4) // max(1, metrics.height())),
        )
        backend = Backend()
        self.terminal.backend = backend
        # Represent the provisional dimensions used while the tab was being
        # created, before the window manager completed its maximized layout.
        self.terminal._last_terminal_size = (100, 30)
        self.terminal._last_backend_size = (100, 30)
        self.terminal._backend_geometry_needs_sync = True

        self.feed_now("shell prompt> ")

        self.assertEqual(backend.sizes, [displayed_size])

    def test_ssh_pty_uses_the_measured_size_before_connection_starts(self):
        backend = SshBackend("example.invalid", 22, "tester", "", autostart=False)
        try:
            backend.resize(167, 46)
            self.assertEqual(backend._pty_size, (167, 46))
            self.assertFalse(backend._started)
        finally:
            backend.close()

    def test_terminal_input_contract_for_ctrl_r_tab_and_readline_editing(self):
        class Backend:
            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(text)

        backend = Backend()
        self.terminal.backend = backend
        self.terminal._session_active = True
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        self.terminal.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier, "r"))
        self.terminal.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier, "\t"))
        self.terminal.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_B, Qt.KeyboardModifier.AltModifier, "b"))
        self.terminal.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_F, Qt.KeyboardModifier.AltModifier, "f"))
        self.terminal.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_D, Qt.KeyboardModifier.AltModifier, "d"))
        self.terminal.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier, ""))
        self.terminal.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier, ""))
        self.terminal.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Backspace, Qt.KeyboardModifier.AltModifier, ""))
        self.terminal.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Slash, Qt.KeyboardModifier.ControlModifier, "/"))
        self.terminal.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.ControlModifier, ""))
        self.terminal.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier, ""))
        self.assertEqual(backend.writes, [
            "\x12", "\t", "\x1bb", "\x1bf", "\x1bd", "\x1b[1;3D", "\x1b[1;3C", "\x1b\x7f",
            "\x1f", "\x1b[1;5D", "\x1b[1;5C",
        ])

    def test_closed_session_stops_cursor_and_rejects_input(self):
        class Backend:
            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(text)

        backend = Backend()
        self.terminal.backend = backend
        self.terminal._session_active = True
        self.terminal._backend_closed()
        self.assertFalse(self.terminal._session_active)
        self.assertFalse(self.terminal._cursor_timer.isActive())
        self.assertFalse(self.terminal._cursor_on)

        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent
        self.terminal.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a"))
        self.assertEqual(backend.writes, [])

    def test_multiline_paste_uses_bracketed_paste(self):
        class Backend:
            def __init__(self):
                self.writes = []

            def write(self, text):
                self.writes.append(text)

        backend = Backend()
        self.terminal.backend = backend
        QApplication.clipboard().setText("echo one\necho two")
        self.terminal.paste_to_terminal()
        self.assertEqual(backend.writes, ["\x1b[200~echo one\necho two\x1b[201~"])

    def test_terminal_presentation_contract_remains_terminal_like(self):
        from PySide6.QtCore import Qt

        self.assertTrue(self.terminal.isReadOnly())
        self.assertEqual(self.terminal.lineWrapMode(), self.terminal.LineWrapMode.NoWrap)
        self.assertEqual(self.terminal.verticalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.assertEqual(self.terminal.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assertIn("#101010", self.terminal.styleSheet())
        self.assertFalse(self.terminal.line_numbers_visible)

    def test_terminal_accepts_keyboard_focus(self):
        self.terminal.show()
        self.terminal.setFocus()
        self.app.processEvents()
        self.assertTrue(self.terminal.focusPolicy() & Qt.FocusPolicy.StrongFocus)

    def test_scrollback_selection_stays_attached_to_text_while_scrolling(self):
        self.terminal._rebuild_screen(30, 5)
        self.feed_now("".join(f"history line {number:02d}\r\n" for number in range(16)))
        document = self.terminal.document()
        start = document.toPlainText().index("history line 02")
        end = document.toPlainText().index("history line 05") + len("history line 05")
        selection = self.terminal.textCursor()
        selection.setPosition(start)
        selection.setPosition(end, selection.MoveMode.KeepAnchor)
        self.terminal.setTextCursor(selection)
        selected_before = selection.selectedText()

        scrollbar = self.terminal.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), scrollbar.minimum())
        scrollbar.setValue(scrollbar.minimum())
        self.app.processEvents()

        self.assertTrue(self.terminal._history_scrolled)
        self.assertEqual(self.terminal.textCursor().selectedText(), selected_before)
        self.assertIn("history line 02", self.terminal.textCursor().selectedText())
        self.assertIn("history line 05", self.terminal.textCursor().selectedText())

    def test_scrollback_selection_survives_new_terminal_output(self):
        self.terminal._rebuild_screen(30, 5)
        self.feed_now("".join(f"selected history {number:02d}\r\n" for number in range(16)))
        scrollbar = self.terminal.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        document = self.terminal.document()
        # Select visible, recent rows so the terminal remains at its live
        # bottom while new output arrives.
        start = document.toPlainText().index("selected history 14")
        end = document.toPlainText().index("selected history 15") + len("selected history 15")
        selection = self.terminal.textCursor()
        selection.setPosition(start)
        selection.setPosition(end, selection.MoveMode.KeepAnchor)
        self.terminal.setTextCursor(selection)
        selected_before = selection.selectedText()

        self.feed_now("new output after selection\r\n")

        self.assertEqual(self.terminal.textCursor().selectedText(), selected_before)
        self.assertIn("new output after selection", self.terminal.document().toPlainText())

    def test_multiline_readline_redraw_replaces_only_the_live_rows(self):
        self.terminal._rebuild_screen(32, 5)
        self.feed_now("".join(f"old line {number}\r\n" for number in range(10)))
        old_history = self.terminal.document().toPlainText()
        self.feed_now("\x1b[2A\r\x1b[2K(reverse-i-search)`one': first\r\n\x1b[2K(reverse-i-search)`two': second\x1b[8D")

        rendered = self.terminal.document().toPlainText()
        self.assertIn("old line 1", rendered)
        self.assertIn("(reverse-i-search)`one': first", rendered)
        self.assertIn("(reverse-i-search)`two': second", rendered)
        self.assertEqual(self.terminal._live_cursor_document_position(),
                         rendered.rfind("(reverse-i-search)`two': second") + len("(reverse-i-search)`two': second") - 8)

    def test_readline_shrinking_wrapped_history_entry_erases_its_tail(self):
        # This is Bash/readline's actual compact redraw form for changing from
        # a long wrapped history entry back to "echo short": movement, DCH,
        # line erases, then cursor repositioning.
        self.terminal._rebuild_screen(20, 5)
        self.feed_now("P> echo this-command-is-much-longer-than-short")
        self.feed_now(
            "\x1b[A\x1b[A\x1b[C\x1b[C\x1b[7Pshort\r\n\r\x1b[K"
            "\r\n\r\x1b[K\x1b[A\x1b[A" + "\x1b[C" * 15
        )

        rows = self.terminal.document().toPlainText().splitlines()[-5:]
        self.assertEqual(rows[0].rstrip(), "P> echo short")
        self.assertTrue(all(not row.strip() for row in rows[1:]))
        self.assertFalse(self.terminal._requires_full_document_rebuild)

        # Once a command is executed, its corrected input rows become
        # scrollback. The renderer must retain the correction there without
        # writing into pyte's history buffer.
        self.feed_now("result\r\n" * 5)
        rendered = self.terminal.document().toPlainText()
        self.assertIn("P> echo short", rendered)
        self.assertNotIn("short-command", rendered)

    def test_wrapped_history_commands_do_not_retain_a_previous_command_suffix(self):
        # Captured from Bash/readline while replacing the second command with
        # the first at a 158-column terminal. The old command wraps to seven
        # final cells; the replacement wraps to two, so a stale " main" tail
        # used to be especially visible here.
        first = (
            "echo./redeploy.sh && git status && git add . && git commit -m "
            "'filter parameter marker on HTMLJS graphs echo in customer facing site' "
            "&& git push origin main"
        )
        second = (
            "./redeploy.sh && git status && git add . && git commit -m "
            "'CMS upgrades. Major upgrades to graph table import and line draw functionality' "
            "&& git push origin main"
        )
        self.terminal._rebuild_screen(158, 5)
        self.feed_now("user1@xps - $ " + second)
        self.feed_now("\x1b[A\x08\x08\x08\x08" + first[:-1] + "\x1b[C\x1b[K")

        rendered = [line.rstrip() for line in self.terminal.document().toPlainText().splitlines()[-5:]]
        first_row_length = self.terminal.screen.columns - len("user1@xps - $ ")
        self.assertEqual(rendered[:2], [
            "user1@xps - $ " + first[:first_row_length],
            first[first_row_length:],
        ])
        self.assertNotIn("main main", "\n".join(rendered))

    def test_split_readline_delete_sequence_clears_wrapped_command_tail(self):
        # Bash uses CSI 5 P here at the width shown in the reported terminal
        # screenshot. Split it as an SSH/PTY read can: ESC[5 then P.
        first = (
            "echo./redeploy.sh && git status && git add . && git commit -m "
            "'filter parameter marker on HTMLJS graphs echo in customer facing site' "
            "&& git push origin main"
        )
        second = (
            "./redeploy.sh && git status && git add . && git commit -m "
            "'CMS upgrades. Major upgrades to graph table import and line draw functionality' "
            "&& git push origin main"
        )
        prompt = "user1@xps - $ "
        self.terminal._rebuild_screen(163, 5)
        self.feed_now(prompt + second)
        self.feed_now("\x1b[A\x1b[C" + first[:-3] + "\x1b[5")
        self.feed_now("P" + "\x1b[C" * 7)

        rendered = [line.rstrip() for line in self.terminal.document().toPlainText().splitlines()[-5:]]
        split = self.terminal.screen.columns - len(prompt)
        self.assertEqual(rendered[:2], [prompt + first[:split], first[split:]])
        self.assertNotIn("main main", "\n".join(rendered))

    def test_single_character_delete_does_not_punch_holes_in_wrapped_input(self):
        # A bracketed multiline paste is edited by Readline with repeated
        # CSI 1 P sequences.  Those are ordinary character deletes, not the
        # multi-cell history-recall redraws that need our tail repair.
        self.terminal._rebuild_screen(20, 5)
        self.feed_now("P> " + "abcdefghijklmnopqrstuvwxyz1234567890")
        for _ in range(4):
            self.feed_now("\x1b[D\x1b[1P")

        rows = self.terminal.document().toPlainText().splitlines()[-5:]
        self.assertEqual(rows[0].rstrip(), "P> abcdefghijklmnopq")
        self.assertEqual(rows[1].rstrip(), "rstuvwxyz123456")
        self.assertNotIn(" ", rows[1].rstrip())

    def test_bash_quoted_insert_backspace_keeps_the_command_prefix(self):
        # Captured from Bash 5 + Readline: Ctrl+V then Backspace displays
        # ``^?`` via ICH, then removes that two-cell representation with
        # Backspace Backspace CSI 2 P. This is not a history redraw.
        self.terminal._rebuild_screen(20, 5)
        self.feed_now("\x1b[?2004hPROMPT> abcdefg")
        self.feed_now("\x08" * 7)
        self.feed_now("\x1b[2@^?")
        self.feed_now("\x08\x08\x1b[2P")

        self.assertEqual(
            self.terminal.document().toPlainText().splitlines()[-5][0:15],
            "PROMPT> abcdefg",
        )

    def test_captured_windows_readline_delete_does_not_retain_wrapped_tail(self):
        # Captured from the maximized Windows terminal. pyte previously left
        # the final " main" of the older command after CSI 21 P shortened it.
        first = (
            "echo./redeploy.sh && git status && git add . && git commit -m "
            "'filter parameter marker on HTMLJS graphs echo in customer facing site' "
            "&& git push origin main"
        )
        second = (
            "./redeploy.sh && git status && git add . && git commit -m "
            "'CMS upgrades. Major upgrades to graph table import and line draw functionality' "
            "&& git push origin main"
        )
        prompt = "                  "
        self.terminal._rebuild_screen(167, 46)
        self.feed_now(prompt + second)
        self.feed_now(
            "\x1b[A\r" + "\x1b[C" * len(prompt) + first[:83] + "\x1b[21P" + first[83:]
        )

        rendered = [line.rstrip() for line in self.terminal.document().toPlainText().splitlines()[-46:]]
        first_row_length = self.terminal.screen.columns - len(prompt)
        command_row = rendered.index(prompt + first[:first_row_length])
        self.assertEqual(rendered[command_row:command_row + 2], [
            prompt + first[:first_row_length],
            first[first_row_length:],
        ])
        self.assertNotIn("main main", "\n".join(rendered))

    def test_wrapped_history_redraw_keeps_the_existing_prompt_row(self):
        prompt = " user1@xps  ~  $  "
        styled_prompt = "\x1b[1;37;44m" + prompt + "\x1b[0m"
        long_command = (
            "./redeploy.sh && git status && git add . && git commit -m "
            "'Added option to duplicate an object (product/series/product-type). Enhanced the CMS and made the pages better looking "
            "but compatible with the capabilities of the CMS. Also added some features to the enquiry email functionality. More of this "
            "to come soon-ish.' && git push origin main" + "                "
        )
        short_command = "cds Documents/fan_graphs_website"
        self.terminal._rebuild_screen(167, 46)
        self.terminal.screen.cursor.y = 37
        self.feed_now(styled_prompt + short_command)
        self.feed_now("\r" + "\x1b[C" * len(prompt) + long_command)
        self.assertEqual(self.terminal.screen.cursor.y, 39)
        history_before = [dict(line) for line in self.terminal.screen.history.top]

        self.feed_now(
            "\x1b[A" * 3 + "\r" + "\x1b[C" * len(prompt) + "\x1b[50P" + short_command
        )

        rows = self.terminal.document().toPlainText().splitlines()[-46:]
        # Bash targets row 36. The renderer corrects that view without
        # rewriting the escape sequence or mutating pyte's scrollback.
        row = rows[36].rstrip()
        self.assertEqual(row, prompt + short_command)
        self.assertEqual(rows[37].rstrip(), "")
        self.assertEqual([dict(line) for line in self.terminal.screen.history.top], history_before)

        # A following history entry can reuse the cells that were blanked
        # while shortening the previous one. Its text must not inherit gaps
        # from that temporary correction.
        self.feed_now("\r" + "\x1b[C" * len(prompt) + long_command)
        rows = self.terminal.document().toPlainText().splitlines()[-46:]
        first_width = self.terminal.screen.columns - len(prompt)
        self.assertEqual(rows[36].rstrip(), prompt + long_command[:first_width])
        self.assertEqual(rows[37].rstrip(), long_command[first_width:first_width + self.terminal.screen.columns])
        self.assertEqual(rows[38].rstrip(), long_command[first_width + self.terminal.screen.columns:].rstrip())

    def test_small_interactive_output_is_not_artificially_delayed(self):
        self.assertEqual(self.terminal.interactive_render_delay_ms, 0)
        self.assertLess(SshBackend.ssh_poll_interval, 0.01)
        self.terminal.feed("x")
        self.assertEqual(self.terminal.screen.cursor.x, 1)
        self.assertFalse(self.terminal._input_pending)

    def test_saved_password_auto_connect_preference_is_safe(self):
        self.assertTrue(should_auto_connect_saved_password(True, "secret"))
        self.assertFalse(should_auto_connect_saved_password(False, "secret"))
        self.assertFalse(should_auto_connect_saved_password(True, ""))
        self.assertEqual(ssh_auth_probe_options("secret"), {"look_for_keys": False, "allow_agent": False})
        self.assertEqual(ssh_auth_probe_options(""), {"look_for_keys": True, "allow_agent": True})

    def test_remote_preload_batches_directories_into_one_ssh_command(self):
        class Backend:
            def __init__(self):
                self.calls = []

            def run_command(self, command, timeout=5):
                self.calls.append(command)
                payload = {
                    "/home/a": {"entries": [["z", False, 1]]},
                    "/home/b": {"entries": [["sub", True, 0]]},
                }
                import json
                return 0, json.dumps(payload), ""

        backend = Backend()
        loader = RemoteFileLoader(backend)
        result = loader._list_entries_many(["/home/a", "/home/b"])
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(set(result), {"/home/a", "/home/b"})
        self.assertEqual(result["/home/a"], [("z", False, 1)])
        self.assertGreaterEqual(loader.directory_cache_ttl, 60.0)
        loader._directory_cache["/home/a"] = (__import__("time").monotonic(), result["/home/a"])
        delivered = []
        loader.loaded.connect(lambda path, entries: delivered.append((path, entries)))
        loader.load("/home/a")
        self.app.processEvents()
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(delivered[0][0], "/home/a")

    def test_urgent_remote_preload_bypasses_the_passive_queue(self):
        class Backend:
            pass

        loader = RemoteFileLoader(Backend())
        calls = []
        loader._start_urgent_preload = lambda paths, generation: calls.append((tuple(paths), generation))
        generation = loader.prioritize()
        loader.preload(["/new/a", "/new/b"], generation=generation, urgent=True)
        self.assertEqual(calls, [(('/new/a', '/new/b'), generation)])
        self.assertTrue(loader._load_queue.empty())
        loader.stop()

    def test_stale_cached_remote_listing_is_not_delivered_after_navigation(self):
        class Backend:
            pass

        import time
        loader = RemoteFileLoader(Backend())
        loader._directory_cache["/old"] = (time.monotonic(), [("file", False, 4)])
        delivered = []
        loader.loaded.connect(lambda path, entries: delivered.append(path))
        loader.load("/old")
        loader.prioritize()
        self.app.processEvents()
        self.assertEqual(delivered, [])
        loader.stop()

    def test_remote_root_discovery_can_prime_the_first_listing(self):
        class Backend:
            def __init__(self):
                self.calls = []

            def run_command(self, command, timeout=5):
                self.calls.append(command)
                import json
                return 0, json.dumps({"path": "/home/user", "entries": [["docs", True, 0]]}), ""

        backend = Backend()
        loader = RemoteFileLoader(backend)
        roots = []
        loader.root_ready.connect(roots.append)
        loader._discover_root()
        self.assertEqual(len(backend.calls), 1)
        self.assertTrue(loader.has_fresh_cache("/home/user"))
        self.assertEqual(roots, ["/home/user"])
        loader.stop()

    def test_foreground_directory_reads_reuse_one_sftp_channel(self):
        class Attr:
            filename = "folder"
            st_mode = 0o040755
            st_size = 0

        class Sftp:
            def listdir_attr(self, path):
                return [Attr()]

            def close(self):
                pass

        class Client:
            def __init__(self):
                self.opens = 0

            def open_sftp(self):
                self.opens += 1
                return Sftp()

        class Backend:
            def __init__(self):
                self.client = Client()

        backend = Backend()
        loader = RemoteFileLoader(backend)
        self.assertEqual(loader._list_entries_persistent("/one", foreground=True), [("folder", True, 0)])
        self.assertEqual(loader._list_entries_persistent("/two", foreground=True), [("folder", True, 0)])
        self.assertEqual(backend.client.opens, 1)
        loader.stop()

    def test_batched_preload_publishes_each_directory_record_immediately(self):
        class Backend:
            def run_command_lines(self, command, on_line, timeout=5):
                on_line('{"path":"/first","entries":[["a",true,0]]}\n')
                on_line('{"path":"/second","entries":[["b",false,3]]}\n')
                return 0, ""

        loader = RemoteFileLoader(Backend())
        delivered = []
        loader.loaded.connect(lambda path, entries: delivered.append(path))
        generation = loader.prioritize()
        self.assertTrue(loader._stream_entries_many(["/first", "/second"], generation))
        self.assertEqual(delivered, ["/first", "/second"])
        loader.stop()

    def test_expanded_subdirectory_becomes_preload_centre_non_hidden_first(self):
        class Loader:
            def __init__(self):
                self._generation = 0
                self.calls = []

            def prioritize(self):
                self._generation += 1
                return self._generation

            def preload(self, paths, generation=None, urgent=False):
                self.calls.append((list(paths), generation, urgent))

        tree = RemoteFileTree()
        loader = Loader()
        tree.loader = loader
        tree.show_hidden = True
        target = QTreeWidgetItem(["target", ""])
        target.setData(0, tree.PATH_ROLE, "/target")
        target.setData(0, tree.IS_DIR_ROLE, True)
        target.setData(0, tree.LOADED_ROLE, True)
        for name in (".hidden", "visible"):
            child = QTreeWidgetItem([name, ""])
            child.setData(0, tree.PATH_ROLE, "/target/" + name)
            child.setData(0, tree.IS_DIR_ROLE, True)
            target.addChild(child)
        tree._expanded(target)
        self.assertEqual(loader.calls, [(["/target/visible", "/target/.hidden"], 1, True)])
        tree.deleteLater()

    def test_properties_dialog_preserves_special_permission_bits(self):
        mode = 0o104755 | 0o4000
        dialog = PermissionsDialog(
            "/tmp/tool",
            mode,
            1234,
            {"Type": "File", "Owner": "1000", "Group": "1000", "Last modified": "Today"},
        )
        self.assertEqual(dialog.permission_mode(), mode & 0o7777)
        self.assertIn("1,234 bytes", dialog.general_details.text())
        self.assertIn("Today", dialog.date_details.text())
        dialog.deleteLater()

    def test_external_edit_fingerprint_detects_content_changes(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "edit.txt"
            path.write_text("before", encoding="utf-8")
            before = file_fingerprint(path)
            path.write_text("after", encoding="utf-8")
            self.assertNotEqual(before, file_fingerprint(path))

    def test_multi_delete_prunes_duplicates_and_nested_children(self):
        targets = [
            ("/home/user/folder/file.txt", False),
            ("/home/user/folder", True),
            ("/home/user/other.txt", False),
            ("/home/user/other.txt", False),
        ]
        self.assertEqual(
            prune_nested_delete_targets(targets, remote=True),
            [("/home/user/folder", True), ("/home/user/other.txt", False)],
        )

    def test_split_terminal_focus_controls_active_session(self):
        window = MainWindow()
        terminals = []
        try:
            for title in ("One", "Two"):
                terminal = TerminalWidget()
                terminal.mode = "local"
                terminal.remote_backend = None
                terminal.profile = None
                terminal.focus_activated.connect(lambda t=terminal: window._activate_terminal_from_focus(t))
                window.session_tabs.addTab(terminal, title)
                terminals.append(terminal)
            window.session_tabs.setCurrentWidget(terminals[1])
            window._set_active_session_pane(window.session_tabs)
            window.terminal = terminals[1]
            generation = window.session_generation
            window._pane_tab_clicked(window.session_tabs, window.session_tabs.currentIndex())
            self.assertEqual(window.session_generation, generation)
            single_pane_icon = window.action_side_by_side.icon().cacheKey()
            self.assertEqual(window.action_side_by_side.text(), "Side by Side\nSplit")
            window.split_active_terminal_right()
            self.assertEqual(len(window.session_panes), 2)
            self.assertEqual(window.session_splitter.count(), 2)
            self.assertTrue(window.action_side_by_side.isChecked())
            self.assertNotEqual(window.action_side_by_side.icon().cacheKey(), single_pane_icon)
            self.assertEqual(window.action_side_by_side.text(), "Side by Side\nMerge")
            self.assertIs(window.active_session(), terminals[1])

            window.show()
            terminals[0].focus_activated.emit()
            self.app.processEvents()
            self.assertIs(window.active_session(), terminals[0])
            self.assertIs(window._active_session_pane, window.session_tabs)
            window.close_session_tab(0, window.session_tabs)
            self.assertIs(window.active_session(), terminals[1])
        finally:
            window.close()

    def test_transfer_split_button_and_three_file_options_exist(self):
        window = MainWindow(show_early=True)
        try:
            self.assertTrue(window.isVisible())
            self.assertIs(window.centralWidget(), window.main_splitter)
            self.assertTrue(window.transfer_button.isEnabled() is False)
            self.assertEqual(len(window.follow_boxes), 4)
            self.assertEqual(len(window.hidden_boxes), 2)
            self.assertEqual(window.transfer_button.popupMode(), window.transfer_button.ToolButtonPopupMode.InstantPopup)
            self.assertTrue(window.connection_progress.isHidden())
            self.assertEqual(window.local_tree.selectionMode(), QAbstractItemView.SelectionMode.ExtendedSelection)
            self.assertEqual(window.remote_tree.selectionMode(), QAbstractItemView.SelectionMode.ExtendedSelection)
            ribbon_actions = [
                button.defaultAction()
                for button in window.ribbon_terminal_group.findChildren(type(window.transfer_button))
                if button.defaultAction() is not None
            ]
            self.assertIn(window.action_side_by_side, ribbon_actions)
            self.assertTrue(window.action_side_by_side.isCheckable())
            self.assertTrue(hasattr(window, "action_deletion_log"))
            file_activity_actions = [
                button.defaultAction()
                for button in window.ribbon_file_activity_group.findChildren(type(window.transfer_button))
                if button.defaultAction() is not None
            ]
            self.assertIn(window.action_deletion_log, file_activity_actions)
            for toolbar in (window.local_file_toolbar, window.remote_file_toolbar):
                toolbar_actions = [
                    button.defaultAction()
                    for button in toolbar.findChildren(type(window.transfer_button))
                    if button.defaultAction() is not None
                ]
                self.assertNotIn(window.action_deletion_log, toolbar_actions)
            self.assertIn(
                window.transfer_button,
                window.remote_file_toolbar.findChildren(type(window.transfer_button)),
            )
            self.assertTrue(hasattr(window, "action_about"))
            self.assertEqual(window.action_about.text(), "About and\nUpdates")
            self.assertTrue(window._release_is_newer("v0.1.1"))
            self.assertFalse(window._release_is_newer(APP_VERSION))
            menu = window.build_file_menu("C:/tmp/example.txt", False, False)
            self.assertIn("Open in Default Application", [action.text() for action in menu.actions()])
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
