#!/usr/bin/env python3
"""Set the server icon and the bot's avatar from picture files.

    discord_branding.py --guild-config server.yml \\
        --server-icon assets/server-icon.png --bot-avatar assets/bot-avatar.png

Discord only tells a hash of the pictures it holds, so this cannot check whether
they are up to date already; it uploads what it is given. The workflow therefore
runs it only when a picture file changes (or by hand). The avatar is also set as
the application's icon, which is what the Developer Portal and the invite screen
show. ``--dry-run`` only checks the files. The bot token is read from
DISCORD_BOT_TOKEN.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

from discord_sync import ApiError, Config, ConfigError, HttpTransport, SyncError

MAX_BYTES = 8 * 1024 * 1024
SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF8", "image/gif"))


class BrandingError(Exception):
    pass


def data_uri(path):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        raise BrandingError(f"picture not found: {path}") from None
    for signature, mime in SIGNATURES:
        if raw.startswith(signature):
            break
    else:
        raise BrandingError(f"{path} is not a PNG, JPEG or GIF picture")
    if len(raw) > MAX_BYTES:
        raise BrandingError(f"{path} is larger than {MAX_BYTES // (1024 * 1024)} MB")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def apply(transport, guild_id, server_icon=None, bot_avatar=None, dry_run=False):
    """Upload the pictures. Returns the list of things done, as text."""
    done = []
    icon = data_uri(server_icon) if server_icon else None
    avatar = data_uri(bot_avatar) if bot_avatar else None
    if dry_run:
        return [f"would set the {what} from `{path}`" for what, path in
                (("server icon", server_icon), ("bot avatar and application icon", bot_avatar)) if path]
    if icon:
        transport.request("PATCH", f"/guilds/{guild_id}", {"icon": icon})
        done.append(f"set the server icon from `{server_icon}`")
    if avatar:
        transport.request("PATCH", "/users/@me", {"avatar": avatar})
        done.append(f"set the bot avatar from `{bot_avatar}`")
        try:
            transport.request("PATCH", "/applications/@me", {"icon": avatar})
            done.append("set the application icon to the same picture")
        except ApiError as error:
            done.append(f"the application icon was not changed ({error}); set it in the Developer Portal if it matters")
    return done


def main(argv=None, transport=None):
    parser = argparse.ArgumentParser(description="Set the Discord server icon and the bot avatar.")
    parser.add_argument("--guild-config", required=True, help="the guild config (for the server ID)")
    parser.add_argument("--server-icon", help="picture for the server")
    parser.add_argument("--bot-avatar", help="picture for the bot")
    parser.add_argument("--dry-run", action="store_true", help="check the files, change nothing")
    parser.add_argument("--summary-file", help="write a Markdown summary to this file")
    args = parser.parse_args(argv)

    def finish(code, text):
        print(text)
        if args.summary_file:
            Path(args.summary_file).write_text(text + "\n", encoding="utf-8")
        return code

    try:
        if not args.server_icon and not args.bot_avatar:
            raise BrandingError("nothing to do: pass --server-icon and/or --bot-avatar")
        guild_id = Config(args.guild_config).guild_id
        if guild_id is None:
            raise BrandingError(f"{args.guild_config} has no guild_id")
        if transport is None and not args.dry_run:
            token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
            if not token:
                raise BrandingError("DISCORD_BOT_TOKEN is not set")
            transport = HttpTransport(token, "discord-branding")
        done = apply(transport, guild_id, args.server_icon, args.bot_avatar, dry_run=args.dry_run)
    except (BrandingError, ConfigError, SyncError, ApiError) as error:
        return finish(1, f"## Discord branding\n\n**Failed:** {error}")
    return finish(0, "## Discord branding\n\n" + "\n".join(f"- {line}" for line in done))


if __name__ == "__main__":
    sys.exit(main())
