import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import discord_branding  # noqa: E402
from fake_discord import GUILD_ID, FakeDiscord  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"icon-bytes"
OTHER_PNG = b"\x89PNG\r\n\x1a\n" + b"avatar-bytes"


def stored(raw):
    return hashlib.sha256(raw).hexdigest()[:32]


class BrandingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "server.yml").write_text(f'guild_id: "{GUILD_ID}"\n', encoding="utf-8")
        (self.dir / "icon.png").write_bytes(PNG)
        (self.dir / "avatar.png").write_bytes(OTHER_PNG)
        self.fake = FakeDiscord()

    def run_cli(self, *args):
        summary = self.dir / "summary.md"
        code = discord_branding.main(
            ["--guild-config", str(self.dir / "server.yml"), "--summary-file", str(summary), *args], transport=self.fake
        )
        return code, summary.read_text(encoding="utf-8")

    def both(self):
        return ["--server-icon", str(self.dir / "icon.png"), "--bot-avatar", str(self.dir / "avatar.png")]

    def test_sets_icon_avatar_and_application_icon(self):
        code, summary = self.run_cli(*self.both())
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.guild["icon"], stored(PNG))
        self.assertEqual(self.fake.me["avatar"], stored(OTHER_PNG))
        self.assertEqual(self.fake.application["icon"], stored(OTHER_PNG))
        self.assertIn("set the server icon", summary)
        self.assertIn("set the bot avatar", summary)
        # nothing but the three pictures was touched
        self.assertEqual(self.fake.guild["name"], "Rykon's server")
        self.assertEqual(len(self.fake.mutations()), 3)

    def test_only_the_server_icon(self):
        code, _ = self.run_cli("--server-icon", str(self.dir / "icon.png"))
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.guild["icon"], stored(PNG))
        self.assertIsNone(self.fake.me["avatar"])

    def test_dry_run_changes_nothing(self):
        code, summary = self.run_cli(*self.both(), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would set the server icon", summary)
        self.assertEqual(self.fake.calls, [])

    def test_application_icon_failure_is_not_fatal(self):
        self.fake.application_icon_fails = True
        code, summary = self.run_cli(*self.both())
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.me["avatar"], stored(OTHER_PNG))
        self.assertIn("application icon was not changed", summary)

    def test_bad_files_are_refused_before_anything_changes(self):
        (self.dir / "avatar.png").write_text("not a picture", encoding="utf-8")
        code, summary = self.run_cli(*self.both())
        self.assertEqual(code, 1)
        self.assertIn("not a PNG, JPEG or GIF", summary)
        code, summary = self.run_cli("--server-icon", str(self.dir / "missing.png"))
        self.assertEqual(code, 1)
        self.assertIn("picture not found", summary)
        code, summary = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("nothing to do", summary)
        self.assertEqual(self.fake.mutations(), [])

    def test_the_repository_pictures_are_valid(self):
        for name in ("server-icon.png", "bot-avatar.png"):
            uri = discord_branding.data_uri(ROOT / "assets" / name)
            self.assertTrue(uri.startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()
