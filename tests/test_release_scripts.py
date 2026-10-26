"""Offline checks for safe newcomer installer and publication scripts."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "scripts/install.sh",
    "scripts/app.sh",
    "scripts/start.sh",
    "scripts/cleanup.sh",
    "scripts/review-not-found-macos.sh",
)


@unittest.skipUnless(shutil.which("bash"), "Bash not installed")
class ShellReleaseTests(unittest.TestCase):
    def test_all_release_scripts_have_valid_bash_syntax(self):
        for relative in SCRIPTS + (("scripts/publish-clean.sh",) if (ROOT / "scripts/publish-clean.sh").is_file() else ()):
            with self.subTest(script=relative):
                path = ROOT / relative
                self.assertTrue(path.is_file(), relative)
                completed = subprocess.run(
                    ["bash", "-n", str(path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=8,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_mac_python_bootstrap_is_version_safe(self):
        installer = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
        launcher = (ROOT / "scripts/app.sh").read_text(encoding="utf-8")
        self.assertIn("sys.version_info >= (3, 10)", installer)
        self.assertIn("python@3.13", installer)
        self.assertIn("brew_executable", installer)
        self.assertIn("VKCM_NONINTERACTIVE", installer)
        self.assertIn(".vkcm-ready-0.4.0", installer)
        self.assertIn(".vkcm-ready-0.4.0", launcher)
        self.assertNotIn("sudo rm", installer)
        self.assertNotIn("sudo rm", launcher)

    def test_new_user_helpers_are_explicit_and_local_only(self):
        app = (ROOT / "scripts/app.sh").read_text(encoding="utf-8")
        cleanup = (ROOT / "scripts/cleanup.sh").read_text(encoding="utf-8")
        publishing_path = ROOT / "scripts/publish-clean.sh"
        publishing = (publishing_path.read_text(encoding="utf-8")
                      if publishing_path.is_file() else "")
        self.assertIn("scripts/install.sh", app)
        self.assertIn("choose_archive", app)
        self.assertIn("--auto --execute", cleanup)
        self.assertIn("VKCM_PORT", cleanup)
        if publishing_path.is_file():
            self.assertIn("gh repo create", publishing)
            self.assertIn("--private", publishing)
            self.assertIn("--visibility public", publishing)
            self.assertIn("git archive", publishing)
            self.assertIn("PUBLISH CLEAN REPOSITORY", publishing)


if __name__ == "__main__":
    unittest.main()
