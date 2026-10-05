import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import discord_announce  # noqa: E402
from discord_announce import AnnounceError, build_message, parse_changelog  # noqa: E402
from fake_discord import BOT_ID, GUILD_ID, OWNER_ID, FakeDiscord  # noqa: E402

CHANGELOG = textwrap.dedent("""\
    ---------------------------------------------------------------------------------------------------
    Version: 1.2.0
      Features:
        - A new machine.
    ---------------------------------------------------------------------------------------------------
    Version: 1.1.0
    Date: 2026-10-01
      Features:
        - First feature with a_snake_case name and *stars*.
        - Second feature
          that continues on the next line.
      Bugfixes:
        - Fixed a crash.
    ---------------------------------------------------------------------------------------------------
    Version: 1.0.0
    Date: 2026-09-01
      Info:
        - Initial release.
    """)

LINKS = [("Full changelog", "https://example.org/v1.1.0"), ("Mod portal", "https://example.org/mod")]


class ChangelogTests(unittest.TestCase):
    def test_parses_one_version(self):
        self.assertEqual(
            parse_changelog(CHANGELOG, "1.1.0"),
            [
                ("Features", ["First feature with a_snake_case name and *stars*.",
                              "Second feature that continues on the next line."]),
                ("Bugfixes", ["Fixed a crash."]),
            ],
        )
        self.assertEqual(parse_changelog(CHANGELOG, "1.0.0"), [("Info", ["Initial release."])])

    def test_unreleased_top_section_without_date(self):
        self.assertEqual(parse_changelog(CHANGELOG, "1.2.0"), [("Features", ["A new machine."])])

    def test_unknown_version(self):
        with self.assertRaises(AnnounceError):
            parse_changelog(CHANGELOG, "9.9.9")

    def test_message_layout_and_escaping(self):
        message = build_message("My Mod", "1.1.0", parse_changelog(CHANGELOG, "1.1.0"), LINKS)
        self.assertEqual(
            message,
            "# My Mod 1.1.0\n"
            "**Features** (2)\n"
            "- First feature with a\\_snake\\_case name and \\*stars\\*.\n"
            "- Second feature that continues on the next line.\n"
            "**Bugfixes**\n"
            "- Fixed a crash.\n"
            "\n"
            "[Full changelog](https://example.org/v1.1.0) · [Mod portal](https://example.org/mod)",
        )

    def test_long_changelog_is_cut_to_the_limit(self):
        sections = [
            (name, [f"Entry {index} of {name} " + "with a long explanation " * 30 for index in range(40)])
            for name in ("Features", "Changes", "Bugfixes", "Balancing", "Graphics")
        ]
        message = build_message("My Mod", "2.0.0", sections, LINKS)
        self.assertLessEqual(len(message), 2000)
        self.assertTrue(message.startswith("# My Mod 2.0.0\n**Features** (40)\n- Entry 0 of Features"))
        self.assertIn("… and ", message)
        self.assertTrue(message.endswith("[Mod portal](https://example.org/mod)"))

    def test_huge_changelog_still_fits(self):
        sections = [(f"Category {index}", ["x" * 500] * 50) for index in range(60)]
        self.assertLessEqual(len(build_message("My Mod", "3.0.0", sections, LINKS)), 2000)


class AnnounceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "server.yml").write_text(f'guild_id: "{GUILD_ID}"\n', encoding="utf-8")
        (self.dir / "changelog.txt").write_text(CHANGELOG, encoding="utf-8")
        self.fake = FakeDiscord()
        self.fake.guild["features"].append("COMMUNITY")
        self.releases = self.fake._add_channel({"name": "mod-releases", "type": 5})
        self.chat = self.fake._add_channel({"name": "mod-chat", "type": 0})

    def run_cli(self, *extra, channel="mod-releases", version="1.1.0"):
        summary = self.dir / "summary.md"
        code = discord_announce.main(
            ["--guild-config", str(self.dir / "server.yml"), "--channel", channel, "--title", "My Mod",
             "--version", version, "--changelog", str(self.dir / "changelog.txt"),
             "--link", "Mod portal=https://example.org/mod", "--summary-file", str(summary), *extra],
            transport=self.fake,
        )
        return code, summary.read_text(encoding="utf-8")

    def test_posts_and_publishes(self):
        code, summary = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("published to its followers", summary)
        [message] = self.fake.messages[self.releases["id"]]
        self.assertEqual(message["author"]["id"], BOT_ID)
        self.assertTrue(message["content"].startswith("# My Mod 1.1.0\n"))
        self.assertTrue(message["crossposted"])
        self.assertEqual(message["flags"], 1 << 2)

    def test_second_run_posts_nothing(self):
        self.run_cli()
        code, summary = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Already announced", summary)
        self.assertEqual(len(self.fake.messages[self.releases["id"]]), 1)

    def test_other_versions_and_authors_do_not_count_as_announced(self):
        self.fake.post_as(self.releases["id"], OWNER_ID, "# My Mod 1.1.0\nposted by hand")
        self.run_cli(version="1.0.0")
        code, summary = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Posted", summary)
        self.assertEqual(len(self.fake.messages[self.releases["id"]]), 3)

    def test_dry_run_posts_nothing(self):
        code, summary = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Dry run", summary)
        self.assertIn("> # My Mod 1.1.0", summary)
        self.assertEqual(self.fake.mutations(), [])

    def test_text_channel_is_not_published(self):
        code, summary = self.run_cli(channel="mod-chat")
        self.assertEqual(code, 0)
        self.assertNotIn("published", summary)
        self.assertNotIn(("POST", f"/channels/{self.chat['id']}/messages/crosspost"), self.fake.calls)
        self.assertEqual(len(self.fake.mutations()), 1)

    def test_errors_are_reported(self):
        for extra, expected in (
            ({"channel": "nope"}, "no text or announcement channel"),
            ({"version": "9.9.9"}, "no section 'Version: 9.9.9'"),
        ):
            code, summary = self.run_cli(**extra)
            self.assertEqual(code, 1)
            self.assertIn(expected, summary)
        self.assertEqual(self.fake.mutations(), [])

    def test_bad_link(self):
        code, summary = self.run_cli("--link", "broken")
        self.assertEqual(code, 1)
        self.assertIn("--link must look like", summary)


if __name__ == "__main__":
    unittest.main()
