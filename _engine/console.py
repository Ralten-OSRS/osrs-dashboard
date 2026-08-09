#!/usr/bin/env python3
"""One place all console output passes through.

Why this exists. A windowed PyInstaller build leaves `sys.stdout` as `None` on
Windows, so every unguarded `print()` raises AttributeError and the app appears
to do nothing at all. The engine and launcher contain over a hundred print
calls between them. Rewriting each one would be a large mechanical change with
a hundred chances to miss a line, and a missed line is an invisible crash.

Instead we replace the stream itself, once, at startup. Every existing print
keeps working untouched, and the replacement does three useful things:

  1. Writes through to the real console when there is one.
  2. Appends to a log file, so a packaged run with no console still leaves
     evidence behind for a bug report.
  3. Keeps the recent lines in memory, so the local service can show build
     progress in the browser.

Encoding is handled deliberately. A Unicode arrow in scraper output once halted
a release build outright, because the Windows console code page could not
represent it. Anything the console cannot encode is degraded rather than
allowed to raise: an unreadable character is a cosmetic problem, a traceback in
its place is not.
"""

import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

# Keep the log from growing without bound across many runs. A run is a few KB,
# so this holds a long history while staying trivially openable in Notepad.
MAX_LOG_BYTES = 1_000_000
RECENT_LINES = 400


class TeeStream:
    """A stdout replacement that survives having no console behind it."""

    def __init__(self, original, log_path=None, buffer_size=RECENT_LINES):
        self._original = original
        self._log = None
        self._lock = threading.Lock()
        self._lines = deque(maxlen=buffer_size)
        self._partial = ""
        self._sequence = 0
        if log_path:
            self._open_log(Path(log_path))

    def _open_log(self, path):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                path.unlink()
            self._log = path.open("a", encoding="utf-8", errors="replace")
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._log.write(f"\n===== run started {stamp} =====\n")
            # The log path is only known once the account is chosen, so the
            # launcher opens it partway through a run. Replay what has already
            # been printed, or the log would be missing the update check and
            # the character resolution — the part of a run most likely to
            # explain why someone is filing a bug.
            for _sequence, line in list(self._lines):
                self._log.write(line + "\n")
            if self._partial:
                self._log.write(self._partial)
            self._log.flush()
        except OSError:
            # A log we cannot open is not worth failing a launch over.
            self._log = None

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        with self._lock:
            self._buffer_lines(text)
            if self._original is not None:
                try:
                    self._original.write(text)
                except UnicodeEncodeError:
                    # The console code page cannot represent something here.
                    # Degrade the characters rather than raise: this exact
                    # failure once stopped a release build mid-run.
                    encoding = getattr(self._original, "encoding", None) or "ascii"
                    safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
                    try:
                        self._original.write(safe)
                    except Exception:  # noqa: BLE001 - console is optional
                        pass
                except Exception:  # noqa: BLE001 - a dead console must not crash a build
                    self._original = None
            if self._log is not None:
                try:
                    self._log.write(text)
                    # Flush every write. The whole point of this log is to
                    # survive a crash, and buffered output is discarded on an
                    # abnormal exit — the log would be empty in precisely the
                    # case it exists for. A few hundred small writes per run
                    # is not worth optimising against that.
                    self._log.flush()
                except Exception:  # noqa: BLE001 - logging is best effort
                    pass
        return len(text)

    def _buffer_lines(self, text):
        """Accumulate whole lines for the in-browser progress view."""
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._sequence += 1
            self._lines.append((self._sequence, line.rstrip("\r")))

    def flush(self):
        with self._lock:
            for stream in (self._original, self._log):
                if stream is None:
                    continue
                try:
                    stream.flush()
                except Exception:  # noqa: BLE001
                    pass

    def recent(self, after=0, limit=RECENT_LINES):
        """Lines logged since a given sequence number, oldest first."""
        with self._lock:
            pending = [entry for entry in self._lines if entry[0] > after]
            partial = self._partial
            latest = self._sequence
        trimmed = pending[-limit:]
        return {
            "lines": [{"n": n, "text": text} for n, text in trimmed],
            "latest": latest,
            "partial": partial,
        }

    # SimpleHTTPRequestHandler and friends occasionally probe these.
    def isatty(self):
        try:
            return bool(self._original is not None and self._original.isatty())
        except Exception:  # noqa: BLE001
            return False

    @property
    def encoding(self):
        return getattr(self._original, "encoding", None) or "utf-8"

    def writable(self):
        return True

    def close(self):
        with self._lock:
            if self._log is not None:
                try:
                    self._log.close()
                except Exception:  # noqa: BLE001
                    pass
                self._log = None


_stream = None


def install(log_path=None):
    """Replace stdout and stderr with one tee. Safe to call more than once."""
    global _stream
    if _stream is None:
        _stream = TeeStream(sys.__stdout__, log_path=log_path)
    elif log_path and _stream._log is None:
        # The log lives beside the account data, so its path is only known
        # after the character is chosen. Attach it once we learn it.
        _stream._open_log(Path(log_path))
    sys.stdout = _stream
    sys.stderr = _stream
    return _stream


def stream():
    return _stream


def recent(after=0, limit=RECENT_LINES):
    if _stream is None:
        return {"lines": [], "latest": 0, "partial": ""}
    return _stream.recent(after=after, limit=limit)


def can_prompt():
    """Whether an interactive input() is possible in this process."""
    return sys.stdin is not None and getattr(sys.stdin, "readable", lambda: False)()


def alert(title, message):
    """Last-resort way to tell the user something when there is no UI left.

    Everything normally reports through the browser. This exists for the one
    failure the browser cannot cover: the local service not starting, so there
    is no page to render an error on. Without it, a windowed build in that
    state simply does nothing when double-clicked, which is unreportable.

    Falls back to printing when a message box is unavailable, so this is safe
    to call on any platform and in any build.
    """
    printed = False
    if sys.platform == "win32":
        try:
            import ctypes

            # MB_OK | MB_ICONERROR | MB_SETFOREGROUND
            ctypes.windll.user32.MessageBoxW(None, str(message), str(title), 0x10 | 0x10000)
            printed = True
        except Exception:  # noqa: BLE001 - a failed alert must not mask the real error
            printed = False
    if not printed:
        try:
            print(f"\n{title}\n{message}\n")
        except Exception:  # noqa: BLE001
            pass
