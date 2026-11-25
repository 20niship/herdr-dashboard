"""python3 test_server.py"""
from server import Handler, Session, send

h = Handler.__new__(Handler)
h.token = "secret"
for addr, cookie, ok in [("127.0.0.1", "", True), ("192.168.1.5", "", False),
                         ("192.168.1.5", "token=bad", False), ("192.168.1.5", "token=secret", True)]:
    h.client_address, h.headers = (addr, 1), {"Cookie": cookie}
    assert h.authed() is ok, (addr, cookie)
for host, ok in [("192.168.1.2:8765", True), ("localhost:8765", True), ("evil.com", False), ("", False)]:
    h.headers = {"Host": host}
    assert h.host_ok() is ok, host


def use(i, name, **inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}}


def result(i, r):
    return {"type": "user", "toolUseResult": r, "message": {"content": [{"type": "tool_result", "tool_use_id": i}]}}


s = Session(None)
for line in [
    use("a", "Agent", description="sub"), result("a", {"isAsync": True, "agentId": "ag1"}),
    use("b", "Bash", command="make"), result("b", {"backgroundTaskId": "sh1"}),
    use("c", "Bash", command="ls"), result("c", {"stdout": ""}),
    use("d", "Bash", command="sleep 9"),
    {"type": "queue-operation", "content": "<task-notification>\n<task-id>sh1</task-id>\n<status>completed</status>"},
    {"type": "frame-link", "frameUrl": "https://claude.ai/artifact/x", "title": "T"},
]:
    s.feed(line)
got = {t["title"]: t["status"] for t in s.summary()["tasks"]}
assert got == {"sub": "running", "make": "completed", "sleep 9": "running"}, got
assert s.summary()["artifacts"] == [{"url": "https://claude.ai/artifact/x", "title": "T"}]

for bad in [{}, {"pane": "-x", "text": "a"}, {"pane": "w1:p1", "keys": ["--help"]}, {"pane": "w1:p1"}]:
    try:
        send(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(bad)
print("ok")
