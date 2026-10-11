import json
from datetime import datetime, timezone

import pytest

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


def test_snapshot_json_empty(tmp_path):
    demo = Demo(root=str(tmp_path))
    s = Session(demo.ref)
    data = snapshot_json(s, {})
    assert data["session"] == s.ref.session_id
    assert data["agents"] == []


def test_snapshot_json_structure(tmp_path):
    demo = Demo(root=str(tmp_path))
    s = Session(demo.ref)
    s.refresh()
    wts = [a.worktree for a in s.agents.values()]
    statuses, _ = assess_all(s, None, demo.prober.probe(wts), Thresholds(), demo.judge)

    at_dt = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc)
    data = snapshot_json(s, statuses, at=at_dt)
    assert set(data.keys()) == {"session", "lane", "as_of", "agents"}
    assert data["session"] == s.ref.session_id
    assert data["lane"] == "checkout-v2"
    assert data["as_of"] == at_dt.astimezone().isoformat()

    agents = data["agents"]
    assert len(agents) == 7

    # Check that sort order matches sort_key: most urgent first (BLOCKED, IDLE, etc.)
    states = [item["state"] for item in agents]
    assert states[0] == "BLOCKED"
    assert states[1] == "IDLE"
    assert states[-1] == "DONE"

    for item in agents:
        assert set(item.keys()) == {"id", "description", "state", "severity", "flags", "quiet_s", "judge"}
        assert isinstance(item["id"], str)
        assert isinstance(item["description"], str)
        assert isinstance(item["state"], str)
        assert isinstance(item["severity"], str)
        assert isinstance(item["flags"], list)
        for f in item["flags"]:
            assert set(f.keys()) == {"code", "severity", "detail"}
            assert isinstance(f["code"], str)
            assert isinstance(f["severity"], str)
            assert isinstance(f["detail"], str)
        assert isinstance(item["quiet_s"], int)
        if item["judge"] is not None:
            assert set(item["judge"].keys()) == {"verdict", "dismissed"}
            assert isinstance(item["judge"]["verdict"], str)
            assert isinstance(item["judge"]["dismissed"], bool)

    by_desc = {item["description"].split()[0]: item for item in agents}
    assert by_desc["#15"]["state"] == "BLOCKED"
    assert by_desc["#15"]["severity"] == "bad"
    assert by_desc["#15"]["judge"] == {"verdict": "on_track", "dismissed": False}

    flag_codes_16 = {f["code"] for f in by_desc["#16"]["flags"]}
    assert "LOOP" in flag_codes_16
    assert "STUCK" in flag_codes_16
    assert by_desc["#16"]["judge"] == {"verdict": "stuck", "dismissed": False}

    flag_codes_13 = {f["code"] for f in by_desc["#13"]["flags"]}
    assert "DRIFT" in flag_codes_13
    assert by_desc["#13"]["judge"] == {"verdict": "drifting", "dismissed": False}
    assert by_desc["#17"]["judge"] is None  # not judged in demo


def test_cli_once_json(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rc = main(["--demo", "--once", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.err == ""

    data = json.loads(captured.out)
    assert set(data.keys()) == {"session", "lane", "as_of", "agents"}
    agents = data["agents"]
    assert len(agents) == 7
    blocked = [a for a in agents if a["state"] == "BLOCKED"]
    assert len(blocked) == 1
    assert blocked[0]["description"].startswith("#15")


def test_cli_json_without_once_errors(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["--demo", "--json"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "--json needs --once" in captured.err


def test_cli_once_json_no_judge(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rc = main(["--demo", "--once", "--json", "--no-judge"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert all(a["judge"] is None for a in data["agents"])


def test_cli_once_text_not_json(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rc = main(["--demo", "--once"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "lane: checkout-v2" in captured.out
    assert "BLOCKED" in captured.out
    with pytest.raises(json.JSONDecodeError):
        json.loads(captured.out)
