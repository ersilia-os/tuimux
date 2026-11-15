# Changelog

## Unreleased

- **Routines**: Claude prompts on a schedule, each on a fixed computer, run from the dashboard (`v` shows them). Grouped by computer; THIS RUN says whether the current period's run happened.
- **Unattended routines** (`mode = "unattended"`) run `claude -p` with an `allow` list of tools, close themselves, and show `done` or `failed`. A MODE column shows which kind each routine is.
- THIS RUN says what to do and since when (`answer it · since Mon`, `read it`, `done Mon ✓`, `scheduled`); amber means it waits on you, red that something broke, and only a question blinks.
- `space` on a routine opens its menu, as on a session, instead of pausing it; a paused routine says so even while a run is open (`answer it · paused`, `done Mon ✓ · paused`).
- The routine form is tidier: flat one-line fields, aligned hints, styled key hints.
- A calmer graphite look, with the sessions and routines columns aligned.
- More responsive over high-latency tailnets.
- The dashboard's settings (`TUIMUX_REFRESH`, the timeouts, `TUIMUX_PERSIST`, …) now work from `~/.config/tuimux/config` too, not only as environment variables.
- CI runs ruff and pytest on Python 3.11–3.13; pre-commit and Dependabot are set up. `app.py` is split into smaller modules.

## v0.3.0 — 2026-10-06

- Remote tabs reconnect after the connection drops (`TUIMUX_RECONNECT=off` disables it).
- `tuimux.__version__` reads the installed package metadata.

## v0.2.2 — 2026-07-29

- Refuse to run inside tmux, with a clear instruction to detach first.

## v0.2.1 — 2026-07-29

- Fix launching tuimux inside tmux, which dropped to a blank shell.

## v0.2.0 — 2026-07-29

- Multi-login hosts, an access-based view, and the org fleet view (`o`).
- A live preview of the highlighted session.
- Per-machine colours, an OWNER / SSH USER pair of columns, and a clearer status model.
- First run enables autostart and mouse scroll once.
