"""Send a message into a live Claude Code session through its cross-session inbox.

Each running Claude Code process registers itself in `~/.claude/sessions/<pid>.json` (read here,
never written) and listens on a Unix socket named there. A message is one JSON line; the session
shows it as coming from another session, between tool calls or as a new turn when idle. The
auth token in the `.key` files is optional on macOS and Linux, so this never opens them.
**The only module that knows the inbox format.**
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path

SESSIONS_DIR = str(Path.home() / ".claude" / "sessions")


@dataclass
class Inbox:
    pid: int
    socket: str
    name: str = ""
    status: str = ""  # idle | busy | ...


def find_inbox(session_id: str, root: str = SESSIONS_DIR) -> Inbox | None:
    """The live process holding `session_id` open, or None if it isn't running."""
    best: tuple[int, Inbox] | None = None
    for f in Path(root).glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        sock = d.get("messagingSocketPath") or ""
        if d.get("sessionId") != session_id or not sock or not os.path.exists(sock) or not _alive(d.get("pid")):
            continue
        ib = Inbox(int(d["pid"]), sock, d.get("name") or "", d.get("status") or "")
        updated = int(d.get("updatedAt") or 0)
        if best is None or updated > best[0]:
            best = (updated, ib)
    return best[1] if best else None


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def send(sock_path: str, text: str, timeout: float = 3.0) -> None:
    """Deliver `text` as one message. Raises OSError if the socket can't be reached."""
    line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall(line.encode())
