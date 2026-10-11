"""Live checks on worktrees: processes Claude launched there, and git state."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field

SHELLS = {"zsh", "bash", "sh", "fish", "dash", "-zsh", "-bash", "login"}


@dataclass
class Proc:
    pid: int
    etime: str
    command: str
    orphan: bool = False  # parent died; nothing will collect its result


@dataclass
class WorktreeProbe:
    procs: list[Proc] = field(default_factory=list)
    branch: str = ""
    ahead: int | None = None  # commits ahead of the base branch
    behind: int | None = None  # base-branch commits HEAD doesn't contain
    dirty: int = 0  # changed paths in `git status --porcelain`
    head: str = ""
    exists: bool = True
    last_change: float = 0.0  # epoch seconds the HEAD/dirty fingerprint last moved
    probed_at: float = 0.0

    @property
    def live(self) -> bool:
        return bool(self.procs)

    def leaf_command(self) -> str:
        """The most specific thing running: prefer a non-shell process."""
        for p in reversed(self.procs):
            if _comm(p.command) not in SHELLS:
                return p.command
        return self.procs[-1].command if self.procs else ""


def _run(args: list[str], timeout: float = 5.0) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _comm(command: str) -> str:
    return os.path.basename(command.split(" ", 1)[0]) if command else ""


class Prober:
    """Probe many worktrees at once; one lsof and one ps call per round."""

    def __init__(self) -> None:
        self._fingerprints: dict[str, tuple[str, str, float]] = {}
        self._base: dict[str, str] = {}

    def probe(self, worktrees: list[str], git: bool = True) -> dict[str, WorktreeProbe]:
        cwds = self._process_cwds()
        table = self._process_table()
        out: dict[str, WorktreeProbe] = {}
        now = time.time()
        for wt in worktrees:
            pr = WorktreeProbe(probed_at=now)
            if not os.path.isdir(wt):
                pr.exists = False
                out[wt] = pr
                continue
            prefix = wt.rstrip("/") + "/"
            pids = sorted(pid for pid, cwd in cwds.items() if cwd == wt or cwd.startswith(prefix))
            for pid in pids:
                owner = _owner(pid, table)
                if owner in ("agent", "orphan"):
                    _, etime, cmd = table[pid]
                    pr.procs.append(Proc(pid, etime, cmd, orphan=owner == "orphan"))
            if git:
                self._git(wt, pr, now)
            out[wt] = pr
        return out

    def _git(self, wt: str, pr: WorktreeProbe, now: float) -> None:
        pr.branch = _run(["git", "-C", wt, "branch", "--show-current"]).strip()
        pr.head = _run(["git", "-C", wt, "rev-parse", "--short", "HEAD"]).strip()
        status = _run(["git", "-C", wt, "status", "--porcelain"])
        pr.dirty = len([line for line in status.splitlines() if line.strip()])
        base = self._base_branch(wt)
        if base:
            n = _run(["git", "-C", wt, "rev-list", "--count", f"{base}..HEAD"]).strip()
            pr.ahead = int(n) if n.isdigit() else None
            n = _run(["git", "-C", wt, "rev-list", "--count", f"HEAD..{base}"]).strip()
            pr.behind = int(n) if n.isdigit() else None
        fp = (pr.head, status)
        prev = self._fingerprints.get(wt)
        if prev is None or prev[:2] != fp:
            self._fingerprints[wt] = (*fp, now)
            pr.last_change = now
        else:
            pr.last_change = prev[2]

    def _base_branch(self, wt: str) -> str:
        if wt not in self._base:
            self._base[wt] = ""
            for b in ("master", "main"):
                if _run(["git", "-C", wt, "rev-parse", "--verify", "-q", b]).strip():
                    self._base[wt] = b
                    break
        return self._base[wt]

    @staticmethod
    def _process_cwds() -> dict[int, str]:
        out = _run(["lsof", "-n", "-P", "-w", "-d", "cwd", "-F", "pn"], timeout=10)
        cwds: dict[int, str] = {}
        pid = None
        for line in out.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("n") and pid is not None:
                cwds[pid] = line[1:]
        return cwds

    @staticmethod
    def _process_table() -> dict[int, tuple[int, str, str]]:
        out = _run(["ps", "-axo", "pid=,ppid=,etime=,command="])
        table = {}
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[0].isdigit():
                table[int(parts[0])] = (int(parts[1]), parts[2], parts[3])
        return table


def _is_claude(cmd: str) -> bool:
    return _comm(cmd) == "claude" or "/.claude/shell-snapshots/" in cmd or "ClaudeCode.app" in cmd


def _owner(pid: int, table: dict) -> str:
    """'agent' if Claude launched it, 'user' if it hangs off a login or terminal, 'orphan' if
    its parents are gone (reparented to launchd), '' if unknown."""
    seen = set()
    cur = pid
    while cur in table and cur not in seen:
        seen.add(cur)
        ppid, _, cmd = table[cur]
        if cur != pid and _is_claude(cmd):
            return "agent"
        if cur == pid and "/.claude/shell-snapshots/" in cmd:
            return "agent"
        if _comm(cmd) in ("login", "ghostty", "Ghostty", "tmux", "sshd", "Terminal", "iTerm2") or cmd.startswith("-"):
            return "user"
        if ppid == 1:
            return "orphan"
        cur = ppid
    return ""
