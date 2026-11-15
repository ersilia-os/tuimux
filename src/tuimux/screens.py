"""Dialogs and tables: confirm/ask/menu screens, the routine form, $EDITOR."""

import os
import shlex
import subprocess
import tempfile
from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Checkbox,
    DataTable,
    Input,
    Label,
    OptionList,
    Select,
    TextArea,
)
from textual.widgets.option_list import Option

from . import routines
from .theme import (
    AMBER,
    RED,
    STEEL,
)


def edit_in_editor(app, text):
    """Open `text` in $EDITOR (vi if unset), suspending the dashboard meanwhile.

    Parameters
    ----------
    app : textual.app.App
        The running app, suspended while the editor is open.
    text : str
        The text to edit.

    Returns
    -------
    str or None
        The edited text, or None if the editor couldn't run.
    """
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    fd, path = tempfile.mkstemp(prefix="tuimux-prompt-", suffix=".md")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        with app.suspend():
            rc = subprocess.run([*editor, path]).returncode
        if rc != 0:
            return None
        with open(path) as f:
            return f.read()
    except SuspendNotSupported:
        app.notify("can't open an editor from this terminal", severity="warning")
        return None
    except OSError:
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class Confirm(ModalScreen[bool]):
    """A yes/no dialog; dismisses with True on yes."""

    BINDINGS = [("escape", "no"), ("n", "no"), ("y", "yes")]

    def __init__(self, message, ok_label="✕   yes, shut it down"):
        super().__init__()
        self.message = message
        self.ok_label = ok_label

    def compose(self) -> ComposeResult:
        """Lay out the message and the yes/cancel options."""
        with Vertical(id="dialog"):
            yield Label(self.message, id="dialog-msg")
            yield OptionList(
                Option("↩   cancel", id="cancel"),
                Option(self.ok_label, id="ok"),
                id="menu-list",
            )

    def on_mount(self):
        """Focus the options."""
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, e: OptionList.OptionSelected) -> None:
        """Dismiss with True when yes was chosen."""
        self.dismiss(e.option.id == "ok")

    def action_yes(self):
        """Answer yes."""
        self.dismiss(True)

    def action_no(self):
        """Answer no."""
        self.dismiss(False)


class Ask(ModalScreen[str]):
    """A one-line text prompt; dismisses with the text, or "" on escape."""

    BINDINGS = [("escape", "cancel")]

    def __init__(self, prompt, default=""):
        super().__init__()
        self.prompt = prompt
        self.default = default

    def compose(self) -> ComposeResult:
        """Lay out the prompt and its input."""
        with Vertical(id="dialog"):
            yield Label(self.prompt, id="dialog-msg")
            yield Input(value=self.default, id="dialog-input")

    def on_mount(self):
        """Focus the input."""
        self.query_one(Input).focus()

    def on_input_submitted(self, e: Input.Submitted) -> None:
        """Dismiss with the typed text."""
        self.dismiss(e.value.strip())

    def action_cancel(self):
        """Dismiss without a choice."""
        self.dismiss("")


class Menu(ModalScreen[str]):
    """The actions for a row (``space``); dismisses with the chosen action id.

    Parameters
    ----------
    title : str
        What the menu is about: a session or a routine name.
    options : list of tuple
        ``(label, action_id)`` pairs, in display order.
    """

    BINDINGS = [("escape", "cancel")]

    def __init__(self, title, options):
        super().__init__()
        self.title_text = title
        self.options = options

    def compose(self) -> ComposeResult:
        """Lay out the title and its actions."""
        with Vertical(id="dialog"):
            yield Label(self.title_text, id="dialog-msg")
            yield OptionList(
                *[Option(t, id=i) for t, i in self.options], id="menu-list"
            )

    def on_mount(self):
        """Focus the actions."""
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, e: OptionList.OptionSelected) -> None:
        """Dismiss with the chosen action."""
        self.dismiss(e.option.id)

    def action_cancel(self):
        """Dismiss without a choice."""
        self.dismiss(None)


class SessionTable(DataTable):
    """The sessions table: enter opens the highlighted session."""

    # DataTable already binds enter→select_cursor (hidden) which posts RowSelected;
    # we just make it visible on the footer as "open tab". Doing it here — on the
    # focused widget — instead of an app-level priority binding is crucial: a
    # priority binding fires app-wide and would hijack Enter inside the modal
    # menus (selecting a menu option, submitting a rename), breaking them.
    # `right` (drill into the row → actions menu) is also bound here because the
    # table's own column-nav binding would otherwise shadow an app-level one.
    BINDINGS = [
        Binding("enter", "select_cursor", "open tab", key_display="enter"),
        Binding("right", "app.menu", "menu", show=False),  # app.* → App.action_menu
    ]


def _keys(*pairs):
    """A key-hint line styled like the footer: steel keys, muted actions."""
    out = Text()
    for i, (key, what) in enumerate(pairs):
        if i:
            out.append("   ")
        out.append(key, f"bold {STEEL}")
        out.append(f" {what}", "dim")
    return out


class RoutineForm(ModalScreen[dict]):
    """Add or edit a routine. Dismisses with the routine dict, or None."""

    BINDINGS = [
        ("escape", "cancel", "cancel"),
        ("ctrl+s", "save", "save"),
        # ctrl+o: the text fields keep ctrl+e (end of line) for themselves
        Binding("ctrl+o", "editor", "prompt in $EDITOR", priority=True),
    ]

    def __init__(self, routine, hosts, taken, title=None, check_claude=None):
        super().__init__()
        self.routine = routine
        self.hosts = hosts
        self.taken = taken  # names already used by other routines
        self.title_text = title or (
            "edit routine" if routine["name"] else "new routine"
        )
        # check_claude(host, callback) reports where claude lives on a computer
        self.check_claude = check_claude

    def compose(self) -> ComposeResult:
        """Lay out the routine's fields."""
        r = self.routine
        host = r["host"] if r["host"] in self.hosts else self.hosts[0]
        with Vertical(id="form"):
            yield Label(self.title_text, id="form-title")
            with Horizontal(classes="row"):
                with Vertical(classes="col"):
                    yield Label("name", classes="field")
                    yield Input(r["name"], placeholder="e.g. lit-digest", id="f-name")
                with Vertical(classes="col"):
                    yield Label("computer", classes="field")
                    yield Select(
                        [(h, h) for h in self.hosts],
                        value=host,
                        allow_blank=False,
                        id="f-host",
                    )
                    yield Label("", id="f-claude", classes="hint")
            with Horizontal(classes="row"):
                with Vertical(classes="col"):
                    yield Label("folder", classes="field")
                    yield Input(r["folder"], placeholder="~", id="f-folder")
                with Vertical(classes="col"):
                    yield Label("ssh login", classes="field")
                    yield Input(
                        r["login"], placeholder="the computer's default", id="f-login"
                    )
            yield Label("schedule", classes="field")
            yield Input(r["schedule"], placeholder="weekly mon 09:00", id="f-schedule")
            yield Label("", id="f-next", classes="hint")
            yield Label(
                Text.assemble(
                    "prompt", ("  ·  usually a skill, e.g. /literature-digest", "dim")
                ),
                classes="field",
            )
            yield TextArea(r["prompt"], id="f-prompt")
            yield Label("mode", classes="field")
            yield Select(
                (
                    (
                        "interactive  ·  Claude waits for you in its session",
                        "interactive",
                    ),
                    (
                        "unattended  ·  Claude runs alone; the run closes itself",
                        "unattended",
                    ),
                ),
                value=r.get("mode") or "interactive",
                allow_blank=False,
                id="f-mode",
            )
            yield Checkbox(
                "ask “done?” when finished  ·  yes closes the run",
                r.get("ask_done", True),
                id="f-ask-done",
            )
            yield Label(
                Text.assemble(
                    "allow", ("  ·  tools it may use; everything else is denied", "dim")
                ),
                classes="field",
                id="f-allow-label",
            )
            yield Input(
                r.get("allow", ""),
                placeholder="e.g. Read,Grep,Glob,WebFetch,Bash(gh *)",
                id="f-allow",
            )
            yield Label("", id="f-error")
            yield Label(
                _keys(
                    ("ctrl+s", "save"),
                    ("ctrl+o", "prompt in $EDITOR"),
                    ("esc", "cancel"),
                ),
                id="form-keys",
            )

    def on_mount(self):
        """Focus the name and show the schedule, computer and mode."""
        self.query_one("#f-name", Input).focus()
        self._preview_schedule(self.routine["schedule"])
        self._check(str(self.query_one("#f-host", Select).value))
        self._show_mode(self.routine.get("mode") or "interactive")

    def on_input_changed(self, e: Input.Changed) -> None:
        """Show what a schedule means as you type it."""
        if e.input.id == "f-schedule":
            self._preview_schedule(e.value)

    def on_select_changed(self, e: Select.Changed) -> None:
        """Check the chosen computer, or switch the mode's fields."""
        if e.select.id == "f-host":
            self._check(str(e.value))
        elif e.select.id == "f-mode":
            self._show_mode(str(e.value))

    def _show_mode(self, mode):
        # interactive asks "done?" at the end; unattended needs its allow list
        alone = mode == "unattended"
        self.query_one("#f-ask-done", Checkbox).display = not alone
        self.query_one("#f-allow-label", Label).display = alone
        self.query_one("#f-allow", Input).display = alone

    def _preview_schedule(self, text):
        # Live: when it would next run, or what's wrong with the phrase.
        hint = self.query_one("#f-next", Label)
        try:
            sched = routines.parse(text)
        except ValueError as err:
            msg = (
                f"{err}"
                if text.strip()
                else (
                    "daily 09:00 · weekdays 09:00 · weekly mon,thu 09:00 · "
                    "monthly 1 09:00 · every 6h"
                )
            )
            hint.update(Text(msg, style=AMBER if text.strip() else "dim"))
            return
        now = datetime.now()
        t = routines.next_slot(sched, now, now.timestamp())
        hint.update(
            Text(f"{routines.describe(text)}  ·  next run {t:%a %d %b, %H:%M}", "dim")
        )

    def _check(self, host):
        label = self.query_one("#f-claude", Label)
        if self.check_claude is None:
            label.update("")
            return
        label.update(Text("checking for claude…", "dim italic"))
        self.check_claude(host, self._show_claude)

    def _show_claude(self, host, where):
        # where: a path (found), "" (not there) or None (couldn't reach it)
        if not self.is_mounted or str(self.query_one("#f-host", Select).value) != host:
            return  # the form closed, or you picked another computer meanwhile
        label = self.query_one("#f-claude", Label)
        if where:
            label.update(Text("✓ claude found", "dim"))
        elif where == "":
            label.update(Text(f"✕ no claude on {host}", RED))
        else:
            label.update(Text(f"couldn't reach {host} to check for claude", "dim"))

    @staticmethod
    def _schedule_error(text):
        try:
            routines.parse(text)
        except ValueError as err:
            return f"schedule: {err}"
        return ""

    def _error(self, msg):
        err = self.query_one("#f-error", Label)
        err.update(Text(msg, style=AMBER))
        err.display = True  # hidden until there's something to say

    def action_editor(self):
        """Edit the prompt in $EDITOR."""
        area = self.query_one("#f-prompt", TextArea)
        text = edit_in_editor(self.app, area.text)
        if text is not None:
            area.text = text.strip("\n")

    def action_save(self):
        """Validate the form and dismiss with the routine."""
        r = {
            "name": self.query_one("#f-name", Input).value.strip(),
            "host": str(self.query_one("#f-host", Select).value),
            "login": self.query_one("#f-login", Input).value.strip(),
            "folder": self.query_one("#f-folder", Input).value.strip() or "~",
            "schedule": " ".join(self.query_one("#f-schedule", Input).value.split()),
            "enabled": self.routine["enabled"],
            "ask_done": self.query_one("#f-ask-done", Checkbox).value,
            "mode": str(self.query_one("#f-mode", Select).value),
            "allow": self.query_one("#f-allow", Input).value.strip(),
            "prompt": self.query_one("#f-prompt", TextArea).text.strip("\n"),
        }
        try:
            routines.check_name(r["name"])
        except ValueError as err:
            return self._error(str(err))
        if r["name"] in self.taken:
            return self._error(f"name: {r['name']} is already used")
        err = self._schedule_error(r["schedule"])
        if err:
            return self._error(err)
        if not r["prompt"].strip():
            return self._error("prompt: write what claude should start with")
        self.dismiss(r)

    def action_cancel(self):
        """Dismiss without a choice."""
        self.dismiss(None)


class RoutineTable(DataTable):
    """The routines panel. Its keys apply only while it has focus."""

    BINDINGS = [
        Binding("enter", "select_cursor", "run", key_display="enter"),
        Binding("n", "app.routine_new", "new"),
        Binding("e", "app.routine_edit", "edit"),
        Binding("d", "app.routine_done", "done"),
        # also there, kept out of the footer so it stays calm
        Binding("x", "app.routine_delete", "delete", show=False),
        Binding("space", "app.routine_menu", "menu"),
        Binding("c", "app.routine_copy", "duplicate", show=False),
        Binding("E", "app.routine_editor", "prompt in $EDITOR", show=False),
    ]
