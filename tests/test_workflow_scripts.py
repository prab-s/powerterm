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
import zipfile
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
            self.assertIsNone(build.availability("windows", "25.08"))
            self.assertIn("ZIP", build.target_label("windows"))

    def test_missing_appimage_tool_is_reported(self):
        build = load("build")
        with patch.object(build.platform, "system", return_value="Linux"), \
                patch.object(build.shutil, "which", return_value=None):
            self.assertIn("appimagetool", build.availability("appimage", "25.08"))

    def test_menu_accepts_multiple_numbers_and_rejects_unavailable_targets(self):
        build = load("build")
        available = dict.fromkeys(build.TARGETS)
        available["flatpak"] = "missing SDK"
        self.assertEqual(build.parse_selection("1, 2,5,1", available), ["windows", "linux", "deb"])
        self.assertEqual(build.parse_selection("windows deb", available), ["windows", "deb"])
        self.assertEqual(build.parse_selection("all", available), ["windows", "linux", "appimage", "deb", "msi"])
        self.assertEqual(build.parse_selection("", available), [])
        with self.assertRaisesRegex(ValueError, "missing SDK"):
            build.parse_selection("1,4", available)
        with self.assertRaisesRegex(ValueError, "Unknown"):
            build.parse_selection("7", available)

    def test_menu_retries_invalid_selection_and_confirms(self):
        build = load("build")
        with patch("builtins.input", side_effect=["oops", "1,2", "no", "1", "yes"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build.choose_targets(dict.fromkeys(build.TARGETS)), ["windows"])

    def test_version_menu_retries_invalid_msi_version(self):
        build = load("build")
        with patch("builtins.input", side_effect=["1.2", "1.2.3"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build.choose_version(["windows", "msi"]), "1.2.3")

    def test_windows_zip_is_self_contained_source_kit_without_repository_data(self):
        build = load("build")
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(build.platform, "system", return_value="Linux"), \
                patch.object(build, "freeze", side_effect=AssertionError("must not cross-compile")):
            output = build.package("windows", Path(folder), "1.2.3", "25.08", {})
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                prefix = "PowerTerm-1.2.3-windows-build-kit/"
                names = {name.removeprefix(prefix) for name in archive.namelist()}
                self.assertEqual(names, {"main.py", "powerterm.svg", "LICENSE", "requirements.txt",
                    "requirements-build.txt", "scripts/build.py", "scripts/windows_setup.py",
                    "scripts/build-windows.bat", "scripts/console_ui.py", "scripts/windows_msi.py", "WINDOWS-MSI.md",
                    "BUILD-WINDOWS.bat", "START-HERE.txt", "PACKAGE-VERSION.txt", "PACKAGE-TARGET.txt"})
                self.assertEqual(archive.read(prefix + "LICENSE"), (SCRIPTS.parent / "LICENSE").read_bytes())
                self.assertEqual(archive.read(prefix + "PACKAGE-VERSION.txt"), b"1.2.3\n")

    def test_linux_standalone_artifact_includes_the_release_version(self):
        build = load("build")
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(build.platform, "system", return_value="Linux"), \
                patch.object(build.platform, "machine", return_value="x86_64"):
            binary = Path(folder) / "powerterm"
            binary.write_text("fixture executable\n")
            output = build.package("linux", Path(folder), "1.2.3", "25.08", {True: binary})
            self.assertEqual(output.name, "PowerTerm-1.2.3-x86_64")
            self.assertEqual(output.read_text(), "fixture executable\n")


class CombinedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = load("workflow", SCRIPTS.parent)

    def exercise(self, failure=None, choice="1"):
        commands = []

        def record(*args):
            commands.append(args)
            if len(commands) == failure:
                raise subprocess.CalledProcessError(2 if failure == 2 else 1, args)

        with patch.object(self.workflow, "prepare_environment", return_value=Path("venv-python")), \
                patch.object(self.workflow.shutil, "which", return_value="git"), \
                patch.object(self.workflow, "run", side_effect=record), \
                patch("builtins.input", return_value=choice), \
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

    def test_skip_push_goes_directly_from_tests_to_build_menu(self):
        self.assertEqual([Path(c[1]).name for c in self.exercise(choice="2")], ["test.py", "build.py"])

    def test_finish_after_tests_does_not_push_or_build(self):
        self.assertEqual([Path(c[1]).name for c in self.exercise(choice="0")], ["test.py"])

    def test_dependency_failure_never_runs_tests_or_push(self):
        with patch.object(self.workflow.shutil, "which", return_value="git"), \
                patch.object(self.workflow, "prepare_environment", side_effect=RuntimeError("missing dependency")), \
                patch.object(self.workflow, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "missing dependency"):
                self.workflow.main([])
            run.assert_not_called()


class WindowsInstallerTests(unittest.TestCase):
    def test_versions_use_only_msi_comparable_fields(self):
        msi = load("windows_msi")
        for version in ("0.1.0", "1.2.3", "255.255.65535"):
            msi.validate_version(version)
        for version in ("1.0", "1.2.3.4", "1.2.3beta", "256.0.0", "0.0.65536", "01.2.3"):
            with self.assertRaises(ValueError):
                msi.validate_version(version)

    def test_upgrade_identity_and_components_stay_stable_without_owning_config(self):
        import xml.etree.ElementTree as ET
        msi = load("windows_msi")
        ns = {"w": msi.NS}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundle = root / "bundle"
            (bundle / "_internal").mkdir(parents=True)
            (bundle / "PowerTerm.exe").write_bytes(b"fixture")
            (bundle / "_internal/library.dll").write_bytes(b"library")
            trees = []
            for version in ("0.1.0", "0.1.1"):
                source = msi.author_installer(bundle, root, version, "x64", SCRIPTS.parent / "LICENSE")
                trees.append(ET.parse(source))
            for tree in trees:
                package = tree.find("w:Package", ns)
                self.assertEqual(package.get("UpgradeCode"), msi.UPGRADE_CODE)
                self.assertIsNone(package.get("ProductCode"))  # WiX generates a new one.
                self.assertEqual(package.get("Scope"), "perMachine")
                upgrade = package.find("w:MajorUpgrade", ns)
                self.assertEqual(upgrade.get("Schedule"), "afterInstallInitialize")
                self.assertEqual(upgrade.get("AllowSameVersionUpgrades"), "yes")
                self.assertIsNotNone(upgrade.get("DowngradeErrorMessage"))
                self.assertIsNotNone(tree.find(".//w:Shortcut", ns))
                self.assertIsNone(tree.find(".//w:RemoveFile", ns))
                self.assertIsNone(tree.find(".//w:RegistryValue", ns))
                self.assertEqual(len(tree.findall(".//w:File", ns)), 2)
            components = lambda tree: [(c.get("Id"), c.get("Guid")) for c in tree.findall(".//w:Component", ns)]
            self.assertEqual(components(trees[0]), components(trees[1]))
            (bundle / "config.json").write_text('{}')
            with self.assertRaisesRegex(ValueError, "configuration"):
                msi.author_installer(bundle, root, "0.1.2", "x64", SCRIPTS.parent / "LICENSE")

    def test_msi_uses_directory_bundle_and_portable_keeps_single_file(self):
        build = load("build")
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            with patch.object(build.platform, "system", return_value="Windows"), \
                    patch.object(build, "freeze", return_value=work / "bundle") as freeze, \
                    patch.object(build.windows_msi, "build_msi", return_value=work / "app.msi"):
                build.package("msi", work, "0.1.0", "25.08", {})
                self.assertFalse(freeze.call_args.args[1])
                build.package("windows", work, "0.1.0", "25.08", {})
                self.assertTrue(freeze.call_args.args[1])
            with patch.object(build.platform, "system", return_value="Windows"), patch.object(build, "run"):
                self.assertEqual(build.freeze(work, True).name, "PowerTerm-Portable.exe")
                self.assertEqual(build.freeze(work, False).name, "PowerTerm")

    def test_linux_msi_kit_selects_msi_on_windows(self):
        build = load("build")
        with tempfile.TemporaryDirectory() as folder, patch.object(build.platform, "system", return_value="Linux"):
            result = build.package("msi", Path(folder), "0.1.1", "25.08", {})
            with zipfile.ZipFile(result) as archive:
                prefix = "PowerTerm-0.1.1-windows-msi-build-kit/"
                self.assertEqual(archive.read(prefix + "PACKAGE-TARGET.txt"), b"msi\n")
                self.assertIn(prefix + "scripts/windows_msi.py", archive.namelist())
                self.assertIn(prefix + "WINDOWS-MSI.md", archive.namelist())
