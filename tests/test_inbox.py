import json
import os
import shutil
import socket
import sys
import tempfile
import threading
from datetime import timedelta

import pytest

from tracon.advice import compose
from tracon.inbox import find_inbox, send
from tracon.lane import assess_all

from test_lane import T0, lane, refusal, ts  # noqa: F401  (fixture)


@pytest.fixture
def sockdir():
    d = tempfile.mkdtemp(prefix="tr", dir=None if sys.platform == "win32" else "/tmp")  # Unix socket paths max out near 104 bytes
    yield d
    shutil.rmtree(d, ignore_errors=True)


def register(root, pid, sid, sock, updated=1):
    (root / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "sessionId": sid, "messagingSocketPath": sock, "name": f"n{pid}", "status": "idle", "updatedAt": updated}
    ))


def test_find_inbox_matches_session_and_live_pid(tmp_path, sockdir):
    sock = os.path.join(sockdir, "a.sock")
    open(sock, "w").close()
    register(tmp_path, os.getpid(), "sess-1", sock)
    register(tmp_path, 999_999, "sess-2", sock)  # not running
    (tmp_path / "junk.json").write_text("{not json")
    ib = find_inbox("sess-1", str(tmp_path))
    assert ib and ib.pid == os.getpid() and ib.socket == sock and ib.name == f"n{os.getpid()}"
    assert find_inbox("sess-2", str(tmp_path)) is None
    assert find_inbox("nope", str(tmp_path)) is None


def test_find_inbox_needs_the_socket(tmp_path):
    register(tmp_path, os.getpid(), "sess-1", "/tmp/does-not-exist.sock")
    assert find_inbox("sess-1", str(tmp_path)) is None


unix_only = pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="named pipes")
PIPE = r"\\.\pipe\LOCAL\tracon-test-"


@unix_only
def test_send_writes_one_user_line(sockdir):
    path = os.path.join(sockdir, "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    got = []

    def serve():
        conn, _ = srv.accept()
        with conn:
            buf = b""
            while chunk := conn.recv(4096):
                buf += chunk
            got.append(buf)

    t = threading.Thread(target=serve)
    t.start()
    send(path, 'stop "now"\nplease')
    t.join(timeout=3)
    srv.close()
    assert got and got[0].endswith(b"\n") and got[0].count(b"\n") == 1
    assert json.loads(got[0]) == {"type": "user", "message": {"role": "user", "content": 'stop "now"\nplease'}}


@unix_only
def test_send_to_missing_socket_raises(sockdir):
    with pytest.raises(OSError):
        send(os.path.join(sockdir, "gone.sock"), "hi")


@windows_only
def test_find_inbox_takes_a_pipe_without_opening_it(tmp_path):
    pipe = f"{PIPE}{os.getpid()}-find"
    register(tmp_path, os.getpid(), "sess-1", pipe)
    ib = find_inbox("sess-1", str(tmp_path))
    assert ib and ib.socket == pipe and ib.token == ""


@windows_only
def test_find_inbox_reads_the_key_only_for_a_pipe(tmp_path, sockdir):
    pid = os.getpid()
    (tmp_path / f"{pid}.ab12.key").write_text("tok-bare\n")
    (tmp_path / f"{pid}1.cd34.key").write_text("someone-else")  # pid prefix, not this pid
    register(tmp_path, pid, "sess-1", f"{PIPE}{pid}-key")
    assert find_inbox("sess-1", str(tmp_path)).token == "tok-bare"
    sock = os.path.join(sockdir, "a.sock")
    open(sock, "w").close()
    register(tmp_path, pid, "sess-1", sock)
    assert find_inbox("sess-1", str(tmp_path)).token == ""  # a socket never needs it


def test_token_takes_a_bare_or_json_key(tmp_path):
    from tracon.inbox import _token

    (tmp_path / "7.aa.key").write_text('{"token": "tok-json", "v": 1}')
    (tmp_path / "8.bb.key").write_text('"tok-quoted"')
    assert _token(7, str(tmp_path)) == "tok-json"
    assert _token(8, str(tmp_path)) == "tok-quoted"
    assert _token(9, str(tmp_path)) == ""


@windows_only
def test_alive_on_windows_never_signals():
    import subprocess

    from tracon.inbox import _alive

    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert _alive(p.pid)
        assert _alive(p.pid)  # still there: probing didn't kill it
    finally:
        p.kill()
        p.wait()
    assert not _alive(p.pid)


@windows_only
@pytest.mark.parametrize("token", ["", "tok-1"])
def test_send_writes_one_user_line_to_a_pipe(token):
    import _winapi

    pipe = f"{PIPE}{os.getpid()}-send{token}"
    h = _winapi.CreateNamedPipe(
        pipe, _winapi.PIPE_ACCESS_DUPLEX, _winapi.PIPE_WAIT, 1, 65536, 65536, 0, _winapi.NULL,  # byte mode
    )
    got = []

    def serve():
        _winapi.ConnectNamedPipe(h, False)
        buf = b""
        try:
            while True:
                chunk, _ = _winapi.ReadFile(h, 4096)
                buf += chunk
        except OSError:  # the writer closed
            pass
        got.append(buf)

    t = threading.Thread(target=serve)
    t.start()
    try:
        send(pipe, 'stop "now"\nplease', token=token)
        t.join(timeout=3)
    finally:
        _winapi.CloseHandle(h)
    lines = [json.loads(line) for line in got[0].splitlines()] if got and got[0].endswith(b"\n") else []
    assert lines[-1:] == [{"type": "user", "message": {"role": "user", "content": 'stop "now"\nplease'}}]
    assert lines[:-1] == ([{"type": "auth", "token": token}] if token else [])


@windows_only
def test_send_to_missing_pipe_raises():
    with pytest.raises(OSError):
        send(f"{PIPE}{os.getpid()}-gone", "hi")


def test_compose_names_agent_flags_and_advice(lane):  # noqa: F811
    lane.agent("a1", "#12 fix the parser")
    for m in (1, 2, 3):
        lane.call("a1", m, "Bash", {"command": "pytest -k parser"}, ok=False)
    s = lane.session()
    statuses, _ = assess_all(s, T0 + timedelta(minutes=3.5))
    text = compose(statuses["a1"])
    assert text.startswith('[tracon] Agent "#12 fix the parser" (agentId a1)')
    assert "- LOOP:" in text and "pytest -k parser" in text
    assert "SendMessage it the piece it is missing" in text
    assert "Verify before acting" in text


def test_compose_states_judge_concern_once(lane, tmp_path):  # noqa: F811
    from tracon.judge import Judge, Judgment

    lane.agent("a1", "#12 fix the parser")
    lane.call("a1", 1, "Bash", {"command": "pytest -k parser"})
    s = lane.session()
    concern = "It rewrote the lexer, which the brief never asks for."
    j = Judge(backend="cli", cache_dir=str(tmp_path / "cache"))
    j.history["a1"] = [Judgment("drifting", "rewriting lexer", concern, ts(1), 1, made=0.0)]
    statuses, _ = assess_all(s, T0 + timedelta(minutes=1.5), judge=j)
    text = compose(statuses["a1"])
    assert "- DRIFT: " + concern in text
    assert text.count(concern) == 1


def test_compose_skips_advice_for_info_flags(lane):  # noqa: F811
    lane.agent("a1", "#12 fix the parser", 0, brief="Fix the parser. It's slow, so you may run it with nohup in the background.")
    lane.call("a1", 1, "Bash", {"command": "nohup pytest > .scratch/t.log 2>&1 &"})
    s = lane.session()
    statuses, _ = assess_all(s, T0 + timedelta(minutes=1.5))
    text = compose(statuses["a1"])
    assert "- BG:" in text and "its brief says" not in text
    assert "If its brief says foreground" not in text


def test_compose_quotes_the_last_refusal_for_a_drift(lane, tmp_path):  # noqa: F811
    from tracon.judge import Judge, Judgment

    wt = lane.agent("a1", "#768 night plan", 0)
    for m in (1, 2, 3):
        lane.call("a1", m, "Bash", {"command": "for s in a b; do tools/godot-test --strategy=$s; done"}, ok=False, out=refusal(wt))
        lane.call("a1", m + 0.1, "Bash", {"command": "tools/godot-test --strategy=a"})
    s = lane.session()
    j = Judge(backend="cli", cache_dir=str(tmp_path / "cache"))
    j.history["a1"] = [Judgment("drifting", "running walks", "Reworded refused commands several times.", ts(3.1), 6, made=0.0)]
    statuses, _ = assess_all(s, T0 + timedelta(minutes=3.5), judge=j)
    text = compose(statuses["a1"])
    assert "- DENIED" not in text  # info, and DRIFT is loud, so only DRIFT is listed
    quote = next(line for line in text.splitlines() if line.startswith("Its last refusal"))
    assert ", 3 in all): This agent is isolated in the worktree <worktree>," in quote
    assert quote.endswith("run them from <worktree>.")


def test_compose_leaves_out_a_dismissed_concern(lane, tmp_path):  # noqa: F811
    from tracon.judge import Judge, Judgment

    lane.agent("a1", "#12 fix the parser")
    lane.call("a1", 1, "Bash", {"command": "pytest -k parser"})
    s = lane.session()
    j = Judge(backend="cli", cache_dir=str(tmp_path / "cache"))
    j.history["a1"] = [Judgment("drifting", "rewriting lexer", "It rewrote the lexer.", ts(1), 1, made=0.0)]
    j.toggle_dismiss("a1", j.history["a1"][0])
    statuses, _ = assess_all(s, T0 + timedelta(minutes=1.5), judge=j)
    text = compose(statuses["a1"])
    assert "lexer" not in text and "DRIFT" not in text
