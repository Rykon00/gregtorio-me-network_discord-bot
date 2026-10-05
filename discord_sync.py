#!/usr/bin/env python3
"""Declarative layout sync for a Discord server.

Reads a YAML layout (roles, categories, channels, managed messages, server
settings) and reconciles a Discord guild with it through the REST API.

    discord_sync.py validate --config server.yml
    discord_sync.py plan     --config server.yml
    discord_sync.py apply    --config server.yml

The guild config (the file that carries ``guild_id``) owns the server-wide
settings. Other repositories ship *fragments* that only describe their own
categories and roles and are applied with ``--guild-config`` pointing at the
guild config.

Design rules:
  * ``plan`` and ``apply`` run the very same code path; ``plan`` only skips the
    mutating requests, so the plan is exactly what ``apply`` would do.
  * Nothing is ever deleted unless the entry says ``delete: true``.
  * Objects are identified by name; a rename lists the old name under
    ``previous_names`` so the channel keeps its ID and history.
  * A config only touches the categories it declares (and what is inside them).

The bot token is read from the DISCORD_BOT_TOKEN environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/Rykon00/gregtorio-me-network_discord-bot, 1.0)"

PERMISSIONS = {
    "create_instant_invite": 0,
    "kick_members": 1,
    "ban_members": 2,
    "administrator": 3,
    "manage_channels": 4,
    "manage_guild": 5,
    "add_reactions": 6,
    "view_audit_log": 7,
    "priority_speaker": 8,
    "stream": 9,
    "view_channel": 10,
    "send_messages": 11,
    "send_tts_messages": 12,
    "manage_messages": 13,
    "embed_links": 14,
    "attach_files": 15,
    "read_message_history": 16,
    "mention_everyone": 17,
    "use_external_emojis": 18,
    "view_guild_insights": 19,
    "connect": 20,
    "speak": 21,
    "mute_members": 22,
    "deafen_members": 23,
    "move_members": 24,
    "use_vad": 25,
    "change_nickname": 26,
    "manage_nicknames": 27,
    "manage_roles": 28,
    "manage_webhooks": 29,
    "manage_guild_expressions": 30,
    "use_application_commands": 31,
    "request_to_speak": 32,
    "manage_events": 33,
    "manage_threads": 34,
    "create_public_threads": 35,
    "create_private_threads": 36,
    "use_external_stickers": 37,
    "send_messages_in_threads": 38,
    "use_embedded_activities": 39,
    "moderate_members": 40,
    "view_creator_monetization_analytics": 41,
    "use_soundboard": 42,
    "create_guild_expressions": 43,
    "create_events": 44,
    "use_external_sounds": 45,
    "send_voice_messages": 46,
    "set_voice_channel_status": 48,
    "send_polls": 49,
    "use_external_apps": 50,
    "pin_messages": 51,
    "bypass_slowmode": 52,
}
PERMISSION_NAMES = {bit: name for name, bit in PERMISSIONS.items()}

TEXT, VOICE, CATEGORY, ANNOUNCEMENT, FORUM = 0, 2, 4, 5, 15
CHANNEL_TYPES = {"text": TEXT, "voice": VOICE, "announcement": ANNOUNCEMENT, "forum": FORUM}
TYPE_NAMES = {value: name for name, value in CHANNEL_TYPES.items()}
TEXT_LIKE = {TEXT, ANNOUNCEMENT, FORUM}
VOICE_LIKE = {VOICE}
# Created after the server settings step, because they depend on (or are
# usually paired with) the Community feature.
LATE_TYPES = {ANNOUNCEMENT, FORUM}

VERIFICATION_LEVELS = {"none": 0, "low": 1, "medium": 2, "high": 3, "very_high": 4}
NOTIFICATION_LEVELS = {"all": 0, "mentions": 1}
CONTENT_FILTERS = {"disabled": 0, "members_without_roles": 1, "all_members": 2}
SYSTEM_MESSAGE_FLAGS = {"join": 1 << 0, "boost": 1 << 1, "tips": 1 << 2, "join_replies": 1 << 3}
FORUM_SORT = {"latest_activity": 0, "creation_date": 1}
FORUM_LAYOUT = {"default": 0, "list": 1, "gallery": 2}
FLAG_REQUIRE_TAG = 1 << 4
FLAG_SUPPRESS_EMBEDS = 1 << 2

MESSAGE_LIMIT = 2000
TOPIC_LIMIT = {TEXT: 1024, ANNOUNCEMENT: 1024, FORUM: 4096}
MAX_TAGS = 20
TAG_NAME_LIMIT = 20

READ_ONLY_DENY = ("send_messages", "send_messages_in_threads", "create_public_threads", "create_private_threads")
WRITER_ALLOW = ("send_messages", "send_messages_in_threads", "create_public_threads")

TOP_KEYS = {"guild_id", "server", "everyone", "roles", "categories"}
SERVER_KEYS = {
    "name", "description", "community", "rules_channel", "updates_channel", "system_channel",
    "suppress_system_messages", "verification_level", "default_notifications", "content_filter", "locale",
}
ROLE_KEYS = {"name", "previous_names", "color", "hoist", "mentionable", "permissions"}
ACCESS_KEYS = {"read_only", "private", "visible_to", "writers", "overwrites"}
CATEGORY_KEYS = {"name", "previous_names", "position", "channels", "delete"} | ACCESS_KEYS
CHANNEL_KEYS = {
    "name", "type", "previous_names", "topic", "slowmode", "messages", "tags", "require_tag",
    "sort", "layout", "default_reaction", "user_limit", "delete",
} | ACCESS_KEYS
MESSAGE_KEYS = {"file", "pin", "embeds"}
OVERWRITE_KEYS = {"role", "allow", "deny"}
TAG_KEYS = {"name", "moderated", "emoji"}

TEXT_CHANNEL_NAME = re.compile(r"^[a-z0-9_\-]{1,100}$")
PLACEHOLDER = re.compile(r"\{\{([#@])([^{}]+)\}\}")


class ConfigError(Exception):
    """The layout file is invalid."""


class SyncError(Exception):
    """The layout cannot be reconciled with the live server."""


class ApiError(Exception):
    def __init__(self, status, method, path, payload):
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {}
        detail = self.payload.get("message") or str(payload)[:300]
        code = self.payload.get("code")
        errors = self.payload.get("errors")
        text = f"Discord API {status} on {method} {path}: {detail}"
        if code is not None:
            text += f" (code {code})"
        if errors:
            text += f" {json.dumps(errors)[:600]}"
        super().__init__(text)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def permission_bits(names, where):
    bits = 0
    for name in _list(names, where):
        key = str(name).lower()
        if key not in PERMISSIONS:
            raise ConfigError(f"{where}: unknown permission '{name}'")
        bits |= 1 << PERMISSIONS[key]
    return bits


def permission_names(bits):
    return [PERMISSION_NAMES.get(bit, f"bit_{bit}") for bit in range(64) if bits >> bit & 1]


def _list(value, where):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected a list")
    return value


def _check_keys(mapping, allowed, where):
    if not isinstance(mapping, dict):
        raise ConfigError(f"{where}: expected a mapping")
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(map(str, unknown))}")


def _check_choice(mapping, key, choices, where):
    if key in mapping and mapping[key] not in choices:
        raise ConfigError(f"{where}: {key} must be one of {', '.join(choices)}")


def _check_name(mapping, where):
    name = mapping.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where}: missing name")
    for old in _list(mapping.get("previous_names"), f"{where}.previous_names"):
        if not isinstance(old, str):
            raise ConfigError(f"{where}: previous_names must be strings")
    return name


def _check_access(mapping, where):
    for key in ("read_only", "private"):
        if key in mapping and not isinstance(mapping[key], bool):
            raise ConfigError(f"{where}: {key} must be true or false")
    for key in ("visible_to", "writers"):
        for role in _list(mapping.get(key), f"{where}.{key}"):
            if not isinstance(role, str):
                raise ConfigError(f"{where}: {key} must be a list of role names")
    for index, entry in enumerate(_list(mapping.get("overwrites"), f"{where}.overwrites")):
        here = f"{where}.overwrites[{index}]"
        _check_keys(entry, OVERWRITE_KEYS, here)
        if not isinstance(entry.get("role"), str):
            raise ConfigError(f"{here}: missing role")
        permission_bits(entry.get("allow"), here)
        permission_bits(entry.get("deny"), here)


def parse_color(value, where):
    if value is None:
        return 0
    if isinstance(value, int) and 0 <= value <= 0xFFFFFF:
        return value
    if isinstance(value, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", value):
        return int(value.lstrip("#"), 16)
    raise ConfigError(f"{where}: color must look like \"#E67E22\"")


def normalize_tag(tag, where):
    if isinstance(tag, str):
        tag = {"name": tag}
    _check_keys(tag, TAG_KEYS, where)
    name = tag.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= TAG_NAME_LIMIT:
        raise ConfigError(f"{where}: tag names must be 1-{TAG_NAME_LIMIT} characters")
    return {"name": name, "moderated": bool(tag.get("moderated", False)), "emoji": tag.get("emoji")}


class Config:
    """A parsed and validated layout file."""

    def __init__(self, path):
        self.path = Path(path)
        self.base_dir = self.path.parent
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"{self.path}: file not found") from None
        except yaml.YAMLError as error:
            raise ConfigError(f"{self.path}: invalid YAML: {error}") from None
        if raw is None:
            raw = {}
        _check_keys(raw, TOP_KEYS, str(self.path))
        self.guild_id = str(raw["guild_id"]) if raw.get("guild_id") is not None else None
        self.server = raw.get("server")
        self.everyone = raw.get("everyone")
        self.roles = _list(raw.get("roles"), "roles")
        self.categories = _list(raw.get("categories"), "categories")
        self._validate()

    @property
    def is_guild_config(self):
        return self.guild_id is not None

    def _validate(self):
        if self.guild_id is not None and not re.fullmatch(r"\d{15,22}", self.guild_id):
            raise ConfigError("guild_id must be the numeric server ID")
        if not self.is_guild_config:
            for key, value in (("server", self.server), ("everyone", self.everyone)):
                if value is not None:
                    raise ConfigError(f"'{key}' is only allowed in the guild config (the file with guild_id)")
        if self.server is not None:
            self._validate_server()
        if self.everyone is not None:
            _check_keys(self.everyone, {"allow", "deny"}, "everyone")
            permission_bits(self.everyone.get("allow"), "everyone.allow")
            permission_bits(self.everyone.get("deny"), "everyone.deny")

        seen_roles = set()
        for index, role in enumerate(self.roles):
            where = f"roles[{index}]"
            _check_keys(role, ROLE_KEYS, where)
            name = _check_name(role, where)
            if name in seen_roles or name == "@everyone":
                raise ConfigError(f"{where}: duplicate or reserved role name '{name}'")
            seen_roles.add(name)
            parse_color(role.get("color"), where)
            permission_bits(role.get("permissions"), where)

        seen_categories = set()
        seen_channels = {}
        for index, category in enumerate(self.categories):
            where = f"categories[{index}]"
            _check_keys(category, CATEGORY_KEYS, where)
            name = _check_name(category, where)
            where = f"category '{name}'"
            if name in seen_categories:
                raise ConfigError(f"{where}: declared twice")
            seen_categories.add(name)
            if "position" in category and not isinstance(category["position"], int):
                raise ConfigError(f"{where}: position must be an integer")
            _check_access(category, where)
            for channel_index, channel in enumerate(_list(category.get("channels"), f"{where}.channels")):
                self._validate_channel(channel, f"{where}.channels[{channel_index}]", seen_channels)

    def _validate_server(self):
        server = self.server
        _check_keys(server, SERVER_KEYS, "server")
        _check_choice(server, "verification_level", VERIFICATION_LEVELS, "server")
        _check_choice(server, "default_notifications", NOTIFICATION_LEVELS, "server")
        _check_choice(server, "content_filter", CONTENT_FILTERS, "server")
        for entry in _list(server.get("suppress_system_messages"), "server.suppress_system_messages"):
            if entry not in SYSTEM_MESSAGE_FLAGS:
                raise ConfigError(
                    f"server.suppress_system_messages: '{entry}' is not one of {', '.join(SYSTEM_MESSAGE_FLAGS)}"
                )
        if server.get("community"):
            for key in ("rules_channel", "updates_channel"):
                if not server.get(key):
                    raise ConfigError(f"server: community needs {key}")
            if server.get("verification_level", "none") == "none":
                raise ConfigError("server: community needs verification_level low or higher")
            if server.get("content_filter") != "all_members":
                raise ConfigError("server: community needs content_filter: all_members")

    def _validate_channel(self, channel, where, seen):
        _check_keys(channel, CHANNEL_KEYS, where)
        name = _check_name(channel, where)
        where = f"channel '{name}'"
        kind = channel.get("type", "text")
        if kind not in CHANNEL_TYPES:
            raise ConfigError(f"{where}: type must be one of {', '.join(CHANNEL_TYPES)}")
        type_id = CHANNEL_TYPES[kind]
        if type_id in TEXT_LIKE and not TEXT_CHANNEL_NAME.fullmatch(name):
            raise ConfigError(f"{where}: use lowercase letters, digits, '-' and '_' only (Discord normalizes the rest)")
        group = "voice" if type_id in VOICE_LIKE else "text"
        if (group, name) in seen:
            raise ConfigError(f"{where}: declared twice in this file")
        seen[(group, name)] = True
        _check_access(channel, where)
        topic = channel.get("topic")
        if topic is not None:
            if type_id not in TOPIC_LIMIT:
                raise ConfigError(f"{where}: {kind} channels have no topic")
            if len(str(topic)) > TOPIC_LIMIT[type_id]:
                raise ConfigError(f"{where}: topic is longer than {TOPIC_LIMIT[type_id]} characters")
        if "slowmode" in channel:
            if type_id not in (TEXT, FORUM):
                raise ConfigError(f"{where}: slowmode is only supported on text and forum channels")
            if not isinstance(channel["slowmode"], int) or not 0 <= channel["slowmode"] <= 21600:
                raise ConfigError(f"{where}: slowmode must be 0-21600 seconds")
        forum_only = [key for key in ("tags", "require_tag", "sort", "layout", "default_reaction") if key in channel]
        if forum_only and type_id != FORUM:
            raise ConfigError(f"{where}: {', '.join(forum_only)} only apply to forum channels")
        if type_id == FORUM:
            tags = [normalize_tag(tag, f"{where}.tags") for tag in _list(channel.get("tags"), f"{where}.tags")]
            if len(tags) > MAX_TAGS:
                raise ConfigError(f"{where}: at most {MAX_TAGS} tags")
            if len({tag["name"].lower() for tag in tags}) != len(tags):
                raise ConfigError(f"{where}: duplicate tag names")
            if channel.get("require_tag") and not tags:
                raise ConfigError(f"{where}: require_tag needs at least one tag")
            _check_choice(channel, "sort", FORUM_SORT, where)
            _check_choice(channel, "layout", FORUM_LAYOUT, where)
        if "user_limit" in channel and type_id != VOICE:
            raise ConfigError(f"{where}: user_limit only applies to voice channels")
        messages = _list(channel.get("messages"), f"{where}.messages")
        if messages and type_id not in (TEXT, ANNOUNCEMENT):
            raise ConfigError(f"{where}: managed messages need a text or announcement channel")
        keys = set()
        for index, message in enumerate(messages):
            here = f"{where}.messages[{index}]"
            _check_keys(message, MESSAGE_KEYS, here)
            if not isinstance(message.get("file"), str):
                raise ConfigError(f"{here}: missing file")
            content = self.read_message(message)
            key = first_line(content)
            if PLACEHOLDER.search(key):
                raise ConfigError(f"{here}: the first line identifies the message and must not contain placeholders")
            if key in keys:
                raise ConfigError(f"{here}: two messages in this channel start with the same first line")
            keys.add(key)
            # Mentions render as <#id> / <@&id>; assume a 25 character worst case.
            if len(PLACEHOLDER.sub("x" * 25, content)) > MESSAGE_LIMIT:
                raise ConfigError(f"{here}: {message['file']} is longer than {MESSAGE_LIMIT} characters")

    def read_message(self, message):
        path = self.base_dir / message["file"]
        try:
            content = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise ConfigError(f"message file not found: {path}") from None
        if not content:
            raise ConfigError(f"message file is empty: {path}")
        return content


def first_line(content):
    return content.strip().splitlines()[0].strip() if content.strip() else ""


def load_configs(config_path, guild_config_path=None):
    """Return (config, guild_config). They are the same object for the guild config itself."""
    config = Config(config_path)
    if guild_config_path is None or Path(guild_config_path).resolve() == Path(config_path).resolve():
        if not config.is_guild_config:
            raise ConfigError(f"{config.path}: no guild_id - pass --guild-config for a fragment")
        return config, config
    guild_config = Config(guild_config_path)
    if not guild_config.is_guild_config:
        raise ConfigError(f"{guild_config.path}: the guild config needs a guild_id")
    if config.is_guild_config:
        raise ConfigError(f"{config.path}: a fragment must not set guild_id")
    for kind, own, shared in (
        ("category", config.categories, guild_config.categories),
        ("role", config.roles, guild_config.roles),
    ):
        taken = set()
        for entry in shared:
            taken.add(entry["name"])
            taken.update(entry.get("previous_names") or [])
        for entry in own:
            for name in [entry["name"], *(entry.get("previous_names") or [])]:
                if name in taken:
                    raise ConfigError(f"{kind} '{name}' belongs to the guild config and cannot be managed by a fragment")
    return config, guild_config


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class HttpTransport:
    """Minimal Discord REST client with rate limit handling."""

    def __init__(self, token, reason=None):
        self._token = token
        self._reason = reason

    def request(self, method, path, body=None, query=None):
        url = API_BASE + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": f"Bot {self._token}", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._reason and method != "GET":
            headers["X-Audit-Log-Reason"] = urllib.parse.quote(self._reason[:400], safe=" ")
        for attempt in range(6):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
                    if response.headers.get("X-RateLimit-Remaining") == "0":
                        time.sleep(min(float(response.headers.get("X-RateLimit-Reset-After", "0") or 0), 10))
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                raw = error.read()
                try:
                    payload = json.loads(raw) if raw else {}
                except ValueError:
                    payload = {"message": raw[:300].decode("utf-8", "replace")}
                if error.code == 429 and attempt < 5:
                    delay = payload.get("retry_after") if isinstance(payload, dict) else None
                    time.sleep(min(float(delay or error.headers.get("Retry-After") or 1), 30) + 0.1)
                    continue
                if error.code >= 500 and attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ApiError(error.code, method, path, payload) from None
            except urllib.error.URLError as error:
                if attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise SyncError(f"cannot reach Discord: {error.reason}") from None
        raise SyncError(f"gave up on {method} {path} after repeated rate limits")


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------


def _snowflake(value):
    return int(value) if str(value).isdigit() else 1 << 70


def _overwrite_map(overwrites):
    """Role overwrites as {role_id: (allow, deny)}."""
    return {
        entry["id"]: (int(entry.get("allow") or 0), int(entry.get("deny") or 0))
        for entry in overwrites or []
        if int(entry.get("type", 0)) == 0
    }


def _tag_signature(tags):
    return [(tag["name"], bool(tag.get("moderated")), tag.get("emoji_name") or None) for tag in tags or []]


class Syncer:
    def __init__(self, transport, config, guild_config, apply=False):
        self.api = transport
        self.config = config
        self.guild_config = guild_config
        self.guild_id = guild_config.guild_id
        self.apply = apply
        self.changes = []
        self.notes = []
        self.deletions = 0
        self.me = None
        self.guild = None
        self.roles = []
        self.channels = []
        self._claimed = set()
        self._categories = {}  # config category name -> live category
        self._managed = {}  # (group, config channel name) -> live channel
        self._new = 0

    # -- plumbing ----------------------------------------------------------

    def _mutate(self, description, method, path, body=None, simulated=None):
        if self.apply:
            try:
                result = self.api.request(method, path, body)
            except ApiError as error:
                raise SyncError(f"could not {description}: {error}") from None
            self.changes.append(description)
            return result
        self.changes.append(description)
        return simulated

    def _fake_id(self, label):
        self._new += 1
        return f"new-{self._new}-{label}"

    @staticmethod
    def _is_new(identifier):
        return not str(identifier).isdigit()

    # -- entry point -------------------------------------------------------

    def run(self):
        self._load()
        self._sync_roles()
        self._sync_everyone()
        self._sync_categories()
        self._sync_channels(late=False)
        self._sync_guild()
        self._sync_channels(late=True)
        self._sync_deletions()
        self._sync_positions()
        self._sync_messages()
        self._report_unmanaged()

    def _load(self):
        try:
            self.me = self.api.request("GET", "/users/@me")
            self.guild = self.api.request("GET", f"/guilds/{self.guild_id}")
        except ApiError as error:
            if error.status == 401:
                raise SyncError("Discord rejected the bot token (401). Check the DISCORD_BOT_TOKEN secret.") from None
            if error.status in (403, 404):
                raise SyncError(
                    f"The bot cannot see server {self.guild_id} ({error.status}). Is it invited to that server?"
                ) from None
            raise
        self.roles = self.api.request("GET", f"/guilds/{self.guild_id}/roles")
        self.channels = self.api.request("GET", f"/guilds/{self.guild_id}/channels")

    # -- lookups -----------------------------------------------------------

    def _role_id(self, name):
        if name == "@everyone":
            return self.guild_id
        matches = [role for role in self.roles if role["name"] == name and role["id"] != self.guild_id]
        if len(matches) == 1:
            return matches[0]["id"]
        if not matches:
            raise SyncError(f"role '{name}' is referenced but does not exist (declare it under roles:)")
        raise SyncError(f"role name '{name}' is ambiguous on the server ({len(matches)} roles)")

    def _channel_id(self, name, purpose):
        managed = self._managed.get(("text", name))
        if managed is not None:
            return managed["id"]
        matches = [channel for channel in self.channels if channel["type"] in TEXT_LIKE and channel["name"] == name]
        if len(matches) == 1:
            return matches[0]["id"]
        if not matches:
            raise SyncError(f"{purpose}: channel '{name}' does not exist")
        raise SyncError(f"{purpose}: channel name '{name}' is ambiguous on the server")

    @staticmethod
    def _pick(tiers, what):
        for tier in tiers:
            if len(tier) == 1:
                return tier[0]
            if len(tier) > 1:
                raise SyncError(f"{what} matches {len(tier)} objects on the server; rename or remove the duplicates")
        return None

    def _find_role(self, entry):
        previous = entry.get("previous_names") or []
        pool = [role for role in self.roles if role["id"] != self.guild_id and not role.get("managed")]
        return self._pick(
            [[r for r in pool if r["name"] == entry["name"]], [r for r in pool if r["name"] in previous]],
            f"role '{entry['name']}'",
        )

    def _find_category(self, entry):
        previous = entry.get("previous_names") or []
        pool = [c for c in self.channels if c["type"] == CATEGORY and c["id"] not in self._claimed]
        return self._pick(
            [[c for c in pool if c["name"] == entry["name"]], [c for c in pool if c["name"] in previous]],
            f"category '{entry['name']}'",
        )

    def _find_channel(self, entry, parent_id):
        name = entry["name"]
        previous = entry.get("previous_names") or []
        group = VOICE_LIKE if CHANNEL_TYPES[entry.get("type", "text")] in VOICE_LIKE else TEXT_LIKE
        # Channels may be adopted from this config's own categories or from
        # outside any category, never from a category another config owns.
        scope = {category["id"] for category in self._categories.values()} | {None, parent_id}
        pool = [
            c for c in self.channels
            if c["type"] in group and c.get("parent_id") in scope and c["id"] not in self._claimed
        ]
        return self._pick(
            [
                [c for c in pool if c["name"] == name and c.get("parent_id") == parent_id],
                [c for c in pool if c["name"] == name],
                [c for c in pool if c["name"] in previous and c.get("parent_id") == parent_id],
                [c for c in pool if c["name"] in previous],
            ],
            f"channel '{name}'",
        )

    # -- roles -------------------------------------------------------------

    def _sync_roles(self):
        for entry in self.config.roles:
            where = f"role '{entry['name']}'"
            desired = {
                "name": entry["name"],
                "color": parse_color(entry.get("color"), where),
                "hoist": bool(entry.get("hoist", False)),
                "mentionable": bool(entry.get("mentionable", False)),
                "permissions": str(permission_bits(entry.get("permissions"), where)),
            }
            live = self._find_role(entry)
            if live is None:
                simulated = dict(desired, id=self._fake_id("role"), managed=False, position=1)
                created = self._mutate(
                    f"create role `{entry['name']}`", "POST", f"/guilds/{self.guild_id}/roles", desired, simulated
                )
                self.roles.append(created)
                continue
            changed = {
                key: value for key, value in desired.items()
                if (str(live.get(key)) if key == "permissions" else live.get(key)) != value
            }
            if not changed:
                continue
            parts = []
            for key in changed:
                if key == "name":
                    parts.append(f"rename → `{desired['name']}`")
                elif key == "permissions":
                    parts.append("permissions")
                else:
                    parts.append(key)
            updated = self._mutate(
                f"update role `{live['name']}`: {', '.join(parts)}",
                "PATCH", f"/guilds/{self.guild_id}/roles/{live['id']}", changed, dict(live, **changed),
            )
            live.update(updated)

    def _sync_everyone(self):
        settings = self.config.everyone
        if not settings:
            return
        role = next(role for role in self.roles if role["id"] == self.guild_id)
        current = int(role["permissions"])
        allow = permission_bits(settings.get("allow"), "everyone.allow")
        deny = permission_bits(settings.get("deny"), "everyone.deny")
        target = (current | allow) & ~deny
        if target == current:
            return
        parts = [f"+{name}" for name in permission_names(target & ~current)]
        parts += [f"-{name}" for name in permission_names(current & ~target)]
        body = {"permissions": str(target)}
        updated = self._mutate(
            f"update `@everyone` permissions: {', '.join(parts)}",
            "PATCH", f"/guilds/{self.guild_id}/roles/{self.guild_id}", body, dict(role, **body),
        )
        role.update(updated)

    # -- permission overwrites ---------------------------------------------

    def _overwrites(self, category, channel, live):
        """Desired overwrite list for a category (channel=None) or a channel in it."""

        def setting(key, default):
            if channel is not None and key in channel:
                return channel[key]
            return category.get(key, default)

        table = {}

        def add(role_name, allow=(), deny=(), where=""):
            role_id = self._role_id(role_name)
            allow_bits = permission_bits(list(allow), where)
            deny_bits = permission_bits(list(deny), where)
            old_allow, old_deny = table.get(role_id, (0, 0))
            table[role_id] = ((old_allow | allow_bits) & ~deny_bits, (old_deny | deny_bits) & ~allow_bits)

        if setting("private", False):
            add("@everyone", deny=["view_channel"])
            for role_name in setting("visible_to", []) or []:
                add(role_name, allow=["view_channel"])
        if setting("read_only", False):
            add("@everyone", deny=READ_ONLY_DENY)
            for role_name in setting("writers", []) or []:
                add(role_name, allow=WRITER_ALLOW)
        extra = list(category.get("overwrites") or [])
        if channel is not None:
            extra += list(channel.get("overwrites") or [])
        for entry in extra:
            add(entry["role"], entry.get("allow") or [], entry.get("deny") or [], "overwrites")

        result = [
            {"id": role_id, "type": 0, "allow": str(allow), "deny": str(deny)}
            for role_id, (allow, deny) in table.items()
            if allow or deny
        ]
        # Member-specific overwrites are set by moderators by hand; keep them.
        for entry in (live or {}).get("permission_overwrites") or []:
            if int(entry.get("type", 0)) == 1:
                result.append(entry)
        return result

    # -- categories --------------------------------------------------------

    def _sync_categories(self):
        for entry in self.config.categories:
            if entry.get("delete"):
                continue
            name = entry["name"]
            live = self._find_category(entry)
            overwrites = self._overwrites(entry, None, live)
            if live is None:
                body = {"name": name, "type": CATEGORY, "permission_overwrites": overwrites}
                if "position" in entry:
                    body["position"] = entry["position"]
                simulated = dict(body, id=self._fake_id("category"), parent_id=None)
                simulated.setdefault("position", 0)
                live = self._mutate(
                    f"create category **{name}**", "POST", f"/guilds/{self.guild_id}/channels", body, simulated
                )
                self.channels.append(live)
            else:
                changed, parts = {}, []
                if live["name"] != name:
                    changed["name"] = name
                    parts.append(f"rename → **{name}**")
                if _overwrite_map(live.get("permission_overwrites")) != _overwrite_map(overwrites):
                    changed["permission_overwrites"] = overwrites
                    parts.append("permissions")
                if changed:
                    updated = self._mutate(
                        f"update category **{live['name']}**: {', '.join(parts)}",
                        "PATCH", f"/channels/{live['id']}", changed, dict(live, **changed),
                    )
                    live.update(updated)
            self._claimed.add(live["id"])
            self._categories[name] = live

    # -- channels ----------------------------------------------------------

    def _desired_tags(self, entry, live):
        existing = {tag["name"].lower(): tag for tag in (live or {}).get("available_tags") or []}
        result, used = [], set()
        for raw in entry.get("tags") or []:
            tag = normalize_tag(raw, "tags")
            body = {"name": tag["name"], "moderated": tag["moderated"], "emoji_id": None, "emoji_name": tag["emoji"]}
            old = existing.get(tag["name"].lower())
            if old is not None:
                body["id"] = old["id"]
                used.add(tag["name"].lower())
            result.append(body)
        for key, old in existing.items():
            if key not in used:
                # Removing a tag strips it from every post; never do that implicitly.
                result.append(old)
                self.notes.append(f"forum #{entry['name']}: tag `{old['name']}` is not in the config (kept)")
        return result

    def _channel_body(self, category, entry, parent_id, live):
        type_id = CHANNEL_TYPES[entry.get("type", "text")]
        body = {
            "name": entry["name"],
            "type": type_id,
            "parent_id": parent_id,
            "permission_overwrites": self._overwrites(category, entry, live),
        }
        if type_id in TOPIC_LIMIT:
            body["topic"] = str(entry.get("topic") or "").strip()
        if type_id in (TEXT, FORUM):
            body["rate_limit_per_user"] = int(entry.get("slowmode", 0))
        if type_id == VOICE and "user_limit" in entry:
            body["user_limit"] = int(entry["user_limit"])
        if type_id == FORUM:
            body["available_tags"] = self._desired_tags(entry, live)
            flags = int((live or {}).get("flags") or 0) & ~FLAG_REQUIRE_TAG
            body["flags"] = flags | (FLAG_REQUIRE_TAG if entry.get("require_tag") else 0)
            if "sort" in entry:
                body["default_sort_order"] = FORUM_SORT[entry["sort"]]
            if "layout" in entry:
                body["default_forum_layout"] = FORUM_LAYOUT[entry["layout"]]
            if "default_reaction" in entry:
                body["default_reaction_emoji"] = {"emoji_id": None, "emoji_name": entry["default_reaction"]}
        return body

    def _channel_changes(self, live, body, category_name):
        changed, parts = {}, []
        for key, value in body.items():
            if key == "name":
                if live["name"] != value:
                    changed[key] = value
                    parts.append(f"rename → #{value}")
            elif key == "type":
                if live["type"] != value:
                    if {live["type"], value} != {TEXT, ANNOUNCEMENT}:
                        raise SyncError(
                            f"channel '{live['name']}' is a {TYPE_NAMES.get(live['type'], live['type'])} channel; "
                            f"Discord cannot convert it to {TYPE_NAMES[value]}"
                        )
                    changed[key] = value
                    parts.append(f"convert to {TYPE_NAMES[value]}")
            elif key == "parent_id":
                if live.get("parent_id") != value:
                    changed[key] = value
                    parts.append(f"move to **{category_name}**")
            elif key == "permission_overwrites":
                if _overwrite_map(live.get(key)) != _overwrite_map(value):
                    changed[key] = value
                    parts.append("permissions")
            elif key == "topic":
                if (live.get(key) or "").strip() != value:
                    changed[key] = value
                    parts.append("topic")
            elif key == "available_tags":
                if _tag_signature(live.get(key)) != _tag_signature(value):
                    changed[key] = value
                    old_names = {tag["name"] for tag in live.get(key) or []}
                    added = [tag["name"] for tag in value if tag["name"] not in old_names]
                    parts.append("tags" + (f" (+{', +'.join(added)})" if added else ""))
            elif key == "default_reaction_emoji":
                if ((live.get(key) or {}).get("emoji_name")) != value["emoji_name"]:
                    changed[key] = value
                    parts.append("default reaction")
            elif key == "flags":
                if int(live.get(key) or 0) != value:
                    changed[key] = value
                    parts.append("require tag")
            elif (live.get(key) or 0) != value:
                changed[key] = value
                parts.append({"rate_limit_per_user": "slowmode"}.get(key, key))
        return changed, parts

    def _sync_channels(self, late):
        for category in self.config.categories:
            if category.get("delete"):
                continue
            parent = self._categories[category["name"]]
            entries = [entry for entry in category.get("channels") or [] if not entry.get("delete")]
            for entry in entries:
                type_id = CHANNEL_TYPES[entry.get("type", "text")]
                if (type_id in LATE_TYPES) != late:
                    continue
                group = "voice" if type_id in VOICE_LIKE else "text"
                live = self._find_channel(entry, parent["id"])
                body = self._channel_body(category, entry, parent["id"], live)
                needs_community = type_id == ANNOUNCEMENT and (live is None or live["type"] != ANNOUNCEMENT)
                if needs_community and "COMMUNITY" not in (self.guild.get("features") or []):
                    raise SyncError(
                        f"channel '{entry['name']}': announcement channels need the Community feature. "
                        "Enable it in the guild config (server.community: true) and apply that first."
                    )
                if live is None:
                    siblings = [e for e in entries if (CHANNEL_TYPES[e.get("type", "text")] in VOICE_LIKE) == (group == "voice")]
                    body["position"] = siblings.index(entry)
                    simulated = dict(body, id=self._fake_id("channel"))
                    live = self._mutate(
                        f"create {TYPE_NAMES[type_id]} channel #{entry['name']} in **{category['name']}**",
                        "POST", f"/guilds/{self.guild_id}/channels", body, simulated,
                    )
                    self.channels.append(live)
                else:
                    changed, parts = self._channel_changes(live, body, category["name"])
                    if changed:
                        updated = self._mutate(
                            f"update #{live['name']}: {', '.join(parts)}",
                            "PATCH", f"/channels/{live['id']}", changed, dict(live, **changed),
                        )
                        live.update(updated)
                self._claimed.add(live["id"])
                self._managed[(group, entry["name"])] = live

    # -- server settings ---------------------------------------------------

    def _sync_guild(self):
        server = self.config.server
        if not server:
            return
        guild = self.guild
        body, parts = {}, []

        def want(key, value, label=None):
            if guild.get(key) != value:
                body[key] = value
                parts.append(label or key)

        if "name" in server:
            want("name", server["name"], f"name → {server['name']}")
        if "verification_level" in server:
            want("verification_level", VERIFICATION_LEVELS[server["verification_level"]],
                 f"verification level → {server['verification_level']}")
        if "default_notifications" in server:
            want("default_message_notifications", NOTIFICATION_LEVELS[server["default_notifications"]],
                 f"default notifications → {server['default_notifications']}")
        if "content_filter" in server:
            want("explicit_content_filter", CONTENT_FILTERS[server["content_filter"]],
                 f"content filter → {server['content_filter']}")
        if "system_channel" in server:
            want("system_channel_id", self._channel_id(server["system_channel"], "server.system_channel"),
                 f"system channel → #{server['system_channel']}")
        if "suppress_system_messages" in server:
            managed = sum(SYSTEM_MESSAGE_FLAGS.values())
            flags = int(guild.get("system_channel_flags") or 0) & ~managed
            for entry in server["suppress_system_messages"] or []:
                flags |= SYSTEM_MESSAGE_FLAGS[entry]
            want("system_channel_flags", flags, "system messages")

        features = list(guild.get("features") or [])
        community = "COMMUNITY" in features
        if server.get("community"):
            if not community:
                body["features"] = features + ["COMMUNITY"]
                parts.append("enable Community")
                community = True
            want("rules_channel_id", self._channel_id(server["rules_channel"], "server.rules_channel"),
                 f"rules channel → #{server['rules_channel']}")
            want("public_updates_channel_id", self._channel_id(server["updates_channel"], "server.updates_channel"),
                 f"community updates channel → #{server['updates_channel']}")
        if community:
            if "locale" in server:
                want("preferred_locale", server["locale"], f"locale → {server['locale']}")
            if "description" in server:
                want("description", server["description"], "description")
        if not body:
            return
        updated = self._mutate(
            f"update server settings: {', '.join(parts)}", "PATCH", f"/guilds/{self.guild_id}", body, dict(guild, **body)
        )
        guild.update(updated)

    # -- deletions ---------------------------------------------------------

    def _sync_deletions(self):
        for category in self.config.categories:
            parent = self._categories.get(category["name"])
            if category.get("delete"):
                parent = self._find_category(category)
            parent_id = parent["id"] if parent else None
            for entry in category.get("channels") or []:
                if not entry.get("delete") or parent is None:
                    continue
                live = self._find_channel(entry, parent_id)
                if live is None:
                    continue
                self._mutate(f"DELETE channel #{live['name']} (delete: true)", "DELETE", f"/channels/{live['id']}")
                self.deletions += 1
                self.channels.remove(live)
            if category.get("delete") and parent is not None:
                children = [c for c in self.channels if c.get("parent_id") == parent["id"]]
                if children:
                    raise SyncError(
                        f"category '{parent['name']}' still contains {len(children)} channel(s); "
                        "a category is only deleted once it is empty"
                    )
                self._mutate(f"DELETE category **{parent['name']}** (delete: true)", "DELETE", f"/channels/{parent['id']}")
                self.deletions += 1
                self.channels.remove(parent)

    # -- ordering ----------------------------------------------------------

    def _sync_positions(self):
        moves, labels = [], []
        for category in self.config.categories:
            live = self._categories.get(category["name"])
            if live is None:
                continue
            if "position" in category and live.get("position") != category["position"]:
                moves.append({"id": live["id"], "position": category["position"]})
                labels.append(f"**{category['name']}**")
                live["position"] = category["position"]
            for group in ("text", "voice"):
                wanted = [
                    self._managed[(group, entry["name"])]
                    for entry in category.get("channels") or []
                    if not entry.get("delete") and (group, entry["name"]) in self._managed
                    and (CHANNEL_TYPES[entry.get("type", "text")] in VOICE_LIKE) == (group == "voice")
                ]
                actual = sorted(wanted, key=lambda c: (c.get("position") or 0, _snowflake(c["id"])))
                if [c["id"] for c in actual] == [c["id"] for c in wanted]:
                    continue
                for index, channel in enumerate(wanted):
                    if channel.get("position") != index:
                        moves.append({"id": channel["id"], "position": index})
                        channel["position"] = index
                labels.append(f"channels in **{category['name']}**")
        if moves:
            self._mutate(f"reorder {', '.join(labels)}", "PATCH", f"/guilds/{self.guild_id}/channels", moves)

    # -- managed messages --------------------------------------------------

    def _render(self, content):
        def replace(match):
            kind, name = match.group(1), match.group(2).strip()
            if kind == "#":
                return f"<#{self._channel_id(name, 'message placeholder')}>"
            return f"<@&{self._role_id(name)}>"

        return PLACEHOLDER.sub(replace, content)

    def _bot_messages(self, channel_id):
        found = {}
        pins = self.api.request("GET", f"/channels/{channel_id}/messages/pins", query={"limit": 50})
        for item in (pins or {}).get("items") or []:
            message = dict(item["message"], pinned=True)
            found[message["id"]] = message
        for message in self.api.request("GET", f"/channels/{channel_id}/messages", query={"limit": 100}) or []:
            found.setdefault(message["id"], message)
        mine = [m for m in found.values() if (m.get("author") or {}).get("id") == self.me["id"]]
        by_key = {}
        for message in sorted(mine, key=lambda m: _snowflake(m["id"])):
            by_key.setdefault(first_line(message.get("content") or ""), message)
        return by_key

    def _sync_messages(self):
        for category in self.config.categories:
            if category.get("delete"):
                continue
            for entry in category.get("channels") or []:
                if entry.get("delete") or not entry.get("messages"):
                    continue
                channel = self._managed[("text", entry["name"])]
                existing = {} if self._is_new(channel["id"]) else self._bot_messages(channel["id"])
                for message in entry["messages"]:
                    self._sync_message(channel, entry["name"], message, existing)

    def _sync_message(self, channel, channel_name, message, existing):
        content = self._render(self.config.read_message(message))
        if len(content) > MESSAGE_LIMIT:
            raise SyncError(f"{message['file']} is {len(content)} characters; Discord allows {MESSAGE_LIMIT}")
        key = first_line(content)
        flags = 0 if message.get("embeds") else FLAG_SUPPRESS_EMBEDS
        body = {"content": content, "flags": flags, "allowed_mentions": {"parse": []}}
        pin = bool(message.get("pin", False))
        base = f"/channels/{channel['id']}/messages"
        live = existing.get(key)
        label = f"`{message['file']}` in #{channel_name}"
        if live is None:
            live = self._mutate(f"post message {label}", "POST", base, body, dict(body, id=self._fake_id("message")))
            live = dict(live or {}, pinned=False)
        elif (live.get("content") or "").strip() != content or int(live.get("flags") or 0) & FLAG_SUPPRESS_EMBEDS != flags:
            self._mutate(f"edit message {label}", "PATCH", f"{base}/{live['id']}", body)
        if pin and not live.get("pinned"):
            self._mutate(f"pin message {label}", "PUT", f"{base}/pins/{live['id']}")
        elif not pin and live.get("pinned"):
            self._mutate(f"unpin message {label}", "DELETE", f"{base}/pins/{live['id']}")

    # -- reporting ---------------------------------------------------------

    def _report_unmanaged(self):
        parents = {category["id"]: name for name, category in self._categories.items()}
        for channel in sorted(self.channels, key=lambda c: (c.get("position") or 0, _snowflake(c["id"]))):
            if channel.get("parent_id") in parents and channel["id"] not in self._claimed:
                self.notes.append(
                    f"#{channel['name']} in **{parents[channel['parent_id']]}** is not in the config (left untouched)"
                )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render_summary(syncer, config, mode, error=None):
    server_name = (syncer.guild or {}).get("name", syncer.guild_id)
    lines = [f"## Discord sync: `{config.path}` → {server_name}", ""]
    applied = mode == "apply"
    if error is not None:
        lines += [f"**Failed:** {error}", ""]
    if syncer.changes:
        if applied and error is not None:
            title = f"Applied before the failure: {len(syncer.changes)} change(s)"
        elif applied:
            title = f"Applied {len(syncer.changes)} change(s)"
        else:
            title = f"Dry run: {len(syncer.changes)} change(s) would be applied"
        lines += [f"**{title}**", ""]
        lines += [f"- {change}" for change in syncer.changes]
    elif error is None:
        lines.append("**No changes.** The server matches the config.")
    if syncer.notes:
        lines += ["", "**Notes**", ""]
        lines += [f"- {note}" for note in syncer.notes]
    return "\n".join(lines) + "\n"


def write_summary(path, text):
    if path:
        Path(path).write_text(text, encoding="utf-8")


def write_outputs(**values):
    """Step outputs for the GitHub workflow (no-op outside of GitHub Actions)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")


def main(argv=None, transport=None):
    parser = argparse.ArgumentParser(description="Sync a Discord server with a YAML layout.")
    parser.add_argument("mode", choices=["validate", "plan", "apply"])
    parser.add_argument("--config", required=True, help="layout file to apply")
    parser.add_argument("--guild-config", help="guild config (required when --config is a fragment)")
    parser.add_argument("--summary-file", help="write a Markdown summary to this file")
    args = parser.parse_args(argv)

    try:
        config, guild_config = load_configs(args.config, args.guild_config)
    except ConfigError as error:
        text = f"## Discord sync: `{args.config}`\n\n**Invalid config:** {error}\n"
        print(text)
        write_summary(args.summary_file, text)
        write_outputs(mode=args.mode, ok=False)
        return 1

    if args.mode == "validate":
        text = f"## Discord sync: `{config.path}`\n\n**Config is valid.** (No connection to Discord was made.)\n"
        print(text)
        write_summary(args.summary_file, text)
        write_outputs(mode=args.mode, ok=True)
        return 0

    if transport is None:
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        if not token:
            print("DISCORD_BOT_TOKEN is not set.", file=sys.stderr)
            return 1
        reason = "discord-sync"
        if os.environ.get("GITHUB_REPOSITORY"):
            reason += f" {os.environ['GITHUB_REPOSITORY']}@{os.environ.get('GITHUB_SHA', '')[:7]}"
        transport = HttpTransport(token, reason)

    syncer = Syncer(transport, config, guild_config, apply=args.mode == "apply")
    error = None
    try:
        syncer.run()
    except (SyncError, ConfigError, ApiError) as caught:
        error = caught
    text = render_summary(syncer, config, args.mode, error)
    print(text)
    write_summary(args.summary_file, text)
    write_outputs(mode=args.mode, ok=error is None, changes=len(syncer.changes), deletions=syncer.deletions)
    return 1 if error is not None else 0


if __name__ == "__main__":
    sys.exit(main())
