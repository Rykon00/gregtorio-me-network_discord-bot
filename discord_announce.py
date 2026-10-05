#!/usr/bin/env python3
"""Announce a mod release in a Discord channel.

Builds a short digest of one version's section of a Factorio ``changelog.txt``
and posts it as the bot. Announcement channels are published, so servers that
follow the channel receive the message too.

    discord_announce.py --guild-config server.yml --channel gregtorio-releases \\
        --title "Gregtorio Continued" --version 0.5.1 --changelog changelog.txt \\
        --link "Full changelog=https://github.com/..." --link "Mod portal=https://mods.factorio.com/..."

Running it again for the same version does nothing: the first line of the
message identifies the release. ``--dry-run`` prints the message instead of
posting it. The bot token is read from DISCORD_BOT_TOKEN.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from discord_sync import (
    ANNOUNCEMENT,
    FLAG_SUPPRESS_EMBEDS,
    MESSAGE_LIMIT,
    TEXT,
    ApiError,
    Config,
    ConfigError,
    HttpTransport,
    SyncError,
    first_line,
)

MARKDOWN_SPECIAL = re.compile(r"([\\*_~`|])")


class AnnounceError(Exception):
    pass


def parse_changelog(text, version):
    """Return [(category, [entry, ...]), ...] of one version of a Factorio changelog."""
    sections, current, inside, found = [], None, False, False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("Version:"):
            inside = line.split(":", 1)[1].strip() == version
            found = found or inside
            continue
        if not inside or not line.strip() or set(line.strip()) == {"-"} or line.startswith("Date:"):
            continue
        category = re.fullmatch(r"\s{2}(\S.*):", line)
        if category:
            current = (category.group(1), [])
            sections.append(current)
            continue
        entry = re.fullmatch(r"\s{4}- (.*)", line)
        if entry and current is not None:
            current[1].append(entry.group(1).strip())
        elif current is not None and current[1] and line.startswith("      "):
            current[1][-1] += " " + line.strip()
    if not found:
        raise AnnounceError(f"the changelog has no section 'Version: {version}'")
    return [(name, entries) for name, entries in sections if entries]


def escape(text):
    return MARKDOWN_SPECIAL.sub(r"\\\1", text)


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:(") + "…"


def build_message(title, version, sections, links, limit=MESSAGE_LIMIT):
    """The announcement text: as much of the changelog as fits, then the links."""
    header = f"# {title} {version}"
    footer = " · ".join(f"[{label}]({url})" for label, url in links)

    def render(per_section, entry_limit):
        lines = [header]
        for name, entries in sections:
            lines.append(f"**{escape(name)}** ({len(entries)})" if len(entries) > 1 else f"**{escape(name)}**")
            for entry in entries[:per_section]:
                lines.append(f"- {escape(shorten(entry, entry_limit))}")
            if len(entries) > per_section:
                rest = len(entries) - per_section
                lines.append(f"- … and {rest} more" if per_section else f"- {rest} entries")
        if footer:
            lines += ["", footer]
        return "\n".join(lines)

    for per_section, entry_limit in [(5, 220), (4, 180), (3, 160), (3, 120), (2, 120), (2, 90), (1, 90), (0, 0)]:
        message = render(per_section, entry_limit)
        if len(message) <= limit:
            return message
    return shorten(render(0, 0), limit)


def find_channel(channels, name):
    matches = [c for c in channels if c["type"] in (TEXT, ANNOUNCEMENT) and c["name"] == name]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise AnnounceError(f"there is no text or announcement channel named '{name}' on the server")
    raise AnnounceError(f"the channel name '{name}' is ambiguous on the server")


def announce(transport, guild_id, channel_name, message, dry_run=False):
    """Post ``message`` unless it is there already. Returns a short status string."""
    me = transport.request("GET", "/users/@me")
    channel = find_channel(transport.request("GET", f"/guilds/{guild_id}/channels"), channel_name)
    base = f"/channels/{channel['id']}/messages"
    key = first_line(message)
    for existing in transport.request("GET", base, query={"limit": 100}) or []:
        if (existing.get("author") or {}).get("id") == me["id"] and first_line(existing.get("content") or "") == key:
            return f"Already announced in #{channel_name}; nothing posted."
    if dry_run:
        return f"Dry run: would post {len(message)} characters to #{channel_name}."
    posted = transport.request(
        "POST", base, {"content": message, "flags": FLAG_SUPPRESS_EMBEDS, "allowed_mentions": {"parse": []}}
    )
    status = f"Posted to #{channel_name}."
    if channel["type"] == ANNOUNCEMENT:
        try:
            transport.request("POST", f"{base}/{posted['id']}/crosspost")
            status = f"Posted to #{channel_name} and published to its followers."
        except ApiError as error:
            status += f" Publishing to followers failed ({error}); publish it by hand in Discord."
    return status


def main(argv=None, transport=None):
    parser = argparse.ArgumentParser(description="Announce a mod release on Discord.")
    parser.add_argument("--guild-config", required=True, help="the guild config (for the server ID)")
    parser.add_argument("--channel", required=True, help="channel name, e.g. gregtorio-releases")
    parser.add_argument("--title", required=True, help="mod title, e.g. \"Gregtorio Continued\"")
    parser.add_argument("--version", required=True)
    parser.add_argument("--changelog", required=True, help="path of the Factorio changelog.txt")
    parser.add_argument("--link", action="append", default=[], metavar="LABEL=URL")
    parser.add_argument("--dry-run", action="store_true", help="print the message, post nothing")
    parser.add_argument("--summary-file", help="write a Markdown summary to this file")
    args = parser.parse_args(argv)

    def finish(code, text):
        print(text)
        if args.summary_file:
            Path(args.summary_file).write_text(text + "\n", encoding="utf-8")
        return code

    try:
        links = []
        for link in args.link:
            label, separator, url = link.partition("=")
            if not separator or not url.startswith("https://"):
                raise AnnounceError(f"--link must look like 'Label=https://...', got '{link}'")
            links.append((label.strip(), url.strip()))
        guild_id = Config(args.guild_config).guild_id
        if guild_id is None:
            raise AnnounceError(f"{args.guild_config} has no guild_id")
        try:
            changelog = Path(args.changelog).read_text(encoding="utf-8")
        except FileNotFoundError:
            raise AnnounceError(f"changelog not found: {args.changelog}") from None
        message = build_message(args.title, args.version, parse_changelog(changelog, args.version), links)
        if transport is None:
            token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
            if not token:
                raise AnnounceError("DISCORD_BOT_TOKEN is not set")
            transport = HttpTransport(token, f"release announcement {args.title} {args.version}")
        status = announce(transport, guild_id, args.channel, message, dry_run=args.dry_run)
    except (AnnounceError, ConfigError, SyncError, ApiError) as error:
        return finish(1, f"## Discord announcement\n\n**Failed:** {error}")
    quoted = "\n".join(f"> {line}" for line in message.splitlines())
    return finish(0, f"## Discord announcement: {args.title} {args.version}\n\n**{status}**\n\n{quoted}")


if __name__ == "__main__":
    sys.exit(main())
