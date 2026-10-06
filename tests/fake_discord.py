"""In-memory stand-in for the parts of the Discord REST API the sync tool uses.

It enforces the constraints that matter for ordering and idempotency
(Community before announcement channels, wholesale tag replacement, name
normalization, the message length limit) so the tests exercise realistic
behaviour instead of a permissive mock.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import re

from discord_sync import ApiError

GUILD_ID = "100000000000000001"
BOT_ID = "100000000000000002"
OWNER_ID = "100000000000000003"

DEFAULT_EVERYONE = str((1 << 10) | (1 << 11) | (1 << 16) | (1 << 17) | (1 << 35) | (1 << 36) | (1 << 38))


class FakeDiscord:
    def __init__(self):
        self._next = 200000000000000000
        self.calls = []  # every request as (method, path)
        self.me = {"id": BOT_ID, "username": "sync-bot", "bot": True, "avatar": None}
        self.application = {"id": BOT_ID, "icon": None}
        self.application_icon_fails = False
        self.guild = {
            "id": GUILD_ID,
            "name": "Rykon's server",
            "features": [],
            "verification_level": 0,
            "default_message_notifications": 0,
            "explicit_content_filter": 0,
            "system_channel_id": None,
            "system_channel_flags": 0,
            "rules_channel_id": None,
            "public_updates_channel_id": None,
            "preferred_locale": "en-US",
            "description": None,
            "icon": None,
        }
        self.roles = [
            {"id": GUILD_ID, "name": "@everyone", "permissions": DEFAULT_EVERYONE, "color": 0,
             "hoist": False, "mentionable": False, "managed": False, "position": 0},
            {"id": self._id(), "name": "sync-bot", "permissions": "8", "color": 0,
             "hoist": False, "mentionable": False, "managed": True, "position": 1},
        ]
        self.channels = []
        self.messages = {}
        self.invites = {}  # channel id -> list of invite objects
        self.automod_rules = []
        self.automod_default = None  # a built-in rule Discord lists but does not let anyone edit
        text = self._add_channel({"name": "Text Channels", "type": 4, "position": 0})
        voice = self._add_channel({"name": "Voice Channels", "type": 4, "position": 1})
        general = self._add_channel({"name": "general", "type": 0, "parent_id": text["id"], "position": 0})
        self._add_channel({"name": "General", "type": 2, "parent_id": voice["id"], "position": 0})
        self.guild["system_channel_id"] = general["id"]

    # -- helpers -----------------------------------------------------------

    def _id(self):
        self._next += 1
        return str(self._next)

    def _add_channel(self, body):
        channel = {
            "id": self._id(),
            "name": body["name"],
            "type": body["type"],
            "position": body.get("position", 0),
            "parent_id": body.get("parent_id"),
            "permission_overwrites": [],
        }
        self._apply_channel(channel, body)
        self.channels.append(channel)
        self.messages[channel["id"]] = []
        return channel

    def _fail(self, status, method, path, message, code=50035):
        raise ApiError(status, method, path, {"message": message, "code": code})

    def _apply_channel(self, channel, body, method="POST", path=""):
        kind = body.get("type", channel["type"])
        if kind == 5 and "COMMUNITY" not in self.guild["features"]:
            self._fail(400, method, path, "Cannot create or convert to an announcement channel without Community")
        if "type" in body and body["type"] != channel["type"] and {body["type"], channel["type"]} != {0, 5}:
            self._fail(400, method, path, "Invalid channel type conversion")
        for key, value in body.items():
            if key == "name" and kind in (0, 5, 15):
                value = re.sub(r"\s+", "-", value.strip().lower())
            if key == "parent_id" and value is not None:
                if not any(c["id"] == value and c["type"] == 4 for c in self.channels):
                    self._fail(400, method, path, "Unknown parent category")
            if key == "permission_overwrites":
                role_ids = {role["id"] for role in self.roles}
                for entry in value:
                    if int(entry.get("type", 0)) == 0 and entry["id"] not in role_ids:
                        self._fail(400, method, path, "Unknown role in overwrite")
                value = [
                    {"id": e["id"], "type": int(e.get("type", 0)), "allow": str(e.get("allow") or 0),
                     "deny": str(e.get("deny") or 0)}
                    for e in value
                ]
            if key == "available_tags":
                if len(value) > 20:
                    self._fail(400, method, path, "Too many tags")
                known = {tag["id"] for tag in channel.get("available_tags", [])}
                tags = []
                for tag in value:
                    tag = dict(tag)
                    if tag.get("id") not in known:
                        tag["id"] = self._id()
                    tag.setdefault("moderated", False)
                    tag.setdefault("emoji_id", None)
                    tag.setdefault("emoji_name", None)
                    tags.append(tag)
                value = tags
            if key == "topic" and value is not None and len(value) > (4096 if kind == 15 else 1024):
                self._fail(400, method, path, "Topic too long")
            channel[key] = value
        if kind in (0, 5, 15):
            channel.setdefault("topic", None)
        if kind in (0, 15):
            channel.setdefault("rate_limit_per_user", 0)
        if kind == 15:
            channel.setdefault("available_tags", [])
            channel.setdefault("flags", 0)

    def _channel(self, channel_id, method, path):
        for channel in self.channels:
            if channel["id"] == channel_id:
                return channel
        self._fail(404, method, path, "Unknown Channel", 10003)

    def post_as(self, channel_id, author_id, content):
        """Test helper: a message that was not written by the sync tool."""
        message = {"id": self._id(), "content": content, "author": {"id": author_id}, "flags": 0, "pinned": False}
        self.messages[channel_id].append(message)
        return message

    def channel(self, name, kind=None):
        found = [c for c in self.channels if c["name"] == name and (kind is None or c["type"] == kind)]
        assert len(found) == 1, f"expected exactly one channel named {name}, got {len(found)}"
        return found[0]

    def mutations(self):
        return [call for call in self.calls if call[0] != "GET"]

    # -- the API -----------------------------------------------------------

    def request(self, method, path, body=None, query=None):
        self.calls.append((method, path))
        body = copy.deepcopy(body)
        result = self._route(method, path, body, query or {})
        return copy.deepcopy(result)

    def _route(self, method, path, body, query):
        if (method, path) == ("GET", "/users/@me"):
            return self.me
        if (method, path) == ("PATCH", "/users/@me"):
            self.me["avatar"] = self._picture(body["avatar"], method, path)
            return self.me
        if (method, path) == ("PATCH", "/applications/@me"):
            if self.application_icon_fails:
                self._fail(403, method, path, "Missing Access", 50001)
            self.application["icon"] = self._picture(body["icon"], method, path)
            return self.application
        guild = f"/guilds/{GUILD_ID}"
        if path == guild:
            if method == "GET":
                return self.guild
            if method == "PATCH":
                return self._patch_guild(body, method, path)
        if path == f"{guild}/auto-moderation/rules":
            if method == "GET":
                listed = list(self.automod_rules)
                default = self.automod_default
                if default and not any(r["trigger_type"] == default["trigger_type"] for r in listed):
                    listed.append(default)
                return listed
            if method == "POST":
                if body["trigger_type"] in (3, 5) and any(r["trigger_type"] == body["trigger_type"] for r in self.automod_rules):
                    self._fail(400, method, path, "Maximum number of rules of this trigger type reached")
                rule = {"id": self._id(), "guild_id": GUILD_ID, "creator_id": BOT_ID, "enabled": False,
                        "trigger_metadata": {}, "exempt_roles": [], "exempt_channels": []}
                rule.update(body)
                self._check_automod(rule, method, path)
                self.automod_rules.append(rule)
                return rule
        match = re.fullmatch(rf"{guild}/auto-moderation/rules/(\d+)", path)
        if match and method == "PATCH":
            for rule in self.automod_rules:
                if rule["id"] == match.group(1):
                    candidate = dict(rule, **body)
                    self._check_automod(candidate, method, path)
                    rule.update(body)
                    return rule
            self._fail(404, method, path, "Unknown rule", 10066)
        if path == f"{guild}/roles":
            if method == "GET":
                return self.roles
            if method == "POST":
                role = {"id": self._id(), "managed": False, "position": 1, "color": 0, "hoist": False,
                        "mentionable": False, "permissions": "0"}
                role.update(body)
                self.roles.append(role)
                return role
        match = re.fullmatch(rf"{guild}/roles/(\d+)", path)
        if match and method == "PATCH":
            for role in self.roles:
                if role["id"] == match.group(1):
                    if role["managed"]:
                        self._fail(403, method, path, "Missing Permissions", 50013)
                    role.update(body)
                    return role
            self._fail(404, method, path, "Unknown Role", 10011)
        if path == f"{guild}/channels":
            if method == "GET":
                return self.channels
            if method == "POST":
                return self._add_channel_checked(body, method, path)
            if method == "PATCH":
                for entry in body:
                    channel = self._channel(entry["id"], method, path)
                    if "position" in entry:
                        channel["position"] = entry["position"]
                return None
        match = re.fullmatch(r"/channels/(\d+)", path)
        if match:
            channel = self._channel(match.group(1), method, path)
            if method == "PATCH":
                self._apply_channel(channel, body, method, path)
                return channel
            if method == "DELETE":
                self.channels.remove(channel)
                for child in self.channels:
                    if child.get("parent_id") == channel["id"]:
                        child["parent_id"] = None
                return channel
        match = re.fullmatch(r"/channels/(\d+)/invites", path)
        if match:
            channel = self._channel(match.group(1), method, path)
            invites = self.invites.setdefault(channel["id"], [])
            if method == "GET":
                return invites
            if method == "POST":
                wanted = {"max_age": body.get("max_age", 86400), "max_uses": body.get("max_uses", 0),
                          "temporary": body.get("temporary", False)}
                if not body.get("unique"):
                    for invite in invites:
                        if invite["inviter"]["id"] == BOT_ID and all(invite[k] == v for k, v in wanted.items()):
                            return invite
                invite = dict(wanted, code=f"code{self._id()[-6:]}", inviter={"id": BOT_ID}, channel={"id": channel["id"]})
                invites.append(invite)
                return invite
        match = re.fullmatch(r"/channels/(\d+)/messages", path)
        if match:
            channel = self._channel(match.group(1), method, path)
            messages = self.messages[channel["id"]]
            if method == "GET":
                return list(reversed(messages))[: int(query.get("limit", 50))]
            if method == "POST":
                self._check_message(body, method, path)
                message = {"id": self._id(), "content": body["content"].strip(), "author": {"id": BOT_ID},
                           "flags": body.get("flags", 0), "pinned": False}
                messages.append(message)
                return message
        match = re.fullmatch(r"/channels/(\d+)/messages/pins", path)
        if match and method == "GET":
            channel = self._channel(match.group(1), method, path)
            pinned = [m for m in self.messages[channel["id"]] if m["pinned"]]
            return {"items": [{"pinned_at": "2026-01-01T00:00:00+00:00", "message": m} for m in pinned],
                    "has_more": False}
        match = re.fullmatch(r"/channels/(\d+)/messages/pins/(\d+)", path)
        if match and method in ("PUT", "DELETE"):
            message = self._message(match.group(1), match.group(2), method, path)
            message["pinned"] = method == "PUT"
            return None
        match = re.fullmatch(r"/channels/(\d+)/messages/(\d+)/crosspost", path)
        if match and method == "POST":
            channel = self._channel(match.group(1), method, path)
            message = self._message(match.group(1), match.group(2), method, path)
            if channel["type"] != 5:
                self._fail(400, method, path, "Cannot crosspost outside of an announcement channel")
            if message.get("crossposted"):
                self._fail(400, method, path, "This message has already been crossposted", 40033)
            message["crossposted"] = True
            return message
        match = re.fullmatch(r"/channels/(\d+)/messages/(\d+)", path)
        if match and method == "PATCH":
            message = self._message(match.group(1), match.group(2), method, path)
            if message["author"]["id"] != BOT_ID:
                self._fail(403, method, path, "Cannot edit a message authored by another user", 50005)
            self._check_message(body, method, path)
            message["content"] = body["content"].strip()
            message["flags"] = body.get("flags", message["flags"])
            return message
        raise AssertionError(f"FakeDiscord: unexpected request {method} {path}")

    def _add_channel_checked(self, body, method, path):
        if body["type"] == 5 and "COMMUNITY" not in self.guild["features"]:
            self._fail(400, method, path, "Cannot create an announcement channel without Community")
        if body.get("parent_id") and not any(c["id"] == body["parent_id"] for c in self.channels):
            self._fail(400, method, path, "Unknown parent category")
        return self._add_channel(body)

    def _check_automod(self, rule, method, path):
        if not rule.get("name") or rule.get("event_type") != 1 or not rule.get("actions"):
            self._fail(400, method, path, "Invalid AutoMod rule")
        channel_ids = {c["id"] for c in self.channels}
        role_ids = {r["id"] for r in self.roles}
        for action in rule["actions"]:
            if action["type"] == 2 and (action.get("metadata") or {}).get("channel_id") not in channel_ids:
                self._fail(400, method, path, "Unknown alert channel")
        if not set(rule.get("exempt_roles") or []) <= role_ids:
            self._fail(400, method, path, "Unknown exempt role")
        if rule["trigger_type"] == 5 and not 1 <= rule["trigger_metadata"].get("mention_total_limit", 0) <= 50:
            self._fail(400, method, path, "Invalid mention limit")

    def _picture(self, uri, method, path):
        """Accept a data URI like Discord does and return a stand-in for the stored hash."""
        match = re.fullmatch(r"data:image/(png|jpeg|gif);base64,([A-Za-z0-9+/=]+)", uri or "")
        if not match:
            self._fail(400, method, path, "Invalid image data")
        return hashlib.sha256(base64.b64decode(match.group(2))).hexdigest()[:32]

    def _patch_guild(self, body, method, path):
        if "icon" in body:
            body["icon"] = self._picture(body["icon"], method, path)
        merged = dict(self.guild, **body)
        if "COMMUNITY" in merged["features"]:
            text_ids = {c["id"] for c in self.channels if c["type"] in (0, 5)}
            if merged["rules_channel_id"] not in text_ids or merged["public_updates_channel_id"] not in text_ids:
                self._fail(400, method, path, "Community needs a rules channel and a public updates channel")
            if merged["verification_level"] < 1 or merged["explicit_content_filter"] != 2:
                self._fail(400, method, path, "Community needs verification and the content filter")
        if merged["system_channel_id"] is not None:
            self._channel(merged["system_channel_id"], method, path)
        self.guild.update(body)
        return self.guild

    def _message(self, channel_id, message_id, method, path):
        for message in self.messages[self._channel(channel_id, method, path)["id"]]:
            if message["id"] == message_id:
                return message
        self._fail(404, method, path, "Unknown Message", 10008)

    def _check_message(self, body, method, path):
        if not body.get("content") or len(body["content"]) > 2000:
            self._fail(400, method, path, "Invalid message content")
        if body.get("allowed_mentions") != {"parse": []}:
            raise AssertionError("managed messages must not ping anyone")
