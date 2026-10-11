import json
from datetime import datetime, timedelta, timezone

import pytest

from lanectl.probe import Proc, WorktreeProbe
from lanectl.session import Session, SessionRef
from lanectl.signals import Thresholds, assess, brief_paths, on_brief
from lanectl.transcript import Transcript

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
PROJECT = "/repo"
WT = "/repo/.claude/worktrees/agent-a1"
BRIEF = "Implement card #1. Files: `core/ids.gd`, `test/foo_test.gd`. Before reading code run `git merge master`."


def ts(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


class Builder:
    """Writes a lead and one subagent transcript in Claude Code's on-disk layout."""

    def __init__(self, root):
        self.root = root
        self.sid = "sess-1"
        self.agent_dir = root / self.sid / "subagents"
        self.agent_dir.mkdir(parents=True)
        self.agent: list[dict] = []
        self.lead: list[dict] = []
        self.n = 0
        (self.agent_dir / "agent-a1.meta.json").write_text(
            json.dumps({"description": "#1 test card", "model": "opus", "worktreePath": WT, "worktreeBranch": "worktree-agent-a1"})
        )
        self.user(0, BRIEF)

    def _id(self):
        self.n += 1
        return f"toolu_{self.n}"

    def user(self, m, text, to=None):
        (to if to is not None else self.agent).append({"type": "user", "timestamp": ts(m), "cwd": WT, "message": {"role": "user", "content": text}})

    def call(self, m, name, inp, result=None, result_at=None, error=False, to=None):
        tid = self._id()
        rec = to if to is not None else self.agent
        rec.append({"type": "assistant", "timestamp": ts(m), "cwd": WT, "message": {"id": f"msg_{tid}", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}], "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000, "output_tokens": 5}}})
        if result is not None:
            rec.append({"type": "user", "timestamp": ts(result_at if result_at is not None else m + 0.01), "cwd": WT, "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": error}]}})

    def bash(self, m, cmd, done=True, to=None):
        self.call(m, "Bash", {"command": cmd}, "ok" if done else None, to=to)

    def edit(self, m, path):
        self.call(m, "Edit", {"file_path": path, "old_string": "a", "new_string": "b"}, "ok")

    def session(self) -> Session:
        (self.agent_dir / "agent-a1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.agent))
        (self.root / f"{self.sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.lead))
        s = Session(SessionRef(str(self.root), self.sid, 0, 1))
        s.refresh()
        return s

    def assess(self, at_min, probe=None, live=False):
        """Replay at `at_min`, or with `live` evaluate as live mode (probe used) with the clock pinned."""
        s = self.session()
        at = T0 + timedelta(minutes=at_min)
        if not live:
            return assess(s.agents["a1"], s, at)
        import lanectl.signals as sig

        orig, sig.now_utc = sig.now_utc, (lambda: at)
        try:
            return assess(s.agents["a1"], s, None, probe)
        finally:
            sig.now_utc = orig


@pytest.fixture
def b(tmp_path):
    return Builder(tmp_path)


def probe(procs=(), orphan=False):
    return WorktreeProbe(procs=[Proc(1, "05:00", c, orphan=orphan) for c in procs], branch="worktree-agent-a1")


# -- states -----------------------------------------------------------------


def test_working_when_recent_activity(b):
    b.bash(1, "git merge master")
    assert b.assess(2).state == "WORKING"


def test_instant_tool_left_pending_is_blocked(b):
    b.call(1, "Edit", {"file_path": WT + "/core/ids.gd"})
    st = b.assess(3)
    assert st.state == "BLOCKED" and "awaiting approval" in st.detail


def test_quick_shell_command_pending_in_replay_is_blocked(b):
    b.bash(1, "git status --short", done=False)
    assert b.assess(4).state == "BLOCKED"


def test_long_shell_command_pending_in_replay_is_ambiguous(b):
    b.bash(1, "tools/godot-test 600 --headless res://test/walk.tscn", done=False)
    assert b.assess(4).state == "RUNNING"
    assert b.assess(10).state == "PENDING"


def test_live_shell_with_process_is_running(b):
    b.bash(1, "tools/godot-test 600 --headless res://test/walk.tscn", done=False)
    assert b.assess(10, probe(["godot --headless"]), live=True).state == "RUNNING"


def test_live_shell_with_no_process_is_blocked(b):
    b.bash(1, "tools/godot-test 600 --headless res://test/walk.tscn", done=False)
    assert b.assess(10, probe([]), live=True).state == "BLOCKED"


def test_silent_with_nothing_running_is_idle(b):
    b.bash(1, "git merge master")
    st = b.assess(12, probe([]), live=True)
    assert st.state == "IDLE"


def test_silent_with_orphaned_process_is_orphaned(b):
    b.bash(1, "git merge master")
    st = b.assess(12, probe(["sh -c tools/godot-test 6000"], orphan=True), live=True)
    assert st.state == "ORPHANED"
    assert any(f.code == "ORPHAN" and f.severity == "bad" for f in st.flags)


def test_silent_waiting_on_background_work(b):
    b.bash(1, "git merge master")
    assert b.assess(12, probe(["godot"]), live=True).state == "WAITING"


def test_handback_is_reported(b):
    b.bash(1, "git merge master")
    b.call(2, "SubagentHandback", {"message": "#1 is done"}, "ok")
    st = b.assess(30)
    assert st.state == "REPORTED" and st.finished


def test_work_after_handback_is_not_reported(b):
    b.call(2, "SubagentHandback", {"message": "#1 is done"}, "ok")
    b.user(3, "The coordinator sent a message while you were working: fix the gate")
    b.bash(4, "git merge master")
    assert b.assess(4.5).state == "WORKING"


def test_task_stop_by_lead(b):
    b.bash(1, "git status", done=False)
    b.call(5, "TaskStop", {"task_id": "a1"}, "ok", to=b.lead)
    assert b.assess(6).state == "STOPPED"


def test_replay_hides_future(b):
    b.bash(1, "git merge master")
    b.call(20, "SubagentHandback", {"message": "done"}, "ok")
    assert b.assess(2).state == "WORKING"
    assert b.assess(21).state == "REPORTED"


# -- flags ------------------------------------------------------------------


def codes(st):
    return {f.code for f in st.flags}


def test_loop_same_call_without_edits(b):
    for m in (1, 2, 3):
        b.bash(m, "python3 tools/run_tests.py")
    assert "LOOP" in codes(b.assess(3.5))


def test_no_loop_when_edits_between(b):
    for m in (1, 2, 3):
        b.bash(m, "python3 tools/run_tests.py")
        b.edit(m + 0.5, WT + "/core/ids.gd")
    assert "LOOP" not in codes(b.assess(3.9))


def test_edit_outside_worktree(b):
    b.edit(1, "/repo/core/ids.gd")
    st = b.assess(2)
    assert "OUTSIDE" in codes(st)


def test_off_brief_files_are_listed(b):
    b.edit(1, WT + "/core/ids.gd")
    b.edit(2, WT + "/simulation/game_state.gd")
    st = b.assess(3)
    assert st.off_brief == ["simulation/game_state.gd"]


def test_stale_base_only_after_lead_merged(b):
    b.edit(2, WT + "/core/ids.gd")
    assert "STALE-BASE" not in codes(b.assess(3))


def test_stale_base_when_lead_merged_first(b):
    b.bash(1, "cd /repo; git merge --no-ff worktree-agent-a0 -m 'Merge #0'", to=b.lead)
    b.edit(2, WT + "/core/ids.gd")
    assert "STALE-BASE" in codes(b.assess(3))


def test_no_stale_base_when_agent_merged(b):
    b.bash(0.5, "cd /repo; git merge --no-ff worktree-agent-a0 -m 'Merge #0'", to=b.lead)
    b.bash(1, "git merge master")
    b.edit(2, WT + "/core/ids.gd")
    assert "STALE-BASE" not in codes(b.assess(3))


def test_uncommitted_edits_age(b):
    b.edit(1, WT + "/core/ids.gd")
    b.bash(50, "echo still going")
    assert "UNCOMMITTED" in codes(b.assess(51))
    b.bash(52, "git commit -am wip")
    assert "UNCOMMITTED" not in codes(b.assess(53))


def test_denial_reason_is_reported(b):
    b.call(1, "Bash", {"command": "sed -i x"}, "Permission for this action was denied by the Claude Code auto mode classifier. Reason: [Modify Shared Resources].", error=True)
    f = next(f for f in b.assess(2).flags if f.code == "DENIED")
    assert "Modify Shared Resources" in f.detail and f.severity == "warn"


def test_finished_agent_flags_are_downgraded(b):
    b.edit(1, WT + "/simulation/other.gd")
    b.call(2, "SubagentHandback", {"message": "done"}, "ok")
    st = b.assess(3)
    assert all(f.severity == "info" for f in st.flags)


# -- helpers ----------------------------------------------------------------


def test_brief_paths_and_matching():
    m = brief_paths("Edit `core/ids.gd` (~line 40), res://test/foo_test.gd, and the README.md section; see tools/board/")
    assert {"core/ids.gd", "test/foo_test.gd", "README.md", "tools/board/"} <= m
    assert on_brief("core/ids.gd", m)
    assert on_brief("tools/board/cli.mjs", m)
    assert not on_brief("core/other.gd", m)


def test_incremental_read_handles_partial_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    rec = {"type": "user", "timestamp": ts(0), "message": {"content": "brief"}}
    line = json.dumps(rec)
    p.write_text(line[:20])
    t = Transcript(str(p))
    t.refresh()
    assert t.events == []
    p.write_text(line + "\n")
    t.refresh()
    assert len(t.events) == 1 and t.brief == "brief"


def test_detached_launch_counts_as_background(b):
    b.bash(1, "nohup tools/godot-test 6000 --headless res://test/walk.tscn > /tmp/run.log 2>&1 &")
    b.bash(2, "make build && echo ok")
    f = next(f for f in b.assess(3).flags if f.code == "BG")
    assert "1 background" in f.detail and f.severity == "info"
