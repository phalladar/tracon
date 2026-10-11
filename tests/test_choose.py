"""Choosing the session to watch: the Claude Code session in the next Ghostty split first, then the
newest in this project (or a parent folder's), then the newest anywhere."""

import json
import os

import pytest

from tracon import ghostty, transcript
from tracon.ghostty import Pane, claude_title, pick_neighbour
from tracon.inbox import Live
from tracon.session import Chooser, encode_project, find_sessions, find_sessions_near, session_ref
from tracon.transcript import read_title

REPO = "/work/repo"


def live(sid, cwd=REPO, interactive=True, updated=1):
    return Live(1, sid, cwd, interactive, updated=updated)


def write_session(root, cwd, sid, agents=1, title="", name="", mtime=1000.0):
    proj = root / encode_project(cwd)
    proj.mkdir(parents=True, exist_ok=True)
    recs = [{"type": "user", "timestamp": "2026-10-11T12:00:00Z", "cwd": cwd, "message": {"content": "hi"}}]
    if title:
        recs.append({"type": "ai-title", "aiTitle": title, "sessionId": sid})
    if name:
        recs.append({"type": "agent-name", "agentName": name, "sessionId": sid})
    lead = proj / f"{sid}.jsonl"
    lead.write_text("".join(json.dumps(r) + "\n" for r in recs))
    paths = [lead]
    if agents:
        sub = proj / sid / "subagents"
        sub.mkdir(parents=True)
        for i in range(agents):
            p = sub / f"agent-a{i}.jsonl"
            p.write_text("")
            paths.append(p)
    for p in paths:
        os.utime(p, (mtime, mtime))


# -- matching a split to a session -------------------------------------------


def test_claude_title_strips_the_status_glyph_and_rejects_plain_titles():
    assert claude_title("✳ Expedite cards waiting on questions") == "Expedite cards waiting on questions"
    assert claude_title("◐ restart-stalled-agent-watch") == "restart-stalled-agent-watch"
    assert claude_title("~/development/winsitch") is None
    assert claude_title("vim notes.md") is None


def test_the_split_in_my_tab_is_matched_by_title():
    panes = [
        Pane("t1", "me", "◎ old-run", REPO),
        Pane("t1", "c1", "✳ Expedite cards", REPO),
        Pane("t2", "c2", "◐ restart-stalled-agent-watch", REPO),
    ]
    sessions = [live("new", updated=2), live("old", updated=1)]
    titles = {"new": "Expedite cards", "old": "restart-stalled-agent-watch"}
    assert pick_neighbour("me", panes, sessions, lambda lv: titles[lv.session_id], "◎ ") == "new"
    # The same Claude pane, seen from tab 2's tracon, is somebody else's neighbour.
    panes[2] = Pane("t2", "me2", "◎ x", REPO)
    panes.append(Pane("t2", "c2", "◐ restart-stalled-agent-watch", REPO))
    assert pick_neighbour("me2", panes, sessions, lambda lv: titles[lv.session_id], "◎ ") == "old"


def test_other_tracon_panes_and_background_sessions_are_ignored():
    panes = [Pane("t1", "me", "◎ a", REPO), Pane("t1", "tr2", "◎ Expedite cards", REPO)]
    sessions = [live("s1"), live("bg", interactive=False)]
    titles = {"s1": "Expedite cards", "bg": "Expedite cards"}
    assert pick_neighbour("me", panes, sessions, lambda lv: titles[lv.session_id], "◎ ") is None
    panes.append(Pane("t1", "c1", "✳ Expedite cards", REPO))
    assert pick_neighbour("me", panes, sessions, lambda lv: titles[lv.session_id], "◎ ") == "s1"


def test_a_session_too_new_for_a_title_is_matched_when_it_is_the_only_one():
    panes = [Pane("t1", "me", "◎ a", REPO), Pane("t1", "c1", "✳ Claude Code", REPO)]
    titles = {"fresh": "", "busy": "Board"}
    sessions = [live("fresh"), live("busy")]
    assert pick_neighbour("me", panes, sessions, lambda lv: titles[lv.session_id]) == "fresh"
    sessions.append(live("fresh2"))  # two untitled sessions here: can't tell which
    titles["fresh2"] = ""
    assert pick_neighbour("me", panes, sessions, lambda lv: titles[lv.session_id]) is None


def test_no_match_when_tracon_has_the_tab_to_itself():
    panes = [Pane("t1", "me", "◎ a", REPO), Pane("t2", "c1", "✳ Board", REPO)]
    assert pick_neighbour("me", panes, [live("s")], lambda lv: "Board") is None
    assert pick_neighbour("gone", panes, [live("s")], lambda lv: "Board") is None


# -- titles and discovery ------------------------------------------------------


def test_read_title_prefers_the_rename_and_reads_from_the_end(tmp_path, monkeypatch):
    p = tmp_path / "s.jsonl"
    filler = json.dumps({"type": "user", "message": {"content": "x" * 500}}) + "\n"
    title = json.dumps({"type": "ai-title", "aiTitle": "Board"}) + "\n"
    name = json.dumps({"type": "agent-name", "agentName": "restart-watch"}) + "\n"
    p.write_text(title + filler * 50)
    monkeypatch.setattr(transcript, "TITLE_TAIL", 1024)  # the title is far from the end: grows the read
    assert read_title(str(p)) == "Board"
    p.write_text(title + filler * 50 + title + name + filler)
    assert read_title(str(p)) == "restart-watch"
    p.write_text(filler)
    assert read_title(str(p)) == ""
    assert read_title(str(tmp_path / "missing.jsonl")) == ""


def test_sessions_are_found_from_a_subfolder_of_the_project(tmp_path):
    write_session(tmp_path, REPO, "s1", mtime=1000)
    write_session(tmp_path, REPO, "s2", mtime=2000)
    write_session(tmp_path, "/work/other", "s3", mtime=3000)
    near = find_sessions_near(f"{REPO}/board/cards", str(tmp_path))
    assert [r.session_id for r in near] == ["s2", "s1"]
    assert find_sessions_near("/elsewhere/x", str(tmp_path)) == []


def test_a_session_without_subagents_is_listed_only_when_asked_for_by_id(tmp_path):
    write_session(tmp_path, REPO, "lonely", agents=0, mtime=5000)
    assert find_sessions(root=str(tmp_path)) == []
    ref = session_ref("lonely", REPO, str(tmp_path))
    assert ref and ref.n_agents == 0 and ref.mtime == 5000
    assert session_ref("lonely", root=str(tmp_path)) == ref  # found without the cwd too
    assert session_ref("nope", REPO, str(tmp_path)) is None


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Projects on disk, the live-session registry, and a Ghostty with tracon split next to a Claude."""
    projects, registry = tmp_path / "projects", tmp_path / "sessions"
    registry.mkdir()
    write_session(projects, REPO, "older", mtime=1000, title="Board")
    write_session(projects, REPO, "newer", mtime=2000, title="Night plan")
    write_session(projects, "/work/other", "elsewhere", mtime=3000)
    write_session(projects, REPO, "beside", agents=0, mtime=1500, title="Expedite cards")
    (registry / "1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "beside", "cwd": REPO, "kind": "interactive"}))
    panes = [Pane("t1", "me", "◎ x", REPO), Pane("t1", "c1", "✳ Expedite cards", REPO)]
    monkeypatch.setattr(ghostty, "panes", lambda: panes)
    return Chooser(cwd=f"{REPO}/sub", root=str(projects), sessions_root=str(registry), ignore_prefix="◎ "), panes


def test_chooser_puts_the_split_first_even_without_subagents(world):
    chooser, _ = world
    chooser.terminal = "me"
    assert [r.session_id for r in chooser.refs()] == ["beside", "newer", "older"]
    assert chooser.neighbour == "beside"


def test_chooser_falls_back_to_this_project_then_anywhere(world):
    chooser, panes = world
    assert [r.session_id for r in chooser.refs()] == ["newer", "older"]  # no Ghostty terminal known
    chooser.terminal = "me"
    panes[1] = Pane("t1", "c1", "~/work/repo", REPO)  # the split is a plain shell
    assert [r.session_id for r in chooser.refs()] == ["newer", "older"] and chooser.neighbour is None
    chooser.cwd = "/nowhere"
    assert [r.session_id for r in chooser.refs()][0] == "elsewhere"
    chooser.project = REPO  # --project: that project only, no split matching
    assert [r.session_id for r in chooser.refs()] == ["newer", "older"]
