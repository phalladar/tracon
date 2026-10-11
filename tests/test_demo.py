from tracon.demo import Demo
from tracon.lane import assess_all
from tracon.session import Session
from tracon.signals import Thresholds


def snapshot(demo):
    s = Session(demo.ref)
    s.refresh()
    wts = [a.worktree for a in s.agents.values()]
    statuses, _ = assess_all(s, None, demo.prober.probe(wts), Thresholds(), demo.judge)
    return {st.agent.description.split()[0]: st for st in statuses.values()}


def test_demo_shows_every_kind_of_trouble(tmp_path):
    demo = Demo(root=str(tmp_path))
    by = snapshot(demo)
    assert {k: st.state for k, st in by.items()} == {
        "#11": "REPORTED", "#12": "WORKING", "#13": "WORKING", "#14": "RUNNING",
        "#15": "BLOCKED", "#16": "WORKING", "#17": "IDLE",
    }
    codes = {k: {f.code for f in st.flags} for k, st in by.items()}
    assert {"COLLIDE", "DRIFT", "CTX"} <= codes["#13"]
    assert {"LOOP", "ERRORS", "STUCK"} <= codes["#16"]
    assert {"STALE-BASE", "DENIED"} <= codes["#17"]
    assert by["#12"].judgment.verdict == "on_track"


def test_demo_stays_live_and_never_judges(tmp_path):
    demo = Demo(root=str(tmp_path))
    for i in range(24):
        demo.step(i)
    by = snapshot(demo)
    assert by["#12"].state == "WORKING" and by["#14"].state in ("RUNNING", "WORKING")
    s = Session(demo.ref)
    s.refresh()
    assert len(s.agents) == 7 and not any(demo.judge.due(a, False) for a in s.agents.values())
