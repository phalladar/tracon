"""Which Claude Code session runs in the split next to tracon, asked of Ghostty (1.3+, macOS).

Ghostty's AppleScript dictionary lists windows, tabs and the terminals in each, with a terminal's
title and working directory. Claude Code titles its terminal `<spinner> <session name>`, so the
terminals sharing tracon's tab name the session to watch. Read-only: nothing is sent to Ghostty
but these queries.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

from .inbox import Live

SEP = "\x1f"  # between fields of one terminal in osascript's output

LIST_SCRIPT = f"""
set out to ""
tell application "Ghostty"
  repeat with w in windows
    repeat with t in tabs of w
      set tid to id of t
      repeat with s in terminals of t
        set out to out & tid & "{SEP}" & (id of s) & "{SEP}" & (name of s) & "{SEP}" & (working directory of s) & linefeed
      end repeat
    end repeat
  end repeat
end tell
return out
"""

FRONT_SCRIPT = """
tell application "Ghostty"
  if not frontmost then return ""
  return id of focused terminal of selected tab of front window
end tell
"""

# Claude Code's title starts with one status glyph (✳, ◐, ...) and a space.
GLYPH_RE = re.compile(r"^[^\x00-\x7f]\s+")


@dataclass
class Pane:
    tab: str
    id: str
    title: str
    cwd: str


def available() -> bool:
    return sys.platform == "darwin" and os.environ.get("TERM_PROGRAM") == "ghostty"


def _osascript(script: str, timeout: float = 3.0) -> str | None:
    try:
        r = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def panes() -> list[Pane] | None:
    """Every Ghostty terminal, or None if Ghostty couldn't be asked."""
    out = _osascript(LIST_SCRIPT)
    if out is None:
        return None
    found = []
    for line in out.splitlines():
        parts = line.split(SEP)
        if len(parts) == 4:
            found.append(Pane(*parts))
    return found


def find_self(write: Callable[[str], None] | None, timeout: float = 1.5) -> str | None:
    """The id of the Ghostty terminal tracon runs in. With `write`, briefly titles the terminal
    with a one-off marker and looks for it (the title stack is pushed and popped around it);
    without, takes the focused terminal if Ghostty is frontmost (it was just typed into)."""
    if write is None:
        return (_osascript(FRONT_SCRIPT) or "").strip() or None
    marker = f"tracon-{os.getpid()}-{secrets.token_hex(4)}"
    write(f"\x1b[22;2t\x1b]2;{marker}\x07")
    try:
        deadline = time.monotonic() + timeout
        while True:
            found = panes()
            if found is None:
                return None
            hit = next((p.id for p in found if p.title == marker), None)
            if hit or time.monotonic() > deadline:
                return hit
            time.sleep(0.1)
    finally:
        write("\x1b[23;2t")


def claude_title(pane_title: str) -> str | None:
    """The session name in a Claude Code terminal title, or None if the title isn't one."""
    if not GLYPH_RE.match(pane_title):
        return None
    return GLYPH_RE.sub("", pane_title).strip().rstrip("…").strip()


def same_title(pane: str, session: str) -> bool:
    if not pane or not session:
        return False
    return pane == session or (len(pane) >= 12 and session.startswith(pane))  # a long title may be cut short


def pick_neighbour(me: str, all_panes: list[Pane], live: list[Live], title_of: Callable[[Live], str],
                   ignore_prefix: str = "") -> str | None:
    """The session id of the Claude Code running in a split of tracon's own tab.

    A pane is matched to a live interactive session by title. A session too new to have a title is
    matched if it is the only untitled one in the pane's directory."""
    mine = next((p for p in all_panes if p.id == me), None)
    if mine is None:
        return None
    live = [lv for lv in live if lv.interactive]
    titles = {lv.session_id: title_of(lv) for lv in live}
    for pane in all_panes:
        t = claude_title(pane.title)
        if pane.id == me or pane.tab != mine.tab or t is None or (ignore_prefix and pane.title.startswith(ignore_prefix)):
            continue
        hits = [lv for lv in live if same_title(t, titles[lv.session_id])]
        if hits:
            here = [lv for lv in hits if lv.cwd == pane.cwd]
            return (here or hits)[0].session_id  # live is newest first
        untitled = [lv for lv in live if lv.cwd == pane.cwd and not titles[lv.session_id]]
        if len(untitled) == 1:
            return untitled[0].session_id
    return None
