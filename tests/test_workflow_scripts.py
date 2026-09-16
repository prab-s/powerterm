"""Exercise push safety with disposable local repositories; never contact GitHub."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name, directory=SCRIPTS):
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PushSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.remote = self.base / "origin.git"
        self.repo.mkdir()
        self.command("init", "-b", "main")
        self.command("config", "user.name", "Workflow Test")
        self.command("config", "user.email", "test@example.invalid")
        (self.repo / "main.py").write_text("# fixture\n")
        self.command("add", ".")
        self.command("commit", "-m", "Initial")
        self.command("init", "--bare", self.remote)
        self.command("remote", "add", "origin", self.remote)
        self.command("push", "origin", "main")
        self.push = load("push")
        self.push.ROOT = self.repo
        self.original_cwd = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, self.original_cwd)
        self.real_git = self.push.git

    def command(self, *args):
        return subprocess.check_output(["git", *map(str, args)], cwd=self.repo,
                                       stderr=subprocess.STDOUT, text=True).strip()

    def local_git(self, *args, **kwargs):
        # Only fake URL inspection; actual fetch/commit/push use the local bare repo.
        if args[:2] == ("remote", "get-url"):
            return "https://github.com/prab-s/powerterm.git\n"
        # Keep expected fixture commits and local pushes out of the test log.
        kwargs["capture"] = True
        return self.real_git(*args, **kwargs)

    def invoke(self, answer="yes", message="Test change"):
        with patch.object(self.push, "git", side_effect=self.local_git) as calls, \
                patch.object(sys, "argv", ["push.py", message]), \
                patch("builtins.input", return_value=answer), contextlib.redirect_stdout(io.StringIO()):
            result = self.push.main()
            self.assertEqual(result, 2 if answer != "yes" else None)
            return calls.call_args_list

    def test_dirty_commit_and_clean_retry_never_make_empty_commit(self):
        (self.repo / "new.txt").write_text("change\n")
        calls = self.invoke(message="A message with $ and ` literal characters")
        head = self.command("rev-parse", "HEAD")
        self.assertEqual(self.command("rev-parse", "FETCH_HEAD"), self.command("rev-parse", "HEAD~1"))
        self.assertEqual(self.command("ls-remote", "origin", "main").split()[0], head)
        self.assertEqual(self.command("log", "-1", "--format=%s"), "A message with $ and ` literal characters")
        self.invoke()
        self.assertEqual(self.command("rev-parse", "HEAD"), head)
        push_call = next(c.args for c in calls if "push" in c.args)
        self.assertIn("--no-force", push_call)
        self.assertEqual(push_call[-2:], ("origin", "refs/heads/main:refs/heads/main"))

    def test_cancellation_leaves_index_and_head_unchanged(self):
        before = self.command("rev-parse", "HEAD")
        (self.repo / "new.txt").write_text("change\n")
        self.invoke(answer="no")
        self.assertEqual(self.command("rev-parse", "HEAD"), before)
        self.assertEqual(self.command("diff", "--cached"), "")

    def test_wrong_remote_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "origin must"):
            self.push.validate()

    def test_wrong_branch_is_rejected(self):
        self.command("switch", "-c", "topic")
        with self.assertRaisesRegex(RuntimeError, "Switch to main"):
            self.invoke()

    def test_wrong_working_repository_is_rejected(self):
        os.chdir(self.remote)
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke()

    def test_remote_ahead_is_rejected_before_staging(self):
        (self.repo / "remote-change").write_text("remote\n")
        self.command("add", ".")
        self.command("commit", "-m", "Remote update")
        self.command("push", "origin", "main")
        self.command("reset", "--hard", "HEAD~1")
        (self.repo / "local-change").write_text("local\n")
        with self.assertRaisesRegex(RuntimeError, "behind or diverged"):
            self.invoke()
        self.assertEqual(self.command("diff", "--cached"), "")


class BuildEnvironmentTests(unittest.TestCase):
    def test_cross_os_builds_are_unavailable(self):
        build = load("build")
        with patch.object(build.platform, "system", return_value="Windows"):
            for target in ("linux", "appimage", "flatpak", "deb"):
                self.assertIn("native Linux", build.availability(target, "25.08"))
        with patch.object(build.platform, "system", return_value="Linux"):
            self.assertIn("native Windows", build.availability("windows", "25.08"))

    def test_missing_appimage_tool_is_reported(self):
        build = load("build")
        with patch.object(build.platform, "system", return_value="Linux"), \
                patch.object(build.shutil, "which", return_value=None):
            self.assertIn("appimagetool", build.availability("appimage", "25.08"))


class CombinedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = load("workflow", SCRIPTS.parent)

    def exercise(self, failure=None):
        commands = []

        def record(*args):
            commands.append(args)
            if len(commands) == failure:
                raise subprocess.CalledProcessError(2 if failure == 2 else 1, args)

        with patch.object(self.workflow, "prepare_environment", return_value=Path("venv-python")), \
                patch.object(self.workflow.shutil, "which", return_value="git"), \
                patch.object(self.workflow, "run", side_effect=record), \
                contextlib.redirect_stdout(io.StringIO()):
            if failure:
                with self.assertRaises(subprocess.CalledProcessError):
                    self.workflow.main(["A literal $message"])
            else:
                self.workflow.main(["A literal $message"])
        return commands

    def test_success_runs_tests_push_then_build_menu(self):
        commands = self.exercise()
        self.assertEqual([Path(c[1]).name for c in commands], ["test.py", "push.py", "build.py"])
        self.assertEqual(commands[1][2], "A literal $message")
        self.assertEqual(len(commands[2]), 2)  # No target means interactive selection.

    def test_failed_tests_never_push_or_build(self):
        self.assertEqual(len(self.exercise(failure=1)), 1)

    def test_cancelled_or_failed_push_never_builds(self):
        self.assertEqual(len(self.exercise(failure=2)), 2)

    def test_dependency_failure_never_runs_tests_or_push(self):
        with patch.object(self.workflow.shutil, "which", return_value="git"), \
                patch.object(self.workflow, "prepare_environment", side_effect=RuntimeError("missing dependency")), \
                patch.object(self.workflow, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "missing dependency"):
                self.workflow.main([])
            run.assert_not_called()
