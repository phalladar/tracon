import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from tracon import judge as judge_mod
from tracon.judge import Judge, Judgment, build_prompt
from tracon.lane import assess_all, card_label, file_matrix
from tracon.session import Session, SessionRef
from tracon.web import LaneState
from tracon.signals import Thresholds

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
ROOT = "/repo"


def ts(m):
    return (T0 + timedelta(minutes=m)).isoformat().replace("+00:00", "Z")


class Lane:
    """A lead plus any number of worktree agents, written in Claude Code's on-disk layout."""

    def __init__(self, tmp):
        self.tmp, self.sid, self.n = tmp, "sess", 0
        self.sub = tmp / self.sid / "subagents"
        self.sub.mkdir(parents=True)
        self.recs: dict[str, list] = {}
        self.lead: list = []

    def agent(self, aid, desc, start=0, brief="Do the card."):
        wt = f"{ROOT}/.claude/worktrees/agent-{aid}"
        (self.sub / f"agent-{aid}.meta.json").write_text(json.dumps({"description": desc, "model": "opus", "worktreePath": wt, "worktreeBranch": f"worktree-agent-{aid}"}))
        self.recs[aid] = [{"type": "user", "timestamp": ts(start), "cwd": wt, "message": {"content": brief}}]
        return wt

    def call(self, aid, m, name, inp, ok=True, out="ok"):
        self.n += 1
        tid = f"t{self.n}"
        cwd = self.recs[aid][0]["cwd"]
        self.recs[aid].append({"type": "assistant", "timestamp": ts(m), "cwd": cwd, "message": {"id": tid, "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        self.recs[aid].append({"type": "user", "timestamp": ts(m + 0.01), "cwd": cwd, "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": not ok}]}})

    def edit(self, aid, m, rel):
        wt = self.recs[aid][0]["cwd"]
        self.call(aid, m, "Edit", {"file_path": f"{wt}/{rel}", "old_string": "a", "new_string": "b"})

    def session(self):
        for aid, recs in self.recs.items():
            (self.sub / f"agent-{aid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
        (self.tmp / f"{self.sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.lead))
        s = Session(SessionRef(str(self.tmp), self.sid, 0, len(self.recs)))
        s.refresh()
        return s


@pytest.fixture
def lane(tmp_path):
    return Lane(tmp_path)


def at(m):
    return T0 + timedelta(minutes=m)


# -- collisions -------------------------------------------------------------


def test_concurrent_edits_of_one_file_collide(lane):
    lane.agent("a", "#1 spine", 0)
    lane.agent("b", "#2 content", 1)
    lane.edit("a", 2, "core/ids.gd")
    lane.edit("b", 3, "core/ids.gd")
    lane.edit("b", 4, "core/other.gd")
    m = file_matrix(lane.session(), at(5))
    assert m.files[0] == "core/ids.gd"
    assert m.editors("core/ids.gd") == ["a", "b"]
    assert [f for f, _ in m.collisions["a"]] == ["core/ids.gd"]
    statuses, _ = assess_all(lane.session(), at(5))
    assert any(f.code == "COLLIDE" and f.severity == "warn" for f in statuses["a"].flags)


def test_sequential_edits_do_not_collide(lane):
    lane.agent("a", "#1 first", 0)
    lane.edit("a", 1, "core/ids.gd")
    lane.call("a", 2, "SubagentHandback", {"message": "done"})
    lane.agent("b", "#2 later", 10)
    lane.edit("b", 11, "core/ids.gd")
    m = file_matrix(lane.session(), at(12))
    assert m.collisions == {}


def test_reads_are_recorded_but_never_collide(lane):
    lane.agent("a", "#1", 0)
    lane.agent("b", "#2", 0)
    lane.edit("a", 1, "core/ids.gd")
    lane.call("b", 1, "Read", {"file_path": f"{ROOT}/.claude/worktrees/agent-b/core/ids.gd"})
    m = file_matrix(lane.session(), at(2))
    assert m.cells[("core/ids.gd", "b")].reads == 1
    assert m.collisions == {}


def test_card_label():
    from tracon.session import Agent
    from tracon.transcript import Transcript

    assert card_label(Agent("x", {"description": "#749 Act III"}, Transcript("/nope"))) == "#749"
    assert card_label(Agent("x", {"description": "Fact sheet"}, Transcript("/nope"))) == "Fact"


# -- judge ------------------------------------------------------------------


def test_judge_due_needs_new_calls_and_time(lane, tmp_path):
    lane.agent("a", "#1", 0)
    for m in range(6):
        lane.call("a", m, "Bash", {"command": f"echo {m}"})
    s = lane.session()
    j = Judge(backend="cli", cache_dir=str(tmp_path / "cache"), min_new_calls=5, min_interval_s=180)
    agent = s.agents["a"]
    assert j.due(agent, finished=False)
    assert not j.due(agent, finished=True)
    j.history["a"] = [Judgment("on_track", "x", "", ts(5), 6, made=__import__("time").time(), prompt_v=judge_mod.PROMPT_VERSION)]
    assert not j.due(agent, finished=False)  # nothing new since
    j.spent = 5.0
    j.budget_usd = 1.0
    j.history = {}
    assert not j.due(agent, finished=False)  # over budget



def test_judge_rechecks_drift_early_once_the_lead_answers(lane, tmp_path):
    import time

    lane.agent("a", "#1", 0)
    for m in range(6):
        lane.call("a", m, "Bash", {"command": f"echo {m}"})
    j = Judge(backend="cli", cache_dir=str(tmp_path / "cache"), min_new_calls=5, min_interval_s=180)
    j.history["a"] = [Judgment("drifting", "x", "Restyling admin.", ts(5), 6, made=time.time() - 60, prompt_v=judge_mod.PROMPT_VERSION)]
    s = lane.session()
    assert not j.due(s.agents["a"], False, s)  # no answer from the lead yet
    now = datetime.now(timezone.utc)
    stamp = now.isoformat().replace("+00:00", "Z")
    send = {"type": "tool_use", "id": "m1", "name": "SendMessage", "input": {"to": "a", "message": "Admin restyle is approved, carry on."}}
    lane.lead.append({"type": "assistant", "timestamp": stamp, "cwd": ROOT, "message": {"id": "m1", "content": [send]}})
    s = lane.session()
    assert not j.due(s.agents["a"], False, s)  # wait until the agent has acted on it
    lane.call("a", 7, "Bash", {"command": "echo 7"})
    s = lane.session()
    assert j.due(s.agents["a"], False, s)
    assert not j.due(s.agents["a"], False)  # needs the session to see the lead
    j.history["a"][0] = Judgment("on_track", "x", "", ts(5), 6, made=time.time() - 60, prompt_v=judge_mod.PROMPT_VERSION)
    assert not j.due(s.agents["a"], False, s)  # only drifting/stuck get the early check
    j.history["a"][0] = Judgment("drifting", "x", "Restyling admin.", ts(7), 7, made=time.time(), prompt_v=judge_mod.PROMPT_VERSION)
    assert not j.due(s.agents["a"], False, s)  # already judged since the message



def test_judge_rechecks_a_verdict_from_an_older_prompt_once(lane, tmp_path):
    import time

    lane.agent("a", "#1", 0)
    for m in range(6):
        lane.call("a", m, "Bash", {"command": f"echo {m}"})
    s = lane.session()
    j = Judge(backend="cli", cache_dir=str(tmp_path / "cache"))
    j.history["a"] = [Judgment("drifting", "x", "Old concern.", ts(5), 6, made=time.time(), prompt_v="0ldpr0mt")]
    assert j.due(s.agents["a"], False, s)  # nothing new, but the prompt has changed since
    assert not j.due(s.agents["a"], True, s)  # finished agents still never
    j.history["a"].append(Judgment("on_track", "x", "", ts(5), 6, made=time.time(), prompt_v=judge_mod.PROMPT_VERSION))
    assert not j.due(s.agents["a"], False, s)


def test_dismissed_drift_drops_the_flag_and_reaches_the_judge(lane, tmp_path, monkeypatch):
    lane.agent("a", "#768 night plan", 0)
    for m in range(6):
        lane.call("a", m, "Bash", {"command": f"echo {m}"})
    s = lane.session()
    cache = str(tmp_path / "cache")
    j = Judge(backend="cli", cache_dir=cache)
    concern = "Reworded refused commands instead of reporting them."
    j.history["a"] = [Judgment("drifting", "running walks", concern, at(5).isoformat(), 6, made=1000.0, prompt_v=judge_mod.PROMPT_VERSION)]
    st = assess_all(s, at(6), judge=j)[0]["a"]
    assert "DRIFT" in [f.code for f in st.flags] and not st.dismissed

    assert j.toggle_dismiss("a", st.judgment) is True
    st = assess_all(s, at(6), judge=j)[0]["a"]
    assert "DRIFT" not in [f.code for f in st.flags] and st.dismissed
    j2 = Judge(backend="cli", cache_dir=cache)  # survives a restart, and another process sees it
    assert j2.is_dismissed("a", j2.latest("a"))

    seen = {}

    def fake_run(args, input, **kw):
        seen["input"] = input
        out = {"is_error": False, "total_cost_usd": 0.001, "structured_output": {"verdict": "drifting", "doing": "Editing admin", "concern": "Now restyling admin pages."}}
        return subprocess.CompletedProcess(args, 0, json.dumps(out), "")

    monkeypatch.setattr(judge_mod.subprocess, "run", fake_run)
    j.run(s.agents["a"], s)
    assert "Concerns the human has reviewed and cleared:\n- " in seen["input"]
    assert f"drifting: {concern}" in seen["input"]
    st = assess_all(s, at(6), judge=j)[0]["a"]  # a new concern flags again
    assert "Now restyling admin pages." in [f.detail for f in st.flags if f.code == "DRIFT"]

    first = j.history["a"][0]
    assert j.toggle_dismiss("a", first) is False  # pressing it again restores
    assert j.cleared("a") == []


def refusal(wt):
    """Claude Code's worktree guard, as #768 met it three times in a row."""
    return (f"This agent is isolated in the worktree {wt}, but this command runs godot-test with a value computed at "
            "runtime (the variable S) inside a construct too complex to verify, so what it runs cannot be shown not to be "
            "git. Refusing to run it — a worktree-isolated agent's git operations must target its own worktree. "
            f"Split it into plain, separate commands and run them from {wt}.")


def test_judge_sees_the_whole_refusal_with_the_worktree_shortened(lane):
    wt = lane.agent("a", "#768 night plan", 0, brief="If a command is refused once, report the exact refusal text; never reword it repeatedly.")
    lane.call("a", 1, "Bash", {"command": "S=$PWD/.scratch; nohup tools/godot-test 12000 > $S/direct.log &"}, ok=False, out=refusal(wt))
    lane.call("a", 1.1, "Bash", {"command": "nohup tools/godot-test 12000 > .scratch/direct.log &"})
    prompt, _, _ = build_prompt(lane.session().agents["a"], lane.session())
    assert "refused: This agent is isolated in the worktree <worktree>, but" in prompt
    assert "Split it into plain, separate commands and run them from <worktree>." in prompt
    assert wt not in prompt.split("Last ")[-1]
    assert "not rewording around it" in judge_mod.SYSTEM and "until the agent hands back" in judge_mod.SYSTEM


def test_judge_cli_backend_parses_structured_output(lane, tmp_path, monkeypatch):
    lane.agent("a", "#1 fix loader", 0, brief="Fix `core/save.gd`. Run tests in the foreground.")
    lane.edit("a", 1, "core/save.gd")
    s = lane.session()
    seen = {}

    def fake_run(args, input, **kw):
        seen["args"], seen["input"] = args, input
        out = {"is_error": False, "total_cost_usd": 0.0012, "structured_output": {"verdict": "drifting", "doing": "Editing save.gd", "concern": "Runs tests detached."}}
        return subprocess.CompletedProcess(args, 0, json.dumps(out), "")

    monkeypatch.setattr(judge_mod.subprocess, "run", fake_run)
    j = Judge(backend="cli", cache_dir=str(tmp_path / "cache"))
    r = j.run(s.agents["a"], s, "WORKING: Edit")
    assert (r.verdict, r.doing, r.cost) == ("drifting", "Editing save.gd", 0.0012)
    assert "--json-schema" in seen["args"] and "Fix `core/save.gd`" in seen["input"]
    assert j.spent == pytest.approx(0.0012)
    # cached on disk and picked up by a fresh instance
    assert Judge(backend="cli", cache_dir=str(tmp_path / "cache")).latest("a").verdict == "drifting"
    statuses, _ = assess_all(s, at(2), judge=j)
    assert any(f.code == "DRIFT" for f in statuses["a"].flags)


def test_judge_failure_is_recorded_not_raised(lane, tmp_path, monkeypatch):
    lane.agent("a", "#1", 0)
    lane.edit("a", 1, "x.gd")
    s = lane.session()
    monkeypatch.setattr(judge_mod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "boom"))
    j = Judge(backend="cli", cache_dir=str(tmp_path / "cache"))
    r = j.run(s.agents["a"], s)
    assert r.error and j.latest("a") is None


def test_build_prompt_respects_replay_time(lane):
    lane.agent("a", "#1", 0)
    lane.call("a", 1, "Bash", {"command": "early", "description": "early step"})
    lane.call("a", 20, "Bash", {"command": "late", "description": "late step"})
    s = lane.session()
    prompt, n, _ = build_prompt(s.agents["a"], s, at=at(5))
    assert "early step" in prompt and "late step" not in prompt and n == 1


# -- web snapshot -----------------------------------------------------------


def test_web_snapshot_shapes(lane):
    lane.agent("a", "#1 spine", 0)
    lane.call("a", 1, "Bash", {"command": "make test"})
    lane.call("a", 1.5, "Bash", {"command": "make lint"})
    lane.edit("a", 3, "core/ids.gd")
    lane.call("a", 4, "SubagentHandback", {"message": "done"})
    lane.session()
    state = LaneState(SessionRef(str(lane.tmp), lane.sid, 0, 1), Thresholds(), None, None)
    snap = state.snapshot(at(10))
    a = snap["agents"][0]
    assert a["label"] == "#1" and a["state"] == "DONE"
    kinds = [sg["k"] for sg in a["segments"]]
    assert kinds == ["shell", "edit"]  # the two shell calls merge into one segment
    assert a["segments"][0]["n"] == 2
    assert any(m["k"] == "report" for m in a["marks"])
    feed = state.feed("a", at(10))
    assert feed[0]["kind"] == "brief" and feed[-1]["tool"] == "REPORT"


def test_project_name_drops_the_home_directory():
    import os

    from tracon.session import encode_project

    home = encode_project(os.path.expanduser("~"))
    assert SessionRef(f"/x/projects/{home}-code-acme-shop", "s", 0, 1).project_name == "code-acme-shop"
    assert SessionRef("/x/projects/-srv-app", "s", 0, 1).project_name == "srv-app"
