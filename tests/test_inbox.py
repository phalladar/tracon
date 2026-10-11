import json
import os
import shutil
import socket
import tempfile
import threading
from datetime import timedelta

import pytest

from tracon.advice import compose
from tracon.inbox import find_inbox, send
from tracon.lane import assess_all

from test_lane import T0, lane  # noqa: F401  (fixture)


@pytest.fixture
def sockdir():
    d = tempfile.mkdtemp(prefix="tr", dir="/tmp")  # Unix socket paths max out near 104 bytes
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


def test_send_to_missing_socket_raises(sockdir):
    with pytest.raises(OSError):
        send(os.path.join(sockdir, "gone.sock"), "hi")


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
