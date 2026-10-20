"""Persistent per-host shells for the dashboard's remote calls.

A Tailscale SSH handshake costs ~2 s, so instead of one connection per probe,
preview or action, the dashboard keeps one long-lived ``ssh host sh`` per
(host, login) and feeds it scripts, each ended by a sentinel line carrying its
exit code. It is one plain session, not ControlMaster multiplexing (Tailscale
SSH hangs multiplexed sessions). The scripts themselves come from the engine
(``__remote``), which stays the single source of truth for what runs remotely.
"""

import os
import select
import subprocess
import threading
import time
import uuid

TIMEOUT = "__TIMEOUT__"


def ssh_argv(host, login, opts):
    """Command line for a persistent shell on ``login@host``.

    Parameters
    ----------
    host, login : str
        Where to connect, and as whom.
    opts : list of str
        The engine's ssh options (``__remote sshopts``).

    Returns
    -------
    list of str
    """
    # Keepalives make a dead link (laptop slept, network changed) fail in ~30s
    # rather than hang, so the next call reconnects.
    keep = ["-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
    return ["ssh", "-T", *opts, *keep, f"{login}@{host}", "sh"]


class HostShell:
    """One long-lived shell on a host. Thread-safe; calls run one at a time.

    Parameters
    ----------
    argv : list of str
        Command that starts the shell (e.g. from `ssh_argv`); it must read
        commands from stdin.
    """

    def __init__(self, argv):
        self.argv = argv
        self._proc = None
        self._lock = threading.Lock()
        self._shut = False  # set by `shutdown`: never connect again

    def run(self, script, timeout):
        """Run a script in a subshell and wait for it.

        Parameters
        ----------
        script : str
            Shell script to run on the host.
        timeout : float
            Seconds to wait overall, including for an in-flight call to finish.

        Returns
        -------
        tuple
            ``(rc, stdout)``. ``rc`` is None when the shell couldn't run it:
            stdout is then `TIMEOUT` if it ran out of time, else empty
            (unreachable).
        """
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=max(timeout, 0)):
            return None, TIMEOUT
        try:
            warm = self._proc is not None
            rc, out = self._call(script, deadline)
            if rc is None and out != TIMEOUT and warm:
                # A shell that worked before has dropped (idle cut, network
                # change): reconnect once rather than report the host down.
                rc, out = self._call(script, deadline)
            return rc, out
        finally:
            self._lock.release()

    def shutdown(self):
        """Terminate the shell for good: a call still in flight can't reconnect."""
        self._shut = True
        self.close()

    def close(self):
        """Terminate the shell; the next `run` reconnects."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _call(self, script, deadline):
        if self._shut:
            return None, ""
        if self._proc is None:
            try:
                self._proc = subprocess.Popen(
                    self.argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    start_new_session=True,  # keep terminal signals off ssh
                )
            except OSError:
                return None, ""
        mark = f"__TUIMUX_{uuid.uuid4().hex}__"
        # The subshell keeps an `exit` in the script from ending this shell, and
        # </dev/null stops the script from eating our command stream.
        wrapped = (
            f"( {script}\n) </dev/null 2>/dev/null; printf '\\n%s %d\\n' {mark} $?\n"
        )
        try:
            self._proc.stdin.write(wrapped.encode())
        except OSError:
            self.close()
            return None, ""
        fd = self._proc.stdout.fileno()
        tail = f"\n{mark} ".encode()
        buf = b""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self.close()  # its state is unknown now; start fresh next time
                return None, TIMEOUT
            if not select.select([fd], [], [], left)[0]:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                self.close()
                return None, ""
            buf += chunk
            i = buf.find(tail)
            if i >= 0 and buf.endswith(b"\n"):
                try:
                    rc = int(buf[i + len(tail) :].strip())
                except ValueError:
                    rc = 1
                return rc, buf[:i].decode(errors="replace")


class ShellPool:
    """Lazily-started `HostShell` per (host, login).

    Parameters
    ----------
    argv_for : callable
        ``argv_for(host, login)`` returns the command that starts a shell.
    """

    def __init__(self, argv_for):
        self._argv_for = argv_for
        self._shells = {}
        self._lock = threading.Lock()
        self._closed = False

    def get(self, host, login):
        """Return the shell for ``(host, login)``, creating it if needed.

        After `close_all`, returns a shell that can't connect, so a probe still
        in flight at exit can't leave a new ssh behind.
        """
        with self._lock:
            if self._closed:
                return HostShell(["false"])
            sh = self._shells.get((host, login))
            if sh is None:
                sh = self._shells[(host, login)] = HostShell(
                    self._argv_for(host, login)
                )
            return sh

    def close_all(self):
        """Terminate every shell (on dashboard exit)."""
        with self._lock:
            self._closed = True
            shells, self._shells = list(self._shells.values()), {}
        for sh in shells:
            sh.shutdown()
