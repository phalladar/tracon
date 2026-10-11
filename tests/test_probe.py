import os
import subprocess
import sys

from tracon.probe import Prober, _owner

SNAPSHOT = "/bin/zsh -c source /Users/u/.claude/shell-snapshots/snapshot-zsh-1.sh && eval 'pytest'"


def test_owner_walks_up_to_claude_a_terminal_or_init():
    table = {
        10: (1, "1:00:00", "/sbin/launchd"),
        20: (10, "50:00", "claude"),
        21: (20, "01:00", SNAPSHOT),
        22: (21, "01:00", "python -m pytest"),
        30: (10, "2:00:00", "/Applications/Ghostty.app/Contents/MacOS/ghostty"),
        31: (30, "1:00:00", "-zsh"),
        32: (31, "05:00", "python -m http.server"),
        40: (1, "30:00", "node dev-server.js"),  # its shell is gone
        50: (1, "1:00:00", "/usr/lib/systemd/systemd --user"),
        51: (50, "1:00:00", "/usr/bin/gnome-terminal-server"),
        52: (51, "1:00:00", "bash"),
        53: (52, "05:00", "vim"),
        54: (50, "30:00", "npm run dev"),  # reparented to the user's systemd subreaper
    }
    assert _owner(22, table) == "agent"
    assert _owner(21, table) == "agent"
    assert _owner(32, table) == "user"
    assert _owner(40, table) == "orphan"
    assert _owner(53, table) == "user"
    assert _owner(54, table) == "orphan"


def test_the_live_probe_sees_a_child_process_and_its_cwd(tmp_path):
    """Exercises the real lsof (macOS) or /proc (Linux) and ps calls."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path)
    try:
        cwds = Prober._process_cwds()
        table = Prober._process_table()
        assert os.path.realpath(cwds[child.pid]) == os.path.realpath(tmp_path)
        ppid, etime, cmd = table[child.pid]
        assert ppid == os.getpid()
        assert ":" in etime
        assert "time.sleep(30)" in cmd
    finally:
        child.kill()
        child.wait()
