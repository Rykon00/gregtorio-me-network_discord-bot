#!/usr/bin/env python3
"""Keep the bot online.

A bot only shows as online while something holds a Gateway connection for it.
The sync and announcement tools talk to Discord over REST and are gone again
after a few seconds, so the bot would look offline all the time. This small
service does nothing but hold that connection: it identifies with a status,
answers heartbeats and reconnects when the connection drops. It asks for no
intents, so it receives no messages and no member data.

Environment:
  DISCORD_BOT_TOKEN   the bot token (required)
  PRESENCE_TEXT       status text, default "Factorio"
  PRESENCE_TYPE       playing (default), watching, listening, competing or custom
  PRESENCE_ALIVE_FILE touched on every heartbeat acknowledgement (for a health check)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import sys
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake

GATEWAY_URL = "wss://gateway.discord.gg"
QUERY = "/?v=10&encoding=json"
ACTIVITY_TYPES = {"playing": 0, "listening": 2, "watching": 3, "custom": 4, "competing": 5}

DISPATCH, HEARTBEAT, IDENTIFY, RESUME, RECONNECT, INVALID_SESSION, HELLO, HEARTBEAT_ACK = 0, 1, 2, 6, 7, 9, 10, 11

# Close codes after which trying again cannot help.
FATAL_CLOSE_CODES = {
    4004: "Discord rejected the bot token",
    4010: "invalid shard",
    4011: "sharding required",
    4012: "invalid API version",
    4013: "invalid intents",
    4014: "disallowed intents",
}
# Close codes that end the session: the next connection identifies anew instead of resuming.
NEW_SESSION_CLOSE_CODES = {1000, 1001, 4007, 4009}

log = logging.getLogger("discord-presence")


class Fatal(Exception):
    """Reconnecting would not help (bad token, protocol error on our side)."""


def presence_payload(text, kind="playing"):
    kind = (kind or "playing").lower()
    if kind not in ACTIVITY_TYPES:
        raise Fatal(f"PRESENCE_TYPE must be one of {', '.join(ACTIVITY_TYPES)}, got '{kind}'")
    activities = []
    if text:
        if kind == "custom":
            activities.append({"name": "Custom Status", "type": ACTIVITY_TYPES[kind], "state": text})
        else:
            activities.append({"name": text, "type": ACTIVITY_TYPES[kind]})
    return {"since": None, "activities": activities, "status": "online", "afk": False}


class Presence:
    def __init__(self, token, text="Factorio", kind="playing", gateway_url=GATEWAY_URL, alive_file=None,
                 backoff=(5, 300), settle=(1, 5), connect=websockets.connect):
        self.token = token
        self.settle = settle  # seconds to wait after INVALID_SESSION, as Discord asks
        self.presence = presence_payload(text, kind)
        self.gateway_url = gateway_url
        self.alive_file = Path(alive_file) if alive_file else None
        self.backoff_first, self.backoff_max = backoff
        self.connect = connect
        self.session_id = None
        self.resume_url = None
        self.sequence = None
        self.sessions = 0  # READY or RESUMED received, for the log and the tests
        self._stop = asyncio.Event()
        self._socket = None

    # -- public ------------------------------------------------------------

    async def run(self):
        """Stay connected until stop() is called. Raises Fatal when retrying is pointless."""
        delay = self.backoff_first
        while not self._stop.is_set():
            before = self.sessions
            try:
                await self._connection()
            except ConnectionClosed as closed:
                code = closed.rcvd.code if closed.rcvd else None
                if code in FATAL_CLOSE_CODES:
                    raise Fatal(f"{FATAL_CLOSE_CODES[code]} (close code {code})") from None
                if code in NEW_SESSION_CLOSE_CODES:
                    self._forget_session()
                log.warning("connection closed (code %s), reconnecting", code)
            except (OSError, asyncio.TimeoutError, InvalidHandshake) as error:
                log.warning("connection failed (%s), reconnecting", error)
            if self._stop.is_set():
                break
            # A connection that got as far as READY/RESUMED was fine: start the back-off over.
            delay = self.backoff_first if self.sessions > before else min(delay * 2, self.backoff_max)
            try:
                await asyncio.wait_for(self._stop.wait(), delay * random.uniform(0.5, 1.0))
            except asyncio.TimeoutError:
                pass

    def stop(self):
        self._stop.set()
        if self._socket is not None:
            asyncio.ensure_future(self._socket.close(1000))

    # -- one connection ----------------------------------------------------

    def _forget_session(self):
        self.session_id = self.resume_url = self.sequence = None

    async def _send(self, socket, op, data):
        await socket.send(json.dumps({"op": op, "d": data}))

    async def _connection(self):
        resuming = self.session_id is not None and self.resume_url is not None
        url = (self.resume_url if resuming else self.gateway_url).rstrip("/") + QUERY
        async with self.connect(url, max_size=2 ** 22, open_timeout=30) as socket:
            self._socket = socket
            heartbeat = None
            try:
                hello = json.loads(await asyncio.wait_for(socket.recv(), 30))
                if hello.get("op") != HELLO:
                    raise Fatal(f"expected HELLO from the gateway, got op {hello.get('op')}")
                interval = hello["d"]["heartbeat_interval"] / 1000
                acknowledged = asyncio.Event()
                acknowledged.set()
                heartbeat = asyncio.ensure_future(self._heartbeats(socket, interval, acknowledged))
                if resuming:
                    await self._send(socket, RESUME, {"token": self.token, "session_id": self.session_id, "seq": self.sequence})
                else:
                    await self._send(socket, IDENTIFY, {
                        "token": self.token,
                        "intents": 0,
                        "properties": {"os": sys.platform, "browser": "discord-presence", "device": "discord-presence"},
                        "presence": self.presence,
                    })
                async for raw in socket:
                    message = json.loads(raw)
                    op = message.get("op")
                    if op == DISPATCH:
                        self.sequence = message.get("s", self.sequence)
                        if message.get("t") == "READY":
                            data = message["d"]
                            self.session_id = data["session_id"]
                            self.resume_url = data.get("resume_gateway_url") or self.gateway_url
                            self.sessions += 1
                            log.info("online as %s", (data.get("user") or {}).get("username", "?"))
                        elif message.get("t") == "RESUMED":
                            self.sessions += 1
                            log.info("session resumed")
                    elif op == HEARTBEAT:
                        await self._send(socket, HEARTBEAT, self.sequence)
                    elif op == HEARTBEAT_ACK:
                        acknowledged.set()
                        if self.alive_file is not None:
                            self.alive_file.touch()
                    elif op == RECONNECT:
                        log.info("the gateway asked for a reconnect")
                        await socket.close(4000)  # not 1000: the session stays resumable
                        return
                    elif op == INVALID_SESSION:
                        if not message.get("d"):
                            self._forget_session()
                        log.info("session invalid, %s", "resuming" if message.get("d") else "identifying anew")
                        await asyncio.sleep(random.uniform(*self.settle))
                        await socket.close(4000)
                        return
                # the loop ends without an exception when the gateway closes normally
                if socket.close_code in NEW_SESSION_CLOSE_CODES:
                    self._forget_session()
            finally:
                self._socket = None
                if heartbeat is not None:
                    heartbeat.cancel()

    async def _heartbeats(self, socket, interval, acknowledged):
        try:
            await asyncio.sleep(interval * random.random())
            while True:
                if not acknowledged.is_set():
                    log.warning("no heartbeat acknowledgement, reconnecting")
                    await socket.close(4000)
                    return
                acknowledged.clear()
                await self._send(socket, HEARTBEAT, self.sequence)
                await asyncio.sleep(interval)
        except ConnectionClosed:
            pass  # the receiving side notices and reconnects


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    try:
        if not token:
            raise Fatal("DISCORD_BOT_TOKEN is not set")
        service = Presence(
            token,
            text=os.environ.get("PRESENCE_TEXT", "Factorio"),
            kind=os.environ.get("PRESENCE_TYPE", "playing"),
            alive_file=os.environ.get("PRESENCE_ALIVE_FILE"),
        )
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, service.stop)
        await service.run()
        log.info("stopped")
        return 0
    except Fatal as error:
        # Do not let a restart policy hammer Discord with a token it rejects:
        # too many failed logins make Discord reset the token.
        log.error("%s. Waiting an hour before giving up, fix the configuration and restart.", error)
        await asyncio.sleep(3600)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
