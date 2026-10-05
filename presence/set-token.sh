#!/bin/sh
# Stores the bot token in .env next to compose.yml (readable by you only) and starts the service.
# The token is typed or pasted at a hidden prompt, so it does not end up in the shell history.
set -eu
cd "$(dirname "$0")"

printf 'Bot token (hidden, then Enter): '
trap 'stty echo' EXIT
stty -echo
IFS= read -r token
stty echo
trap - EXIT
echo
if [ -z "$token" ]; then
  echo "No token entered, nothing changed."
  exit 1
fi

umask 077
printf 'DISCORD_BOT_TOKEN=%s\n' "$token" > .env
unset token
docker compose up -d --build
echo "Started. The log should say 'online as ...' within a few seconds:"
sleep 6
docker compose logs --tail 5
