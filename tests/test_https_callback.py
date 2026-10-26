"""Public distribution regression tests. No user-specific OAuth defaults."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class PublicReleaseDefaults(unittest.TestCase):
    def test_login_has_no_preconfigured_third_party_application(self):
        html = (ROOT / "vkmgr/index.html").read_text(encoding="utf-8")
        self.assertIn('id="vkidAppId"', html)
        self.assertIn('id="vkidRedirect"', html)
        self.assertIn('id="vkidAppId" inputmode="numeric" placeholder="App ID своего приложения" value=""', html)
        self.assertIn('id="vkidRedirect" placeholder="Redirect URL, зарегистрированный в VK ID" value=""', html)
        self.assertNotIn("vkcm-callback.html", html)

    def test_no_hardcoded_external_callback_or_personal_server(self):
        for rel in ("vkmgr/server.py", "vkmgr/playwright_batch.py",
                    "vkmgr/oauth.py", "vkmgr/index.html"):
            with self.subTest(path=rel):
                contents = (ROOT / rel).read_text(encoding="utf-8")
                self.assertNotIn("vkcm-callback.html", contents)
                self.assertNotIn("placeholder-app-id-is-forbidden", contents)
                self.assertNotIn("example-absolute-user-path", contents)

    def test_no_personal_site_deployment_in_public_tree(self):
        self.assertFalse((ROOT / "deploy/factologist").exists())

if __name__ == "__main__":
    unittest.main()
