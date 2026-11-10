# release-watch

Telegram alerts for node operators: new releases of the software you run, scheduled
Cosmos SDK upgrades, and a daily report of which of your nodes are behind.

One Python file, standard library only, run by a systemd timer. Built and used by
[MrsML](https://mrs-ml.website) to watch about 30 clients across 60+ nodes.

## What it sends

**New release**, within an hour of publication (GitHub or GitLab):

```
🆕 Cosmos Hub (gaiad) v29.1.0-rc0 (pre-release) ⚠️ important
Networks: cosmoshub-4, provider
Date: 2026-11-02
https://github.com/cosmos/gaia/releases/tag/v29.1.0-rc0
```

"Important" means the release notes mention a hard fork, a mandatory upgrade, an upgrade
height, breaking changes and so on (the regex is configurable).

**Scheduled upgrade** on a Cosmos chain, read from your own node (`x/upgrade` current plan),
with an ETA from the recent block time, and a reminder a few hours before:

```
⏫ Upgrade scheduled
cosmoshub-4: upgrade v29 at height 31000000
Current height 30871234, expected 14.11 15:20 UTC, in ~2.4 days
```

**Daily report**: checks and errors of the last 24 h, new releases, scheduled upgrades, and
installed versions compared with the latest releases:

```
🔎 Installed versions: up to date 58 of 61
⬆️ Behind the latest stable release:
• BTC — Bitcoin Core: running 31.1 → 31.2, released 2026-11-03, 4 days ago
🧪 Testnets with a newer pre-release:
• cosmoshub-testnet — Cosmos Hub (gaiad): running 29.0.0, pre-release 29.1.0-rc0 (2026-11-02)
```

**Announcements** outside GitHub: new posts in a Discourse forum search and changes to a
documentation page (with the added lines), checked once a day.

## Details that took a while to get right

- The first run records existing releases silently and sends one summary, so adding a
  source never floods the chat with its history.
- Monorepos (e.g. `ethereum-optimism/optimism`) are split per component with `tag_filter`.
- Projects that patch several release branches at once (Polkadot SDK) only report the
  newest stable branch: `branch_regex`.
- Versions are compared numerically, with `alpha < beta < rc < stable`; when the tag is not
  the node version (Polkadot SDK), it is read from the release notes: `version_body`.
- Installed versions are probed over a single SSH session per host. Probes can run inside
  Proxmox LXC containers (`ct = 108` → `pct exec 108`), and there are helpers for the common
  cases: binary from a systemd unit, Cosmos `/abci_info`, `web3_clientVersion`, docker image tag.
- The Cosmos upgrade plan is decoded from the raw ABCI query, so no chain binary or
  `grpcurl` is needed.

## Install

Requires Python 3.11+ and a Telegram bot ([@BotFather](https://t.me/BotFather)) added to your
chat or group.

```sh
sudo install -D -m 755 release_watch.py /opt/release-watch/release_watch.py
sudo install -D -m 644 config.example.toml /etc/release-watch/config.toml
sudo install -m 600 release-watch.env.example /etc/release-watch.env
sudo editor /etc/release-watch/config.toml /etc/release-watch.env

# try it without sending anything
sudo python3 /opt/release-watch/release_watch.py --dry-run
rm -f /var/lib/release-watch/state.json   # so the real first run sends the summary

sudo cp systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now release-watch.timer release-watch-daily.timer
```

Get the chat id by sending a message to the group and opening
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

Without `GITHUB_TOKEN` GitHub allows 60 API requests per hour per IP, and every `[[watch]]`
entry is one request per run. With more than ~50 sources, or other tools on the same IP,
add a token (a fine-grained token with no permissions is enough).

For version probes and upgrade plans the machine needs key-based SSH access to the hosts
(`BatchMode=yes`, so a missing key fails instead of hanging).

## Configure

Everything is in [`config.example.toml`](config.example.toml), with comments: release
sources, hosts, installed-version probes, Cosmos chains to watch, forum searches and pages.

Adding a network usually means one `[[watch]]` entry, one `[[node]]` entry per node that
runs it, and a `[[plan]]` entry for a Cosmos chain.

## License

MIT
