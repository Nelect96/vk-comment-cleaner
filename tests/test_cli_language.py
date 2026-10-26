"""Prevent Russian-only terminal prompts from returning in public releases.

The VK DOM still has Russian labels. Those must NOT be translated because
they are part of parsing the VK website, not terminal user interaction.
"""
import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI_MODULES = [
    "vkmgr/playwright_batch.py",
    "vkmgr/playwright_delete.py",
    "vkmgr/browser_assistant.py",
    "vkmgr/queue_plan.py",
    "vkmgr/queue_progress.py",
    "vkmgr/port_diagnostics.py",
]
SCRIPTS = [
    "scripts/install.sh",
    "scripts/app.sh",
    "scripts/start.sh",
    "scripts/cleanup.sh",
    "scripts/review-not-found-macos.sh",
]
CYRILLIC = re.compile("[\\u0400-\\u04ff]")


class EnglishCliTests(unittest.TestCase):
    def test_cli_module_python_syntax_and_no_cyrillic_user_prompts(self):
        for name in CLI_MODULES:
            with self.subTest(file=name):
                source = (ROOT / name).read_text(encoding="utf-8")
                tree = ast.parse(source, filename=name)
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    if not isinstance(node.func, ast.Name):
                        continue
                    if node.func.id not in {"print", "input", "ask", "echo"}:
                        continue
                    for subnode in ast.walk(node):
                        if isinstance(subnode, ast.Constant) and isinstance(subnode.value, str):
                            self.assertFalse(
                                CYRILLIC.search(subnode.value),
                                f"{name}:{subnode.lineno} contains a Cyrillic user-visible message",
                            )

    def test_installers_and_interactive_confirmation_are_ascii(self):
        for name in SCRIPTS:
            with self.subTest(file=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIsNone(CYRILLIC.search(text), name)

        batch = (ROOT / "vkmgr/playwright_batch.py").read_text(encoding="utf-8")
        browser = (ROOT / "vkmgr/browser_assistant.py").read_text(encoding="utf-8")
        self.assertIn("DELETE BATCH ", batch)
        self.assertIn("CONFIRM DELETED ", batch)
        self.assertIn('"STOP"', batch)
        self.assertIn('"DELETED"', browser)
        self.assertIn('"YES"', browser)

    def test_deletion_marker_and_expansion_regex_still_read_vks_actual_dom(self):
        source = (ROOT / "vkmgr/playwright_batch.py").read_text(encoding="utf-8")
        self.assertIn("explicit_deleted_marker", source)
        self.assertIn("_EXPAND_LABEL", source)
        self.assertIn("Восстановить", source)


if __name__ == "__main__":
    unittest.main()
