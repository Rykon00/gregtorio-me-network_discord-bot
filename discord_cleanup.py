#!/usr/bin/env python3
"""One-off cleanup: delete Discord's "X joined the server" messages from a channel,
together with the "Wave to say hi!" sticker replies whose join message is gone.

    discord_cleanup.py --guild-config server.yml --channel general [--delete]

Only messages of the type "user join" and sticker-only replies to a deleted message
are touched, never anything else a person wrote.
Without ``--delete`` it only lists what it would remove. Deleted messages cannot
be restored. The bot token is read from DISCORD_BOT_TOKEN.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from discord_sync import ApiError, Config, ConfigError, HttpTransport, SyncError

USER_JOIN = 7
REPLY = 19


class CleanupError(Exception):
    pass


def find_channel(transport, guild_id, name):
    matches = [c for c in transport.request("GET", f"/guilds/{guild_id}/channels")
               if c.get("name") == name and c.get("type") in (0, 5)]
    if len(matches) != 1:
        raise CleanupError(f"expected one text channel named #{name}, found {len(matches)}")
    return matches[0]["id"]


def is_orphan_wave(message):
    """A sticker-only reply whose original message was deleted (a wave at a removed join message)."""
    return (message.get("type") == REPLY and message.get("sticker_items")
            and not message.get("content") and not message.get("referenced_message"))


def join_messages(transport, channel_id):
    """All user-join messages and orphaned wave replies of the channel, newest first."""
    found, before = [], None
    while True:
        query = {"limit": 100}
        if before:
            query["before"] = before
        page = transport.request("GET", f"/channels/{channel_id}/messages", query=query)
        if not page:
            return found
        found += [m for m in page if m.get("type") == USER_JOIN or is_orphan_wave(m)]
        before = page[-1]["id"]


def cleanup(transport, guild_id, channel, delete=False):
    channel_id = find_channel(transport, guild_id, channel)
    messages = join_messages(transport, channel_id)
    if delete:
        for message in messages:
            transport.request("DELETE", f"/channels/{channel_id}/messages/{message['id']}")
    return messages


def main(argv=None, transport=None):
    parser = argparse.ArgumentParser(description="Delete join messages from a channel.")
    parser.add_argument("--guild-config", required=True)
    parser.add_argument("--channel", required=True, help="channel name without #")
    parser.add_argument("--delete", action="store_true", help="really delete (default: list only)")
    parser.add_argument("--summary-file")
    args = parser.parse_args(argv)

    def finish(code, text):
        print(text)
        if args.summary_file:
            Path(args.summary_file).write_text(text + "\n", encoding="utf-8")
        return code

    try:
        guild_id = Config(args.guild_config).guild_id
        if guild_id is None:
            raise CleanupError(f"{args.guild_config} has no guild_id")
        if transport is None:
            token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
            if not token:
                raise CleanupError("DISCORD_BOT_TOKEN is not set")
            transport = HttpTransport(token, "discord-cleanup")
        messages = cleanup(transport, guild_id, args.channel, args.delete)
    except (CleanupError, ConfigError, SyncError, ApiError) as error:
        return finish(1, f"## Discord cleanup\n\n**Failed:** {error}")
    verb = "Deleted" if args.delete else "Would delete"
    lines = [f"## Discord cleanup\n\n{verb} {len(messages)} join message(s) and wave(s) in #{args.channel}."]
    if messages:
        names = ", ".join(sorted({m["author"].get("global_name") or m["author"]["username"] for m in messages}))
        lines.append(f"\nMembers: {names}")
    return finish(0, "\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
