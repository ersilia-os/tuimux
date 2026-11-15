"""The live preview of the highlighted session, and the bottom panel switch."""

from rich.text import Text
from textual import work

from .fmt import (
    _preview_tail,
)
from .hosts import (
    PANELS,
    PEEK_DEBOUNCE,
    PREVIEW_LINES,
    SNAPSHOT,
    peek,
    save_snapshot,
)


class Preview:
    """Mirrors the highlighted session's pane under the table (mixed into Tuimux)."""

    # ---- live preview ----
    # A panel under the table mirrors the highlighted session's pane — a glimpse
    # of what it's actually doing without attaching. It updates as you move the
    # cursor (only on a real selection change) and on every heartbeat, so the
    # selected session stays live. Capture is read-only (tmux capture-pane), so
    # it never steals focus or disturbs the session.
    def on_data_table_row_highlighted(self, event):
        """Follow the cursor: preview the highlighted session."""
        # Only the table you're in drives the preview (the other one re-renders
        # and moves its cursor in the background).
        if event.data_table is not self.focused:
            return
        if event.data_table is self._rtable:
            self._sync_routine_detail()
        else:
            self._sync_preview_target()

    def _sync_preview_target(self):
        # Point the preview at the current row, if it's a live session. Re-kick a
        # capture only when the target actually changes — re-renders that keep the
        # cursor on the same session shouldn't spawn a peek every time.
        m = self._cur()
        key = (
            (m["host"], m.get("login", ""), m["session"])
            if m and m.get("action") == "attach"
            else None
        )
        if key == self._preview_key:
            return
        self._preview_key = key
        if not self._preview_on:
            return
        if key is None:
            self._set_preview_idle()
        else:
            # Title updates immediately, with the last capture of this session if
            # we have one; a fresh capture replaces it once the cursor rests.
            self._preview.border_title = f"{key[2]} · {key[0]}"
            if key in self._peek_cache:
                self._paint_preview(self._peek_cache[key])
            else:
                self._preview.update(Text("  …", style="dim"))
            if self._peek_timer is not None:
                self._peek_timer.stop()
            self._peek_timer = self.set_timer(
                PEEK_DEBOUNCE, lambda: self._peek_one(key)
            )

    @work(thread=True, exclusive=True, group="peek")
    def _peek_one(self, key):
        host, login, session = key
        raw = peek(host, login, session)
        self.call_from_thread(self._show_preview, key, raw)

    def _show_preview(self, key, raw):
        self._peek_cache[key] = raw
        # Don't paint a capture that finished after the cursor moved on (exclusive
        # workers cancel queued peeks, but one already mid-SSH still returns).
        if not self._preview_on or key != self._preview_key:
            return
        self._paint_preview(raw)

    def _paint_preview(self, raw):
        # Show the most-recent lines that fit; size.height is 0 until first layout.
        height = self._preview.size.height or PREVIEW_LINES
        lines = _preview_tail(raw, height)
        if not lines:
            self._preview.update(Text("  (pane is empty)", style="dim italic"))
            return
        self._preview.update(Text.from_ansi("\n".join(lines)))

    def _set_preview_idle(self):
        self._preview.border_title = "preview"
        self._preview.update(
            Text("  Move to a session to preview its output.", style="dim italic")
        )

    def action_panel(self):
        """Cycle the bottom panel: preview, routines, nothing."""
        # v: preview → routines → none → preview
        self._set_panel(PANELS[(PANELS.index(self._panel) + 1) % len(PANELS)])

    def _set_panel(self, panel, save=True):
        self._panel = panel
        self._preview.display = panel == "preview"
        self.query_one("#routines-wrap").display = panel == "routines"
        if panel == "routines":
            self._rtable.focus()  # ready for n / e / x / space / enter
            self._sync_routine_detail()
        else:
            self._table.focus()
            # Force a re-target: the key may be stale from while it was hidden.
            self._preview_key = ("",)
            self._sync_preview_target()
        self._refresh_badge()
        if save:  # remember it now, not only on a clean exit
            save_snapshot(SNAPSHOT, self._hosts, self._snap, panel)
