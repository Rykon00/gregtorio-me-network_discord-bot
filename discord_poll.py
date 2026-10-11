#!/usr/bin/env python3
"""Post a Discord poll to a channel.

    discord_poll.py --guild-config server.yml --channel gregtorio-ideas \\
        --question "..." --answer "..." --answer "..." [--days 7] [--multi] [--dry-run]

A poll has fixed answers, members cannot add their own. Posting the same question
to the same channel again does nothing while the bot's earlier poll is among the
channel's last 50 messages. ``--dry-run`` only shows the poll. The bot token is
read from DISCORD_BOT_TOKEN.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from discord_sync import ApiError, Config, ConfigError, HttpTransport, SyncError

MAX_ANSWERS = 10
MAX_QUESTION = 300
MAX_ANSWER = 55
MAX_DAYS = 32


class PollError(Exception):
    pass


def build_poll(question, answers, days, multi):
    question = question.strip()
    answers = [a.strip() for a in answers if a.strip()]
    if not question or len(question) > MAX_QUESTION:
        raise PollError(f"the question needs 1 to {MAX_QUESTION} characters")
    if not 2 <= len(answers) <= MAX_ANSWERS:
        raise PollError(f"a poll needs 2 to {MAX_ANSWERS} answers, got {len(answers)}")
    for answer in answers:
        if len(answer) > MAX_ANSWER:
            raise PollError(f"answer longer than {MAX_ANSWER} characters: {answer!r}")
    if not 1 <= days <= MAX_DAYS:
        raise PollError(f"days must be between 1 and {MAX_DAYS}")
    return {
        "question": {"text": question},
        "answers": [{"poll_media": {"text": a}} for a in answers],
        "duration": days * 24,
        "allow_multiselect": bool(multi),
        "layout_type": 1,
    }


def post(transport, guild_id, channel, poll, dry_run=False):
    """Returns a line saying what was done."""
    channels = [c for c in transport.request("GET", f"/guilds/{guild_id}/channels")
                if c.get("name") == channel and c.get("type") in (0, 5)]
    if len(channels) != 1:
        raise PollError(f"expected one text channel named #{channel}, found {len(channels)}")
    channel_id = channels[0]["id"]
    me = transport.request("GET", "/users/@me")["id"]
    recent = transport.request("GET", f"/channels/{channel_id}/messages", query={"limit": 50}) or []
    for message in recent:
        if message.get("author", {}).get("id") == me and \
                (message.get("poll") or {}).get("question", {}).get("text") == poll["question"]["text"]:
            return f"the poll is posted in #{channel} already, nothing to do"
    if dry_run:
        return f"would post the poll in #{channel}"
    transport.request("POST", f"/channels/{channel_id}/messages", {"poll": poll})
    return f"posted the poll in #{channel}"


def main(argv=None, transport=None):
    parser = argparse.ArgumentParser(description="Post a Discord poll.")
    parser.add_argument("--guild-config", required=True)
    parser.add_argument("--channel", required=True, help="channel name without #")
    parser.add_argument("--question", required=True)
    parser.add_argument("--answer", action="append", default=[], help="repeat for every answer")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--multi", action="store_true", help="allow several answers per member")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary-file")
    args = parser.parse_args(argv)

    def finish(code, text):
        print(text)
        if args.summary_file:
            Path(args.summary_file).write_text(text + "\n", encoding="utf-8")
        return code

    try:
        poll = build_poll(args.question, args.answer, args.days, args.multi)
        guild_id = Config(args.guild_config).guild_id
        if guild_id is None:
            raise PollError(f"{args.guild_config} has no guild_id")
        if transport is None:
            token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
            if not token:
                raise PollError("DISCORD_BOT_TOKEN is not set")
            transport = HttpTransport(token, "discord-poll")
        line = post(transport, guild_id, args.channel, poll, args.dry_run)
    except (PollError, ConfigError, SyncError, ApiError) as error:
        return finish(1, f"## Discord poll\n\n**Failed:** {error}")
    answers = "\n".join(f"- {a['poll_media']['text']}" for a in poll["answers"])
    return finish(0, f"## Discord poll\n\n**{poll['question']['text']}**\n\n{answers}\n\n{line}")


if __name__ == "__main__":
    sys.exit(main())
