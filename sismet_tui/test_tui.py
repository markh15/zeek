#!/usr/bin/env python3
"""Drive the curses TUI inside a pty against an in-process stand-in server.

This is the only coverage of the interface itself: that it starts, renders,
loads stations, opens the picker, probes the date parameters and writes a CSV
in response to real keystrokes.  It caught two crashes during development --
a curses write filling the bottom-right cell, and arrow keys arriving as
application-mode escape sequences.

    python3 -m unittest test_tui -v
"""

from __future__ import annotations

import csv
import os
import select
import struct
import subprocess
import sys
import tempfile
import time
import unittest

try:
    import fcntl
    import pty
    import termios

    HAS_PTY = True
except ImportError:  # pragma: no cover - Windows
    HAS_PTY = False

try:
    import pyte

    HAS_PYTE = True
except ImportError:  # pragma: no cover - optional test dependency
    HAS_PYTE = False

from test_integration import StandInSismet

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sismet_historic_tui.py")

# curses puts the terminal into application-cursor mode, so the arrows are
# ESC O A/B rather than ESC [ A/B.
KEYS = {
    "F2": "\x1bOQ", "F3": "\x1bOR", "F4": "\x1bOS", "F5": "\x1b[15~",
    "UP": "\x1bOA", "DOWN": "\x1bOB", "ENTER": "\r", "ESC": "\x1b", "SPACE": " ",
}

ROWS, COLS = 30, 110


class Screen:
    """A real terminal emulator, so assertions see what a user would see."""

    def __init__(self):
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.Stream(self.screen)

    def feed(self, text: str) -> None:
        self.stream.feed(text)

    def text(self) -> str:
        return "\n".join(line.rstrip() for line in self.screen.display)


class TuiSession:
    """A TUI running on the far end of a pty."""

    def __init__(self, args):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        self.proc = subprocess.Popen(
            [sys.executable, SCRIPT] + args,
            stdin=slave, stdout=slave, stderr=slave, close_fds=True,
            env=dict(os.environ, TERM="xterm"),
        )
        os.close(slave)
        self.display = Screen()

    def pump(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            self.display.feed(data.decode("utf-8", "replace"))

    def send(self, *keys, wait: float = 0.5) -> None:
        for key in keys:
            os.write(self.master, KEYS.get(key, key).encode())
            self.pump(wait)

    def wait_for(self, needle: str, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump(0.3)
            if needle in self.display.text():
                return True
        return False

    @property
    def screen(self) -> str:
        return self.display.text()

    def close(self) -> None:
        try:
            os.write(self.master, b"q")
            self.pump(0.5)
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        try:
            os.close(self.master)
        except OSError:
            pass


@unittest.skipUnless(HAS_PTY, "pty is not available on this platform")
@unittest.skipUnless(HAS_PYTE, "pip install pyte to run the TUI tests")
class TuiTests(unittest.TestCase):
    def setUp(self):
        self.server = StandInSismet()
        self.server.__enter__()
        self.dir = tempfile.mkdtemp()
        self.out = os.path.join(self.dir, "tui.csv")
        self.session = TuiSession([
            "--no-proxy", "--base-url", self.server.base_url, "-o", self.out,
            "--start", "2024-01-01", "--end", "2024-01-31", "--window-days", "10",
        ])
        self.addCleanup(self.server.__exit__, None, None, None)
        self.addCleanup(self.session.close)

    def test_it_starts_and_renders_the_form(self):
        self.assertTrue(self.session.wait_for("SISMET historic export"))
        screen = self.session.screen
        for label in ("Dataset", "Station(s)", "Start date", "Output CSV",
                      "SOCKS5 proxy", "Date params"):
            self.assertIn(label, screen)
        self.assertIn("F5 start", screen)

    def test_full_flow_from_keystrokes_to_csv(self):
        self.assertTrue(self.session.wait_for("SISMET historic export"))

        self.session.send("F2")                       # load the station list
        self.assertTrue(self.session.wait_for("stations loaded"))

        self.session.send("DOWN", "ENTER")            # open the picker
        self.assertTrue(self.session.wait_for("SPACE toggle"))
        self.assertIn("Guaxupe", self.session.screen)

        self.session.send("SPACE", "DOWN", "SPACE", "ENTER")   # take two, confirm
        self.assertTrue(self.session.wait_for("2 station(s) selected"))

        self.session.send("F3")                       # probe the date parameters
        self.assertTrue(self.session.wait_for("dtInicio/dtFim"))

        self.session.send("F5")                       # download
        self.assertTrue(self.session.wait_for("Done:", timeout=30))

        screen = self.session.screen
        self.assertIn("100.0%", screen)
        self.assertIn("8/8 windows", screen)          # 4 windows x 2 stations

        with open(self.out, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 62)               # 31 days x 2 stations
        self.assertEqual({r["Estacao"] for r in rows}, {"1", "7"})
        self.assertEqual(len({(r["Estacao"], r["Data"]) for r in rows}), 62)

    def test_download_refuses_to_start_without_date_parameters(self):
        self.assertTrue(self.session.wait_for("SISMET historic export"))
        self.session.send("F2")
        self.assertTrue(self.session.wait_for("stations loaded"))
        self.session.send("DOWN", "ENTER")
        self.assertTrue(self.session.wait_for("SPACE toggle"))
        self.session.send("SPACE", "ENTER")
        self.assertTrue(self.session.wait_for("1 station(s) selected"))

        self.session.send("F5")                       # no probe has run yet
        self.assertTrue(self.session.wait_for("Date parameters unknown"))
        self.assertFalse(os.path.exists(self.out))

    def test_it_reports_a_station_list_it_cannot_load(self):
        self.server.__exit__(None, None, None)        # server disappears
        self.assertTrue(self.session.wait_for("SISMET historic export"))
        self.session.send("F2")
        self.assertTrue(self.session.wait_for("enter codes by hand", timeout=20))


if __name__ == "__main__":
    unittest.main(verbosity=2)
