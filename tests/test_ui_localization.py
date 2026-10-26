"""UI internationalization must not weaken ownership or deletion safeguards."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "vkmgr" / "index.html").read_text(encoding="utf-8")
SERVER = (ROOT / "vkmgr" / "server.py").read_text(encoding="utf-8")


class LocalizedInterfaceTests(unittest.TestCase):
    def test_languages_and_persistence(self):
        self.assertIn('<html lang="en">', HTML)
        self.assertIn('id="uiLanguage"', HTML)
        self.assertIn('<option value="en">English</option>', HTML)
        self.assertIn('<option value="ru">Русский</option>', HTML)
        self.assertIn("vkcm.ui.language", HTML)
        self.assertIn('navigator.language.toLowerCase().startsWith("ru")', HTML)
        self.assertIn('document.documentElement.lang=current', HTML)

    def test_only_text_nodes_and_safe_attributes_are_translated(self):
        self.assertIn('const attributes=["placeholder","aria-label","title"]', HTML)
        self.assertIn("document.createTreeWalker", HTML)
        self.assertIn("originalTexts=new WeakMap()", HTML)
        self.assertIn('["SCRIPT","STYLE","TEXTAREA"]', HTML)
        self.assertNotIn("innerHTML=", HTML)

    def test_international_confirmation_requires_ascii_without_protocol_change(self):
        self.assertIn("if(confirmation!=='DELETE')return;", HTML)
        self.assertIn("confirmation:'УДАЛИТЬ'", HTML)
        self.assertIn('data.get("confirmation") != "УДАЛИТЬ"', SERVER)
        self.assertIn("confirmation:'Я УДАЛИЛ В VK'", HTML)
        self.assertIn('data.get("confirmation") != "Я УДАЛИЛ В VK"', SERVER)

    def test_deletion_messages_keep_unknown_outcomes_visible(self):
        for text in ("Missing from VK UI", "User confirmed removal",
                     "Comment not found (no click)", "server not verified",
                     "VK API 1051", "not independent proof of deletion"):
            with self.subTest(text=text):
                self.assertIn(text, HTML)

    def test_no_external_translation_service(self):
        self.assertIn('const messages=new Map(', HTML)
        self.assertNotIn("translate.googleapis.com", HTML)
        self.assertNotIn("api.openai.com", HTML)

    def test_two_inline_scripts_and_no_unescaped_closing_script_tokens(self):
        self.assertEqual(len(re.findall(r"<script>", HTML)), 2)
        self.assertEqual(len(re.findall(r"</script>", HTML)), 2)


if __name__ == "__main__":
    unittest.main()
