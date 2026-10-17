"""Send a message into a live Claude Code session through its cross-session inbox.

Each running Claude Code process registers itself in `~/.claude/sessions/<pid>.json` (read here,
never written) and listens on a Unix socket named there (a named pipe on Windows). A message is
one JSON line; the session shows it as coming from another session, between tool calls or as a new
turn when idle. The auth token in the `.key` files is optional on macOS and Linux, so this never
opens them.
**The only module that knows the inbox format.**
"""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

SESSIONS_DIR = str(Path.home() / ".claude" / "sessions")


@dataclass
class Inbox:
    pid: int
    socket: str
    name: str = ""
    status: str = ""  # idle | busy | ...


@dataclass
class Live:
    """A running Claude Code process, as it registers itself."""

    pid: int
    session_id: str
    cwd: str
    interactive: bool  # in a terminal, as opposed to a background session
    socket: str = ""
    name: str = ""
    status: str = ""
    updated: int = 0


def live_sessions(root: str = SESSIONS_DIR) -> list[Live]:
    """Claude Code processes that are still running, most recently updated first."""
    out = []
    for f in Path(root).glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or not d.get("sessionId") or not _alive(d.get("pid")):
            continue
        out.append(Live(
            int(d["pid"]), d["sessionId"], d.get("cwd") or "", d.get("kind", "interactive") == "interactive",
            d.get("messagingSocketPath") or "", d.get("name") or "", d.get("status") or "", int(d.get("updatedAt") or 0),
        ))
    return sorted(out, key=lambda lv: lv.updated, reverse=True)


def find_inbox(session_id: str, root: str = SESSIONS_DIR) -> Inbox | None:
    """The live process holding `session_id` open, or None if it isn't running."""
    for lv in live_sessions(root):
        if lv.session_id == session_id and lv.socket and (_is_pipe(lv.socket) or os.path.exists(lv.socket)):
            return Inbox(lv.pid, lv.socket, lv.name, lv.status)
    return None


def _is_pipe(path: str) -> bool:
    """A Windows named pipe. Not stat'ed: that opens a connection; `send` reports one that's gone."""
    return path.startswith("\\\\.\\pipe\\")


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    if sys.platform == "win32":  # os.kill(pid, 0) there sends CTRL_C_EVENT instead of probing
        return _alive_win(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _alive_win(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: running, as another user
    try:
        code = wintypes.DWORD()
        return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def send(sock_path: str, text: str, timeout: float = 3.0) -> None:
    """Deliver `text` as one message. Raises OSError if the socket can't be reached."""
    line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
    if _is_pipe(sock_path):  # r+b opens the existing pipe; w would ask to create it
        with open(sock_path, "r+b", buffering=0) as f:
            f.write(line.encode())
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall(line.encode())
