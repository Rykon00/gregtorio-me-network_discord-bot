import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import discord_sync  # noqa: E402
from discord_sync import ConfigError, SyncError, Syncer, load_configs  # noqa: E402
from fake_discord import BOT_ID, GUILD_ID, OWNER_ID, FakeDiscord  # noqa: E402

GUILD_CONFIG = f"""
guild_id: "{GUILD_ID}"
server:
  name: Test Server
  verification_level: low
  default_notifications: mentions
  content_filter: all_members
  community: true
  rules_channel: rules
  updates_channel: moderators
  system_channel: general
  suppress_system_messages: [tips]
everyone:
  deny: [mention_everyone]
roles:
  - name: Maintainer
    color: "#E67E22"
    hoist: true
    permissions: [kick_members, manage_messages]
categories:
  - name: Information
    position: 0
    read_only: true
    channels:
      - name: rules
        messages:
          - file: rules.md
      - name: announcements
        type: announcement
  - name: Community
    previous_names: [Text Channels]
    position: 10
    channels:
      - name: general
        topic: Talk here.
      - name: help
        type: forum
        tags: [question, solved]
        require_tag: true
  - name: Staff
    position: 80
    private: true
    visible_to: [Maintainer]
    channels:
      - name: moderators
  - name: Voice
    previous_names: [Voice Channels]
    position: 90
    channels:
      - name: General
        type: voice
"""

FRAGMENT = """
roles:
  - name: Mod Updates
    mentionable: true
categories:
  - name: My Mod
    position: 20
    channels:
      - name: mod-chat
        topic: Chat about the mod.
        messages:
          - file: pinned.md
            pin: true
      - name: mod-releases
        type: announcement
        read_only: true
"""


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.fake = FakeDiscord()
        self.write("rules.md", "# Rules\n\nBe nice. Ask a {{@Maintainer}} in {{#general}}.\n")
        self.write("server.yml", GUILD_CONFIG)

    def write(self, name, text):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def sync(self, apply=True, config="server.yml", guild_config=None):
        guild_path = self.dir / guild_config if guild_config else None
        loaded, guild = load_configs(self.dir / config, guild_path)
        syncer = Syncer(self.fake, loaded, guild, apply=apply)
        syncer.run()
        return syncer

    def edit(self, old, new, name="server.yml"):
        path = self.dir / name
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), encoding="utf-8")


class GuildConfigTests(SyncTestCase):
    def test_plan_changes_nothing(self):
        syncer = self.sync(apply=False)
        self.assertTrue(syncer.changes)
        self.assertEqual(self.fake.mutations(), [])

    def test_plan_lists_exactly_what_apply_does(self):
        planned = self.sync(apply=False).changes
        applied = self.sync(apply=True).changes
        self.assertEqual(planned, applied)

    def test_apply_builds_the_server_and_is_idempotent(self):
        general_id = self.fake.channel("general")["id"]
        self.sync()

        guild = self.fake.guild
        self.assertEqual(guild["name"], "Test Server")
        self.assertIn("COMMUNITY", guild["features"])
        self.assertEqual(guild["rules_channel_id"], self.fake.channel("rules")["id"])
        self.assertEqual(guild["public_updates_channel_id"], self.fake.channel("moderators")["id"])
        self.assertEqual(guild["system_channel_flags"], 1 << 2)

        # The default category was renamed, not replaced, and #general kept its ID.
        community = self.fake.channel("Community", 4)
        self.assertEqual([c["name"] for c in self.fake.channels if c["name"] == "Text Channels"], [])
        self.assertEqual(self.fake.channel("general")["id"], general_id)
        self.assertEqual(self.fake.channel("general")["parent_id"], community["id"])
        self.assertEqual(self.fake.channel("general")["topic"], "Talk here.")
        self.assertEqual(self.fake.channel("announcements")["type"], 5)

        forum = self.fake.channel("help")
        self.assertEqual([tag["name"] for tag in forum["available_tags"]], ["question", "solved"])
        self.assertEqual(forum["flags"], 1 << 4)

        everyone = self.fake.roles[0]
        self.assertFalse(int(everyone["permissions"]) >> 17 & 1)

        second = self.sync()
        self.assertEqual(second.changes, [])
        self.assertEqual(second.notes, [])

    def test_permission_presets(self):
        self.sync()
        maintainer = next(r for r in self.fake.roles if r["name"] == "Maintainer")
        self.assertEqual(int(maintainer["permissions"]), (1 << 1) | (1 << 13))

        rules = {o["id"]: o for o in self.fake.channel("rules")["permission_overwrites"]}
        self.assertTrue(int(rules[GUILD_ID]["deny"]) >> 11 & 1)  # send_messages

        staff = {o["id"]: o for o in self.fake.channel("moderators")["permission_overwrites"]}
        self.assertTrue(int(staff[GUILD_ID]["deny"]) >> 10 & 1)  # view_channel
        self.assertTrue(int(staff[maintainer["id"]]["allow"]) >> 10 & 1)

    def test_member_overwrites_survive(self):
        self.sync()
        general = self.fake.channel("general")
        general["permission_overwrites"].append({"id": OWNER_ID, "type": 1, "allow": "0", "deny": str(1 << 11)})
        self.edit("previous_names: [Text Channels]", "previous_names: [Text Channels]\n    read_only: true")
        self.sync()
        overwrites = self.fake.channel("general")["permission_overwrites"]
        self.assertIn(OWNER_ID, [o["id"] for o in overwrites])
        self.assertIn(GUILD_ID, [o["id"] for o in overwrites])
        self.assertEqual(self.sync().changes, [])

    def test_unknown_channels_are_reported_not_deleted(self):
        self.sync()
        community = self.fake.channel("Community", 4)
        self.fake._add_channel({"name": "memes", "type": 0, "parent_id": community["id"], "position": 5})
        syncer = self.sync()
        self.assertEqual(syncer.changes, [])
        self.assertEqual(len(syncer.notes), 1)
        self.assertIn("#memes", syncer.notes[0])
        self.fake.channel("memes")

    def test_removed_entries_stay_on_the_server(self):
        self.sync()
        self.edit("      - name: help\n        type: forum\n        tags: [question, solved]\n        require_tag: true\n", "")
        syncer = self.sync()
        self.assertEqual(syncer.changes, [])
        self.assertIn("#help", syncer.notes[0])
        self.fake.channel("help")

    def test_delete_needs_an_explicit_flag(self):
        self.sync()
        self.edit("        tags: [question, solved]\n        require_tag: true\n", "        delete: true\n")
        syncer = self.sync()
        self.assertEqual(len(syncer.changes), 1)
        self.assertIn("DELETE channel #help", syncer.changes[0])
        self.assertEqual([c for c in self.fake.channels if c["name"] == "help"], [])
        self.assertEqual(self.sync().changes, [])

    def test_category_is_only_deleted_when_empty(self):
        self.sync()
        self.edit("  - name: Voice\n    previous_names: [Voice Channels]\n    position: 90\n",
                  "  - name: Voice\n    delete: true\n")
        with self.assertRaises(SyncError):
            self.sync()
        self.fake.channel("Voice", 4)
        self.edit("      - name: General\n        type: voice\n",
                  "      - name: General\n        type: voice\n        delete: true\n")
        self.sync()
        self.assertEqual([c for c in self.fake.channels if c["name"] in ("Voice", "General")], [])

    def test_rename_keeps_the_channel(self):
        self.sync()
        old_id = self.fake.channel("general")["id"]
        message = self.fake.post_as(old_id, OWNER_ID, "hello")
        self.edit("      - name: general\n", "      - name: lobby\n        previous_names: [general]\n")
        self.edit("system_channel: general", "system_channel: lobby")
        self.edit("{{#general}}", "{{#lobby}}", "rules.md")
        syncer = self.sync()
        self.assertIn("update #general: rename → #lobby", syncer.changes)
        self.assertEqual(self.fake.channel("lobby")["id"], old_id)
        self.assertIn(message, self.fake.messages[old_id])
        self.assertEqual(self.sync().changes, [])

    def test_moving_a_channel_between_categories(self):
        self.sync()
        old_id = self.fake.channel("moderators")["id"]
        self.edit("    channels:\n      - name: moderators\n", "    channels: []\n")
        self.edit("      - name: general\n", "      - name: moderators\n      - name: general\n")
        self.sync()
        moved = self.fake.channel("moderators")
        self.assertEqual(moved["id"], old_id)
        self.assertEqual(moved["parent_id"], self.fake.channel("Community", 4)["id"])
        self.assertEqual(self.sync().changes, [])

    def test_channel_order_follows_the_config(self):
        self.sync()
        self.edit("      - name: rules\n        messages:\n          - file: rules.md\n      - name: announcements\n        type: announcement\n",
                  "      - name: announcements\n        type: announcement\n      - name: rules\n        messages:\n          - file: rules.md\n")
        syncer = self.sync()
        self.assertEqual(len(syncer.changes), 1)
        self.assertIn("reorder", syncer.changes[0])
        info = self.fake.channel("Information", 4)["id"]
        ordered = sorted((c for c in self.fake.channels if c.get("parent_id") == info), key=lambda c: c["position"])
        self.assertEqual([c["name"] for c in ordered], ["announcements", "rules"])
        self.assertEqual(self.sync().changes, [])

    def test_forum_tags_keep_ids_and_extra_tags(self):
        self.sync()
        forum = self.fake.channel("help")
        ids = {tag["name"]: tag["id"] for tag in forum["available_tags"]}
        forum["available_tags"].append({"id": "999", "name": "manual", "moderated": False, "emoji_id": None, "emoji_name": None})
        self.edit("tags: [question, solved]", "tags: [question, solved, bug]")
        syncer = self.sync()
        self.assertTrue(any("tags (+bug)" in change for change in syncer.changes))
        tags = {tag["name"]: tag["id"] for tag in self.fake.channel("help")["available_tags"]}
        self.assertEqual(tags["question"], ids["question"])
        self.assertEqual(tags["solved"], ids["solved"])
        self.assertEqual(tags["manual"], "999")
        self.assertIn("bug", tags)
        again = self.sync()
        self.assertEqual(again.changes, [])
        self.assertTrue(any("manual" in note for note in again.notes))

    def test_managed_message_is_edited_in_place(self):
        self.sync()
        rules = self.fake.channel("rules")
        maintainer = next(r for r in self.fake.roles if r["name"] == "Maintainer")
        [message] = self.fake.messages[rules["id"]]
        self.assertEqual(message["author"]["id"], BOT_ID)
        self.assertIn(f"<@&{maintainer['id']}>", message["content"])
        self.assertIn(f"<#{self.fake.channel('general')['id']}>", message["content"])
        self.assertEqual(message["flags"], 1 << 2)

        self.write("rules.md", "# Rules\n\nBe very nice.\n")
        syncer = self.sync()
        self.assertEqual(syncer.changes, ["edit message `rules.md` in #rules"])
        [edited] = self.fake.messages[rules["id"]]
        self.assertEqual(edited["id"], message["id"])
        self.assertEqual(edited["content"], "# Rules\n\nBe very nice.")
        self.assertEqual(self.sync().changes, [])

    def test_messages_from_other_users_are_ignored(self):
        self.sync()
        rules = self.fake.channel("rules")
        self.fake.post_as(rules["id"], OWNER_ID, "# Rules\n\nSomething else.")
        self.assertEqual(self.sync().changes, [])

    def test_role_rename_and_recolor(self):
        self.sync()
        role_id = next(r for r in self.fake.roles if r["name"] == "Maintainer")["id"]
        self.edit('  - name: Maintainer\n    color: "#E67E22"', '  - name: Team\n    previous_names: [Maintainer]\n    color: "#112233"')
        self.edit("visible_to: [Maintainer]", "visible_to: [Team]")
        self.edit("{{@Maintainer}}", "{{@Team}}", "rules.md")
        self.sync()
        role = next(r for r in self.fake.roles if r["id"] == role_id)
        self.assertEqual((role["name"], role["color"]), ("Team", 0x112233))
        self.assertEqual(self.sync().changes, [])

    def test_ambiguous_names_stop_the_run(self):
        self.fake._add_channel({"name": "Text Channels", "type": 4, "position": 7})
        with self.assertRaises(SyncError):
            self.sync(apply=False)

    def test_failed_request_is_reported_with_the_step(self):
        self.fake.roles[1]["name"] = "Maintainer"  # only a managed role has the name -> a new one is created
        original = self.fake._route

        def broken(method, path, body, query):
            if method == "POST" and path.endswith("/roles"):
                self.fake._fail(403, method, path, "Missing Permissions", 50013)
            return original(method, path, body, query)

        self.fake._route = broken
        with self.assertRaises(SyncError) as caught:
            self.sync()
        self.assertIn("could not create role `Maintainer`", str(caught.exception))
        self.assertIn("Missing Permissions", str(caught.exception))


AUTOMOD = """
automod:
  alert_channel: moderators
  exempt_roles: [Maintainer]
  block_spam: true
  block_mention_spam: 6
"""


class AutoModTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.edit("everyone:\n", AUTOMOD.lstrip("\n") + "everyone:\n")

    def rule(self, trigger_type):
        [rule] = [r for r in self.fake.automod_rules if r["trigger_type"] == trigger_type]
        return rule

    def test_creates_both_rules_once(self):
        planned = self.sync(apply=False)
        self.assertIn('create AutoMod rule "Block spam"', planned.changes)
        self.assertIn('create AutoMod rule "Block mention spam"', planned.changes)
        self.assertEqual(self.fake.mutations(), [])

        self.sync()
        maintainer = next(r for r in self.fake.roles if r["name"] == "Maintainer")["id"]
        moderators = self.fake.channel("moderators")["id"]
        spam, mentions = self.rule(3), self.rule(5)
        for rule in (spam, mentions):
            self.assertTrue(rule["enabled"])
            self.assertEqual(rule["exempt_roles"], [maintainer])
            self.assertEqual(rule["actions"], [{"type": 1}, {"type": 2, "metadata": {"channel_id": moderators}}])
        self.assertEqual(mentions["trigger_metadata"],
                         {"mention_total_limit": 6, "mention_raid_protection_enabled": True})
        self.assertEqual(self.sync().changes, [])

    def test_an_existing_rule_of_the_kind_is_adjusted_not_duplicated(self):
        self.fake.automod_rules.append({
            "id": "777", "name": "Block Mention Spam", "event_type": 1, "trigger_type": 5, "enabled": False,
            "trigger_metadata": {"mention_total_limit": 20}, "actions": [{"type": 1, "metadata": {}}],
            "exempt_roles": [], "exempt_channels": ["123"]})
        syncer = self.sync()
        self.assertTrue(any(change.startswith('update AutoMod rule "Block Mention Spam"') for change in syncer.changes))
        rule = self.rule(5)
        self.assertEqual(rule["id"], "777")
        self.assertTrue(rule["enabled"])
        self.assertEqual(rule["trigger_metadata"]["mention_total_limit"], 6)
        self.assertEqual(rule["exempt_channels"], ["123"])       # what the config does not set stays
        self.assertEqual(self.sync().changes, [])

    def test_changing_the_limit_updates_the_rule(self):
        self.sync()
        self.edit("block_mention_spam: 6", "block_mention_spam: 10")
        self.assertEqual(self.sync().changes, ['update AutoMod rule "Block mention spam": limits'])
        self.assertEqual(self.rule(5)["trigger_metadata"]["mention_total_limit"], 10)

    def test_switched_off_rules_are_left_alone(self):
        self.sync()
        self.edit("block_spam: true", "block_spam: false")
        self.assertEqual(self.sync().changes, [])
        self.assertTrue(self.rule(3)["enabled"])                 # nothing is disabled or deleted implicitly

    def test_validation(self):
        for old, new in (("block_mention_spam: 6", "block_mention_spam: 99"),
                         ("block_spam: true", "block_spam: sometimes"),
                         ("alert_channel: moderators", "alert_chanel: moderators")):
            self.edit(old, new)
            with self.assertRaises(ConfigError):
                load_configs(self.dir / "server.yml")
            self.edit(new, old)

    def test_a_fragment_cannot_set_it(self):
        self.write("mod/.discord/server.yml", "automod:\n  block_spam: true\n")
        with self.assertRaises(ConfigError):
            load_configs(self.dir / "mod/.discord/server.yml", self.dir / "server.yml")


class InviteTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.edit("  system_channel: general\n", "  system_channel: general\n  invite_channel: rules\n")

    def invites(self):
        return self.fake.invites.get(self.fake.channel("rules")["id"], [])

    def test_plan_announces_the_link_and_apply_creates_it_once(self):
        planned = self.sync(apply=False)
        self.assertIn("create a permanent invite link to #rules", planned.changes)
        self.assertIsNone(planned.invite_url)
        self.assertEqual(self.fake.mutations(), [])

        applied = self.sync()
        [invite] = self.invites()
        self.assertEqual((invite["max_age"], invite["max_uses"], invite["temporary"]), (0, 0, False))
        self.assertEqual(applied.invite_url, f"https://discord.gg/{invite['code']}")

        again = self.sync()
        self.assertEqual(again.changes, [])
        self.assertEqual(again.invite_url, applied.invite_url)
        self.assertEqual(len(self.invites()), 1)

    def test_other_invites_do_not_count(self):
        self.sync()
        rules = self.fake.channel("rules")["id"]
        own = self.invites()[0]
        self.fake.invites[rules] = [
            dict(own, code="byowner", inviter={"id": OWNER_ID}),      # somebody else's permanent link
            dict(own, code="expires", max_age=3600),                  # the bot's, but it expires
            dict(own, code="limited", max_uses=5),                    # the bot's, but limited
        ]
        syncer = self.sync()
        self.assertEqual(syncer.changes, ["create a permanent invite link to #rules"])
        self.assertNotIn(syncer.invite_url.rsplit("/", 1)[1], ("byowner", "expires", "limited"))

    def test_the_link_is_in_the_summary(self):
        summary = self.dir / "summary.md"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.dir / "out")}):
            code = discord_sync.main(["apply", "--config", str(self.dir / "server.yml"), "--summary-file", str(summary)],
                                     transport=self.fake)
        self.assertEqual(code, 0)
        self.assertIn(f"**Invite link:** https://discord.gg/{self.invites()[0]['code']}", summary.read_text(encoding="utf-8"))

    def test_a_fragment_cannot_set_it(self):
        self.write("mod/.discord/server.yml", "server:\n  invite_channel: rules\n")
        with self.assertRaises(ConfigError):
            load_configs(self.dir / "mod/.discord/server.yml", self.dir / "server.yml")


class FragmentTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.write("mod/.discord/server.yml", FRAGMENT)
        self.write("mod/.discord/pinned.md", "**How to report a bug**\n\nOpen an issue on GitHub. Rules: {{#rules}}\n")

    def fragment(self, apply=True):
        return self.sync(apply=apply, config="mod/.discord/server.yml", guild_config="server.yml")

    def test_fragment_adds_its_category_and_is_idempotent(self):
        self.sync()
        before = {c["id"]: dict(c) for c in self.fake.channels}
        syncer = self.fragment()
        self.assertTrue(syncer.changes)

        category = self.fake.channel("My Mod", 4)
        self.assertEqual(category["position"], 20)
        self.assertEqual(self.fake.channel("mod-releases")["type"], 5)
        [pinned] = self.fake.messages[self.fake.channel("mod-chat")["id"]]
        self.assertTrue(pinned["pinned"])
        self.assertIn(f"<#{self.fake.channel('rules')['id']}>", pinned["content"])

        # Nothing that belongs to the guild config was touched.
        for channel_id, channel in before.items():
            self.assertEqual(next(c for c in self.fake.channels if c["id"] == channel_id), channel)
        self.assertEqual(self.fake.guild["name"], "Test Server")

        self.assertEqual(self.fragment().changes, [])
        self.assertEqual(self.sync().changes, [])

    def test_fragment_plan_matches_apply(self):
        self.sync()
        planned = self.fragment(apply=False).changes
        self.assertEqual(self.fake.channels, [c for c in self.fake.channels if c["name"] != "My Mod"])
        self.assertEqual(planned, self.fragment().changes)

    def test_fragment_ignores_channels_of_other_categories(self):
        self.sync()
        self.fragment()
        # A channel with the same name in a category this fragment does not own.
        community = self.fake.channel("Community", 4)
        self.fake._add_channel({"name": "mod-chat", "type": 0, "parent_id": community["id"], "position": 9})
        self.assertEqual(self.fragment().changes, [])

    def test_fragment_does_not_adopt_channels_of_other_categories(self):
        self.sync()
        community = self.fake.channel("Community", 4)
        foreign = self.fake._add_channel({"name": "mod-chat", "type": 0, "parent_id": community["id"], "position": 9})
        self.fragment()
        own = [c for c in self.fake.channels if c["name"] == "mod-chat" and c["id"] != foreign["id"]]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["parent_id"], self.fake.channel("My Mod", 4)["id"])
        self.assertEqual(foreign["parent_id"], community["id"])
        self.assertEqual(self.fake.messages[foreign["id"]], [])

    def test_announcement_channel_needs_community(self):
        with self.assertRaises(SyncError) as caught:
            self.fragment()
        self.assertIn("Community", str(caught.exception))

    def test_fragment_cannot_carry_server_settings(self):
        self.write("mod/.discord/server.yml", "server:\n  name: Hijacked\n" + FRAGMENT)
        with self.assertRaises(ConfigError):
            self.fragment()

    def test_fragment_cannot_claim_guild_categories_or_roles(self):
        self.write("mod/.discord/server.yml", FRAGMENT.replace("name: My Mod", "name: Community"))
        with self.assertRaises(ConfigError):
            self.fragment()
        self.write("mod/.discord/server.yml", FRAGMENT.replace("name: My Mod", "name: Mine\n    previous_names: [Text Channels]"))
        with self.assertRaises(ConfigError):
            self.fragment()
        self.write("mod/.discord/server.yml", FRAGMENT.replace("name: Mod Updates", "name: Maintainer"))
        with self.assertRaises(ConfigError):
            self.fragment()

    def test_fragment_needs_a_guild_config(self):
        with self.assertRaises(ConfigError):
            load_configs(self.dir / "mod/.discord/server.yml")


class ValidationTests(SyncTestCase):
    def assert_invalid(self, old, new, expected):
        self.edit(old, new)
        with self.assertRaises(ConfigError) as caught:
            load_configs(self.dir / "server.yml")
        self.assertIn(expected, str(caught.exception))

    def test_unknown_key(self):
        self.assert_invalid("topic: Talk here.", "topik: Talk here.", "unknown key(s) topik")

    def test_unknown_permission(self):
        self.assert_invalid("kick_members", "kick_everyone", "unknown permission")

    def test_channel_name_must_be_normalized(self):
        self.assert_invalid("- name: general", "- name: General Chat", "lowercase")

    def test_duplicate_channel(self):
        self.assert_invalid("      - name: moderators\n", "      - name: general\n", "declared twice")

    def test_community_requirements(self):
        self.assert_invalid("content_filter: all_members", "content_filter: disabled", "community needs")

    def test_forum_keys_on_text_channel(self):
        self.assert_invalid("topic: Talk here.", "tags: [a]", "only apply to forum")

    def test_message_too_long(self):
        self.write("rules.md", "# Rules\n" + "x" * 2000)
        with self.assertRaises(ConfigError) as caught:
            load_configs(self.dir / "server.yml")
        self.assertIn("longer than 2000", str(caught.exception))

    def test_missing_message_file(self):
        (self.dir / "rules.md").unlink()
        with self.assertRaises(ConfigError):
            load_configs(self.dir / "server.yml")


class CliTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.outputs = self.dir / "github-output"
        patcher = mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.outputs)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def step_outputs(self):
        lines = self.outputs.read_text(encoding="utf-8").splitlines()
        self.outputs.unlink()
        return dict(line.split("=", 1) for line in lines)

    def test_step_outputs(self):
        config = str(self.dir / "server.yml")
        self.run_cli("plan", "--config", config)
        self.assertEqual(self.step_outputs(), {"mode": "plan", "ok": "true", "changes": "14", "deletions": "0"})
        self.run_cli("apply", "--config", config)
        self.assertEqual(self.step_outputs()["mode"], "apply")
        self.edit("        tags: [question, solved]\n        require_tag: true\n", "        delete: true\n")
        self.run_cli("plan", "--config", config)
        self.assertEqual(self.step_outputs(), {"mode": "plan", "ok": "true", "changes": "1", "deletions": "1"})
        self.run_cli("validate", "--config", config)
        self.assertEqual(self.step_outputs(), {"mode": "validate", "ok": "true"})
        self.edit("topic: Talk here.", "topik: x")
        self.run_cli("plan", "--config", config)
        self.assertEqual(self.step_outputs(), {"mode": "plan", "ok": "false"})

    def run_cli(self, *args):
        summary = self.dir / "summary.md"
        code = discord_sync.main([*args, "--summary-file", str(summary)], transport=self.fake)
        return code, summary.read_text(encoding="utf-8")

    def test_validate_makes_no_requests(self):
        code, summary = self.run_cli("validate", "--config", str(self.dir / "server.yml"))
        self.assertEqual(code, 0)
        self.assertIn("Config is valid", summary)
        self.assertEqual(self.fake.calls, [])

    def test_plan_then_apply_summaries(self):
        code, summary = self.run_cli("plan", "--config", str(self.dir / "server.yml"))
        self.assertEqual(code, 0)
        self.assertIn("Dry run", summary)
        self.assertIn("create category **Information**", summary)
        self.assertIn("update category **Text Channels**: rename → **Community**", summary)
        self.assertEqual(self.fake.mutations(), [])

        code, summary = self.run_cli("apply", "--config", str(self.dir / "server.yml"))
        self.assertEqual(code, 0)
        self.assertIn("Applied", summary)

        code, summary = self.run_cli("plan", "--config", str(self.dir / "server.yml"))
        self.assertEqual(code, 0)
        self.assertIn("No changes", summary)

    def test_invalid_config_fails(self):
        self.edit("topic: Talk here.", "topik: x")
        code, summary = self.run_cli("plan", "--config", str(self.dir / "server.yml"))
        self.assertEqual(code, 1)
        self.assertIn("Invalid config", summary)


class RepositoryLayoutTests(unittest.TestCase):
    """The real server.yml of this repository against a factory-fresh server."""

    def test_real_layout_applies_cleanly(self):
        config, guild = load_configs(ROOT / "server.yml")
        fake = FakeDiscord()
        # The fake uses its own guild ID; everything else is the real file.
        guild.guild_id = GUILD_ID
        general_id = fake.channel("general")["id"]

        plan = Syncer(fake, config, guild, apply=False)
        plan.run()
        self.assertEqual(fake.mutations(), [])

        first = Syncer(fake, config, guild, apply=True)
        first.run()
        self.assertEqual(plan.changes, first.changes)
        self.assertEqual(fake.channel("general")["id"], general_id)
        self.assertIn("COMMUNITY", fake.guild["features"])
        for name in ("welcome", "rules"):
            [message] = fake.messages[fake.channel(name)["id"]]
            self.assertLessEqual(len(message["content"]), 2000)
            self.assertNotIn("{{", message["content"])

        second = Syncer(fake, config, guild, apply=True)
        second.run()
        self.assertEqual(second.changes, [])
        self.assertEqual(second.notes, [])


if __name__ == "__main__":
    unittest.main()
