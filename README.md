# Gregtorio & ME Network: Discord server as code

The layout of the Gregtorio & ME Network Discord server is
described in YAML files and applied by a small tool, `discord_sync.py`, through GitHub Actions.
Changing the server means changing a file and merging a pull request.

| Lives in | Describes |
| --- | --- |
| this repository, `server.yml` | server settings, general roles, the shared categories (Information, Community, Staff, Voice) |
| `Rykon00/Gregtorio`, `.discord/server.yml` | the Gregtorio category and its roles |
| `Rykon00/me-network`, `.discord/server.yml` | the ME Network category and its roles |

`server.yml` here is the **guild config**: it is the only file with a `guild_id` and the only one that
may change server-wide settings. The files in the mod repositories are **fragments**. A fragment can
only touch the categories and roles it declares itself.

## How a change gets applied

1. Edit the YAML (or a message under `content/`) on a branch and open a pull request.
2. The workflow runs a dry run against the live server and posts the plan as a comment, for example
   `create text channel #showcase in **Community**`.
3. Merging to `main` applies exactly that plan.

`plan` and `apply` run the same code; `plan` only skips the requests that change something.
Running `apply` twice in a row changes nothing the second time.

### Automatic merge

A pull request does not wait for someone to press the button when all of this holds:

- it comes from a branch of the repository itself, targets `main` and is not a draft,
- it only changes Discord files: `.discord/` in a mod repository; here the layout, `content/`, the
  tool, its tests and this README,
- the tests and the dry run against the server succeeded,
- the plan deletes nothing.

The workflow then merges it, applies it and posts the result as a second comment. Open a pull
request as a **draft** to hold it back. Deletions, changes to `.github/` or `requirements.txt` and
pull requests that also touch other files always wait for a manual merge.

## Safety rules

- **Nothing is deleted implicitly.** A channel, category, role or forum tag that disappears from the
  config stays on the server and is listed under "Notes". Deleting a channel needs `delete: true` on
  its entry; a category is only deleted once it is empty. Roles are never deleted.
- **Renames keep the channel.** Objects are found by name. To rename, change `name` and list the old
  one under `previous_names`; the channel keeps its ID, history and permissions.
- **Each file stays in its lane.** A config only looks at channels inside its own categories (or
  outside any category). It never adopts or changes a channel in a category another file owns.
- **Manual per-member permission overrides survive.** Role overrides on managed channels are owned
  by the config; overrides for individual members are left alone.
- **Managed messages never ping.** They are posted with mentions disabled.

## Config reference

```yaml
guild_id: "123..."            # guild config only

server:                       # guild config only
  name: My Server
  description: Shown on the invite page of a Community server.
  locale: en-US
  verification_level: low     # none | low | medium | high | very_high
  default_notifications: mentions   # all | mentions
  content_filter: all_members       # disabled | members_without_roles | all_members
  community: true             # required for announcement channels; never switched off by the tool
  rules_channel: rules
  updates_channel: moderators
  system_channel: general
  invite_channel: welcome     # keeps one permanent invite link to this channel
  suppress_system_messages: [tips]  # any of: join, boost, tips, join_replies

everyone:                     # guild config only; adjusts @everyone, leaves other bits alone
  deny: [mention_everyone]
  allow: []

roles:
  - name: Maintainer
    previous_names: []
    color: "#E67E22"
    hoist: true               # shown as its own group in the member list
    mentionable: false
    permissions: [kick_members, manage_messages]   # lowercase Discord permission names

categories:
  - name: Information
    previous_names: []
    position: 0               # order of categories on the server, lowest first
    read_only: true           # members can read but not write; inherited by the channels
    private: false            # hidden from @everyone ...
    visible_to: [Maintainer]  # ... except these roles (with private: true)
    writers: [Maintainer]     # roles that may still write (with read_only: true)
    overwrites:               # escape hatch for anything else
      - role: Tester
        allow: [attach_files]
        deny: []
    channels:
      - name: rules           # channels are ordered as listed
        type: text            # text (default) | announcement | forum | voice
        topic: The rules.
        slowmode: 0           # seconds, text and forum channels
        messages:             # text and announcement channels
          - file: content/rules.md   # path relative to this YAML file
            pin: false
            embeds: false     # show link previews
      - name: help
        type: forum
        topic: Post guidelines shown above the forum.
        tags: [question, bug, {name: solved, moderated: true, emoji: "✅"}]
        require_tag: true
        sort: latest_activity # latest_activity | creation_date
        layout: list          # default | list | gallery
        default_reaction: "👍"
      - name: old-channel
        delete: true          # the only way a channel gets deleted
```

Access settings (`read_only`, `private`, `visible_to`, `writers`) set on a channel replace the
value inherited from its category.

### Invite link

With `server.invite_channel` the tool keeps one invite link that never expires and has no use
limit, made by the bot, to that channel. The link is printed at the end of every run ("Invite
link: ..."). It stays the same from run to run. If someone deletes it in Discord, the next run
makes a new one with a different address, and every place that links the old one has to be updated.

### Managed messages

A message file is posted by the bot and edited in place when the file changes. The **first line**
of the file identifies the message, so keep it stable (a heading works well); changing it posts a
new message and leaves the old one. One file is one message, at most 2000 characters.

Placeholders are replaced with real mentions: `{{#channel-name}}` and `{{@Role Name}}`.

## Using it from a mod repository

Add the fragment as `.discord/server.yml`, the secret `DISCORD_BOT_TOKEN` to the repository, and
this workflow:

```yaml
name: Discord
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
    paths: [".discord/**", ".github/workflows/discord.yml"]
  push:
    branches: [main]
    paths: [".discord/**", ".github/workflows/discord.yml"]

permissions:
  contents: write             # read is enough without automerge
  pull-requests: write

jobs:
  sync:
    uses: Rykon00/gregtorio-me-network_discord-bot/.github/workflows/sync.yml@main
    with:
      config: .discord/server.yml
      mode: ${{ github.event_name == 'push' && 'apply' || 'plan' }}
      automerge: true         # merge and apply pull requests that only change .discord/
    secrets:
      DISCORD_BOT_TOKEN: ${{ secrets.DISCORD_BOT_TOKEN }}
```

A fragment looks like the `roles` and `categories` part of the guild config:

```yaml
roles:
  - name: ME Network Updates
    mentionable: true
categories:
  - name: ME Network
    position: 30              # 20-79 are reserved for the mod categories
    channels:
      - name: me-chat
      - name: me-releases
        type: announcement
        read_only: true
```

Pull requests from forks have no access to the token; for those the workflow only validates the
file.

## Release announcements

`discord_announce.py` posts a digest of one version's section of a Factorio `changelog.txt` to a
channel: the first entries of every category, as many as fit into one message, and links to the
full changelog and the mod portal. In an announcement channel the message is also published to the
servers that follow the channel. Announcing the same version again does nothing.

The release workflows of the mod repositories run it in a job of their own after a release (job
`announce` in their `.github/workflows/release.yml`). That job checks this repository out and calls
the script directly instead of using a reusable workflow, so the release pipelines do not depend on
the workflow files here: whatever happens to this repository, a release still goes through, and the
announcement can be re-run alone.

To preview an announcement, or to post one for a version that is out already, start the workflow
**Discord announcement** by hand in this repository (Actions tab). It starts as a dry run.

## Running it locally

```sh
pip install -r requirements.txt -r presence/requirements.txt
python -m unittest discover -s tests          # no network, runs against in-memory fakes
python discord_sync.py validate --config server.yml
DISCORD_BOT_TOKEN=... python discord_sync.py plan --config server.yml
```

For a fragment add `--guild-config path/to/this/repo/server.yml`.

## Server icon and bot avatar

`assets/server-icon.png` and `assets/bot-avatar.png` are the pictures of the server and of the bot.
Both are a crossover of the two mod thumbnails: the Gregtorio lettering and gear with the drive,
terminal and cable loop of ME Network. The avatar is the variant without anything important in the
corners, because Discord shows avatars as circles.

`tools/make_icons.py` builds them from the thumbnails (it needs Pillow and NumPy):

```sh
python tools/make_icons.py --gregtorio ../Gregtorio/thumbnail.png --me-network ../me-network/thumbnail.png
```

When one of the two files changes on `main`, the workflow **Discord branding** uploads them with
`discord_branding.py`: the server icon, the bot's avatar and the application icon. It can also be
started by hand. Discord only reports a hash of the pictures it holds, so the sync cannot compare
them with the files; that is why this is a step of its own and not part of `server.yml`.

## Keeping the bot online

Discord shows a bot as online only while something holds a Gateway connection for it. The tools
above talk to Discord for a few seconds and are gone again, so on their own the bot looks offline
all the time.

`presence/` is a small service that does nothing but hold that connection: it logs in with a
status ("Playing Factorio" by default), answers heartbeats and reconnects when the connection
drops. It asks for no intents, so it receives no messages and no member data. It runs as a Docker
container on a machine that is always on:

```sh
mkdir discord-presence && cd discord-presence
curl -fsSLO https://raw.githubusercontent.com/Rykon00/gregtorio-me-network_discord-bot/main/presence/compose.yml
curl -fsSLO https://raw.githubusercontent.com/Rykon00/gregtorio-me-network_discord-bot/main/presence/set-token.sh
sh set-token.sh        # asks for the bot token, writes .env, builds and starts the container
```

The status is set in `compose.yml` (`PRESENCE_TEXT`, `PRESENCE_TYPE`). `docker compose up -d --build
--pull always` updates the service to the current `main`. If Discord rejects the token, the
service waits an hour instead of retrying, because repeated failed logins make Discord reset a
token.

## Not managed (yet)

The server banner, role order, who has which role, onboarding and the welcome screen, AutoMod
rules, webhooks, emoji. Set these by hand in Discord; the tool does not touch them.

## The bot

The tool acts as the Discord application "Gregtorio & ME Network" and needs the Administrator
permission on the server. Its token is stored as the Actions secret `DISCORD_BOT_TOKEN` and must
never be committed or pasted anywhere else. If it leaks, reset it in the Discord Developer Portal
and update the secrets.

## License

GPLv3 (see `LICENSE`), like Gregtorio Continued and ME Network, the mods this server belongs to.

The pictures in `assets/` are put together from the thumbnails of the two mods: the Gregtorio logo
(lettering and gear) from Gregtorio Continued, and the drive, terminal and cables from ME Network,
whose graphics are made from [GT5-Unofficial](https://github.com/GTNewHorizons/GT5-Unofficial) by
GTNewHorizons (LGPL-3.0). See the License sections of the two mod repositories.
