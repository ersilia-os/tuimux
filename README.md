# A TUI for Ersilia's Tailnet

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)

A terminal dashboard to see, open, and keep awake `tmux` sessions across every
machine on Ersilia's [Tailscale](https://tailscale.com) tailnet — over Tailscale SSH, from
one screen.

Built for [Ersilia](https://ersilia.io), where work is spread across shared laptops,
workstations, and servers on a private tailnet. It is internal tooling, shared here in case
it helps others too.

## Requirements

- `tmux` (required — tuimux is a front-end for it), plus `ssh`, `tailscale`, and a terminal.
- Python ≥ 3.9.

## Install

```sh
pip install git+https://github.com/ersilia-os/tuimux
```

## Usage

```sh
tuimux                              # the dashboard — all you normally need
tuimux attach [name]                # put this terminal into a tmux session (attach or create)
tuimux detach                       # detach this terminal; the session keeps running
tuimux autostart on|off|status      # auto-tmux every new local terminal
tuimux mouse on|off|status          # tmux mouse mode (wheel scrolls the pane)
tuimux login [host user[,user…]]    # show/set the SSH user(s) per host
tuimux devices                      # list every device in the tailnet
tuimux doctor                       # check your setup
```

Open, rename, detach, close, and keep-awake all happen inside the dashboard. The footer lists the keys, and a live preview of the highlighted session shows under the table.

## Setup conventions

`tuimux` assumes a small, consistent tailnet. The simplest way to have everything work from the start:

- **Enable Tailscale SSH on every computer** — `sudo tailscale up --ssh`. Phones and tablets are status-only (never SSH'd).
- **Use the same SSH username (and password) on all machines.** `tuimux` logs in as your local `$USER` by default, so a consistent username makes every box reachable with no per-host setup. Override per host with `tuimux login` when needed.
- **Give each device a tailnet name matching its hostname**, so the dashboard names line up with the machines.
- **Tag shared machines that several people log into with `tag:dev`** — the Tailscale ACL tag we use for inbound multi-user access.

## About the Ersilia Open Source Initiative

The [Ersilia Open Source Initiative](https://ersilia.io) is a tech-nonprofit organization fueling sustainable research in the Global South. Ersilia's main asset is the [Ersilia Model Hub](https://github.com/ersilia-os/ersilia), an open-source repository of AI/ML models for antimicrobial drug discovery.

![Ersilia Logo](assets/Ersilia_Brand.png)
