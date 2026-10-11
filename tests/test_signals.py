import json
from datetime import datetime, timedelta, timezone

import pytest

from tracon.probe import Proc, WorktreeProbe
from tracon.session import Session, SessionRef
from tracon.signals import Thresholds, assess, brief_paths, brief_wants_foreground, on_brief
from tracon.transcript import Transcript

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
        import tracon.signals as sig

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


def test_handback_is_done(b):
    b.bash(1, "git merge master")
    b.call(2, "SubagentHandback", {"message": "#1 is done"}, "ok")
    st = b.assess(30)
    assert st.state == "DONE" and st.finished


def test_work_after_handback_is_not_done(b):
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
    assert b.assess(21).state == "DONE"


# -- flags ------------------------------------------------------------------


def codes(st):
    return {f.code for f in st.flags}


def test_loop_same_call_without_edits(b):
    for m in (1, 2, 3):
        b.bash(m, "python3 tools/run_tests.py")
    assert "LOOP" in codes(b.assess(3.5))


def test_loop_clears_once_it_edits(b):
    for m in (1, 2, 3):
        b.bash(m, "python3 tools/run_tests.py")
    b.edit(4, WT + "/core/ids.gd")
    b.bash(5, "python3 tools/run_tests.py")
    assert "LOOP" not in codes(b.assess(5.5))


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


def test_no_stale_base_when_agent_reset_onto_master(b):
    b.bash(0.5, "cd /repo; git merge --no-ff worktree-agent-a0 -m 'Merge #0'", to=b.lead)
    b.bash(1, "git reset --hard master && git log --oneline -3")
    b.edit(2, WT + "/core/ids.gd")
    assert "STALE-BASE" not in codes(b.assess(3))


def test_file_checkout_from_master_is_not_catching_up(b):
    b.bash(0.5, "cd /repo; git merge --no-ff worktree-agent-a0 -m 'Merge #0'", to=b.lead)
    b.bash(1, "git checkout master -- core/ids.gd")
    b.edit(2, WT + "/core/ids.gd")
    assert "STALE-BASE" in codes(b.assess(3))


def test_probe_clears_stale_base_when_head_contains_master(b):
    # A reset to a SHA isn't visible as catching up in the transcript; git says it did.
    b.bash(0.5, "cd /repo; git merge --no-ff worktree-agent-a0 -m 'Merge #0'", to=b.lead)
    b.bash(1, "git reset --hard 6407ba26")
    b.edit(2, WT + "/core/ids.gd")
    caught_up, stale = probe(), probe()
    caught_up.behind, stale.behind = 0, 3
    assert "STALE-BASE" not in codes(b.assess(3, caught_up, live=True))
    f = next(f for f in b.assess(3, stale, live=True).flags if f.code == "STALE-BASE")
    assert "3 commits behind" in f.detail


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


GUARD = (f"This agent is isolated in the worktree {WT}, but this command is too complex to verify that it stays inside "
         f"the worktree. Refusing to run it — a worktree-isolated agent's git operations must target its own worktree. "
         f"Split it into plain, separate commands and run them from {WT}.")


def test_guard_refusals_the_agent_works_around_stay_quiet(b):
    for m in (1, 2, 3):  # #768: each refusal followed by the split commands it asked for
        b.call(m, "Bash", {"command": "for s in a b; do tools/godot-test --strategy=$s; done"}, GUARD, error=True)
        b.bash(m + 0.1, "tools/godot-test --strategy=a")
    s = b.session()
    refused = [e for e in s.agents["a1"].transcript.events if e.refused]
    assert len(refused) == 3 and not any(e.rejected for e in refused)
    assert refused[0].text.endswith(f"run them from {WT}.")  # the whole message, not the feed line
    f = next(f for f in b.assess(4).flags if f.code == "DENIED")
    assert f.severity == "info" and f.detail == "3 refused by a guard"


def test_guard_refusals_back_to_back_are_loud(b):
    b.call(1, "Bash", {"command": "cd x && git status"}, GUARD, error=True)
    b.call(2, "Bash", {"command": "(cd x; git status)"}, GUARD, error=True)
    f = next(f for f in b.assess(3).flags if f.code == "DENIED")
    assert f.severity == "warn"
    assert next(f for f in b.assess(40).flags if f.code == "DENIED").severity == "info"


def test_sleep_block_is_a_guard_refusal(b):
    b.call(1, "Bash", {"command": "sleep 240; cat log"}, "<tool_use_error>Blocked: sleep 240 followed by: cat log. To wait for a condition, use Monitor.</tool_use_error>", error=True)
    assert next(f for f in b.assess(2).flags if f.code == "DENIED").detail == "1 refused by a guard"


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


def test_brief_wants_foreground():
    assert brief_wants_foreground("Fix the loader. Run tests in the foreground.")
    assert brief_wants_foreground("Run tests in the foreground, never in the background.")
    assert brief_wants_foreground("Don't use run_in_background.\nRun the walk in the foreground.")
    assert not brief_wants_foreground("Fix the loader.")
    # #770: mentions the foreground limit, but allows nohup
    assert not brief_wants_foreground(
        "- It exceeds the 600 s foreground limit, so you may run it with `nohup` in the background.\n"
        "- Wait on it with one foreground `until` loop per call, at most 590 s each."
    )


def test_detached_process_of_a_working_agent_is_info(b):
    b.bash(1, "nohup tools/godot-test 6000 --headless res://test/walk.tscn > .scratch/walk.log 2>&1 &")
    b.bash(9, "until grep -q DONE .scratch/walk.log; do sleep 30; done", done=False)
    st = b.assess(10, probe(["sh -c tools/godot-test 6000"], orphan=True), live=True)
    assert st.state == "RUNNING"
    assert any(f.code == "ORPHAN" and f.severity == "info" for f in st.flags)


# -- agents that finish without a hand-back tool ------------------------------


def final_text(b, m, text, stop="end_turn"):
    b.agent.append({"type": "assistant", "timestamp": ts(m), "cwd": WT, "message": {"id": f"fin{m}", "stop_reason": stop, "model": "claude-opus-5-5", "content": [{"type": "text", "text": text}]}})


def task_note(b, m, status, aid="a1"):
    body = f"<task-notification>\n<task-id>{aid}</task-id>\n<status>{status}</status>\n<summary>Agent finished</summary>\n</task-notification>"
    b.lead.append({"type": "user", "timestamp": ts(m), "message": {"content": body}})


def test_final_answer_counts_as_done(b):
    b.bash(1, "grep -rn foo .")
    final_text(b, 2, "Here is what I found.")
    st = b.assess(20)
    assert st.state == "DONE" and "what I found" in st.detail


def test_text_mid_turn_is_not_final(b):
    b.bash(1, "grep -rn foo .")
    final_text(b, 2, "Looking further.", stop="tool_use")
    assert b.assess(20).state == "IDLE"


def test_final_answer_with_live_work_is_waiting(b):
    b.bash(1, "nohup make long &")
    final_text(b, 2, "Waiting for the build.")
    assert b.assess(20, probe(["make long"]), live=True).state == "WAITING"


def test_final_answer_after_background_launch_in_replay_is_waiting(b):
    b.bash(1, "nohup make long &")
    final_text(b, 2, "Waiting for the build.")
    assert b.assess(20).state == "WAITING"


def test_lead_task_note_completed_is_authoritative(b):
    b.bash(1, "make")
    final_text(b, 2, "All done.")
    task_note(b, 3, "completed")
    st = b.assess(20)
    assert st.state == "DONE" and st.detail == "All done."


def test_lead_task_note_failed_is_stopped(b):
    b.bash(1, "make")
    task_note(b, 2, "failed")
    assert b.assess(20).state == "STOPPED"


def test_resumed_after_task_note_is_active_again(b):
    final_text(b, 1, "Done.")
    task_note(b, 1.5, "completed")
    b.user(3, "The coordinator sent a message while you were working: one more fix")
    b.bash(3.5, "make test")
    assert b.assess(4).state == "WORKING"


def test_model_falls_back_to_transcript(b, tmp_path):
    final_text(b, 1, "x")
    s = b.session()
    a = s.agents["a1"]
    a.meta.pop("model")
    assert a.model == "opus"


def test_lane_falls_back_to_session_title(b):
    b.lead.append({"type": "ai-title", "aiTitle": "macos-port-proposal"})
    assert b.session().lane() == "macos-port-proposal"


def test_completed_note_after_background_launch_is_parked(b):
    b.call(1, "Bash", {"command": "make walk", "run_in_background": True}, "Command running in background with ID: bx1.")
    final_text(b, 2, "Waiting on the walk.")
    task_note(b, 3, "completed")
    st = b.assess(20)
    assert st.state == "WAITING" and "mid-task" in st.detail


def test_completed_note_after_background_work_reported_back_is_done(b):
    # A run_in_background shell that already notified the agent (as a queued command) isn't parked work.
    b.call(1, "Bash", {"command": "make long", "run_in_background": True}, "Command running in background with ID: bx1.")
    b.agent.append({"type": "attachment", "timestamp": ts(1.5), "attachment": {"type": "queued_command", "prompt": (
        "<task-notification>\n<task-id>bx1</task-id>\n<tool-use-id>toolu_1</tool-use-id>\n<status>completed</status>\n</task-notification>"
    )}})
    final_text(b, 2, "Build passes.")
    task_note(b, 3, "completed")
    assert b.assess(20).state == "DONE"


def test_completed_note_after_detached_launch_is_done(b):
    # A detached shell never notifies the agent, so the agent can't be parked waiting on it.
    b.bash(1, "(./app > log 2>&1 &); sleep 2; pkill -f app")
    final_text(b, 2, "Checked the app.")
    task_note(b, 3, "completed")
    assert b.assess(20).state == "DONE"
    assert b.assess(20, probe(), live=True).state == "DONE"


def test_fork_does_not_inherit_the_leads_spawn_call(b):
    # A fork's transcript opens with the lead's message that launched it in the background.
    spawn = {"type": "assistant", "timestamp": ts(-1), "cwd": WT, "message": {"id": "lead1", "content": [
        {"type": "tool_use", "id": "toolu_lead", "name": "Agent", "input": {"description": "#1 test card", "run_in_background": True}}]}}
    b.agent[:0] = [{"type": "fork-context-ref", "agentId": "a1"}, spawn]
    b.bash(1, "make")
    final_text(b, 2, "All done.")
    task_note(b, 3, "completed")
    s = b.session()
    assert not any(e.tool == "Agent" for e in s.agents["a1"].transcript.events)
    assert b.assess(20).state == "DONE"


def test_session_title_prefers_rename_then_generated_title(b):
    b.lead.append({"type": "ai-title", "aiTitle": "generated-title"})
    assert b.session().title() == "generated-title"
    b.lead.append({"type": "agent-name", "agentName": "my-renamed-session"})
    assert b.session().title() == "my-renamed-session"


def test_coordination_calls_read_as_text_not_json():
    from tracon.transcript import _tool_use_event

    ev = lambda name, inp: _tool_use_event(T0, {"name": name, "input": inp}, "")  # noqa: E731
    assert ev("Agent", {"description": "#12 Payment webhooks", "prompt": "Card #12…"}).summary == "#12 Payment webhooks"
    assert ev("SendMessage", {"to": "a1", "message": "Mock the client."}).summary == "→ a1: Mock the client."
    assert ev("TaskStop", {"task_id": "a1"}).summary == "stop a1"
