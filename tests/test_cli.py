import json

from tracon.cli import build_parser, main, snapshot_json
from tracon.demo import Demo
from tracon.lane import assess_all
from tracon.session import Session
from tracon.signals import Thresholds


def test_build_parser_json_flag():
    p = build_parser()
    args = p.parse_args(["--once", "--json"])
    assert args.once is True
    assert args.json is True


def test_snapshot_json_empty():
    assert snapshot_json({}) == []
    assert snapshot_json([]) == []


def test_snapshot_json_structure(tmp_path):
    demo = Demo(root=str(tmp_path))
    s = Session(demo.ref)
    s.refresh()
    wts = [a.worktree for a in s.agents.values()]
    statuses, _ = assess_all(s, None, demo.prober.probe(wts), Thresholds(), demo.judge)

    data = snapshot_json(statuses)
    assert len(data) == 7

    # Check that sort order matches sort_key: most urgent first (BLOCKED, IDLE, etc.)
    states = [item["state"] for item in data]
    assert states[0] == "BLOCKED"
    assert states[1] == "IDLE"
    assert states[-1] == "DONE"

    for item in data:
        assert set(item.keys()) == {"id", "description", "state", "flags", "quiet_s", "judge"}
        assert isinstance(item["id"], str)
        assert isinstance(item["description"], str)
        assert isinstance(item["state"], str)
        assert isinstance(item["flags"], list)
        assert all(isinstance(f, str) for f in item["flags"])
        assert isinstance(item["quiet_s"], int)
        assert item["judge"] is None or isinstance(item["judge"], str)

    by_desc = {item["description"].split()[0]: item for item in data}
    assert by_desc["#15"]["state"] == "BLOCKED"
    assert by_desc["#15"]["judge"] == "on_track"
    assert "LOOP" in by_desc["#16"]["flags"]
    assert "STUCK" in by_desc["#16"]["flags"]
    assert by_desc["#16"]["judge"] == "stuck"
    assert "DRIFT" in by_desc["#13"]["flags"]
    assert by_desc["#13"]["judge"] == "drifting"
    assert by_desc["#17"]["judge"] is None  # not judged in demo


def test_cli_once_json(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rc = main(["--demo", "--once", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.err == ""

    data = json.loads(captured.out)
    assert isinstance(data, list)
    assert len(data) == 7
    blocked = [a for a in data if a["state"] == "BLOCKED"]
    assert len(blocked) == 1
    assert blocked[0]["description"].startswith("#15")


def test_cli_json_implies_once(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rc = main(["--demo", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data) == 7


def test_cli_once_json_no_judge(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rc = main(["--demo", "--once", "--json", "--no-judge"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert all(a["judge"] is None for a in data)


def test_cli_once_text_not_json(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rc = main(["--demo", "--once"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "lane: checkout-v2" in captured.out
    assert "BLOCKED" in captured.out
    # Not valid JSON
    try:
        json.loads(captured.out)
        assert False, "Should not be JSON"
    except json.JSONDecodeError:
        pass
