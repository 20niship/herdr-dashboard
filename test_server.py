"""python3 test_server.py"""
import server
from server import Handler, Session, send

# ip コマンドがなくても落ちずに IP を返す
real_run = server.subprocess.run


def no_ip(*args, **kwargs):
    raise FileNotFoundError("ip")


server.subprocess.run = no_ip
fallback = server.own_ips()
server.subprocess.run = real_run
assert fallback and not fallback[0].startswith("127."), fallback

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
# 会話履歴: システム挿入文は除き、ページングできる
s = Session(None)
s.feed({"type": "user", "message": {"content": "hello"}})
s.feed({"type": "user", "message": {"content": "<system-reminder>x</system-reminder>"}})
s.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
s.feed(use("z", "Bash", command="ls"))
assert [i["role"] for i in s.log] == ["user", "assistant", "tool"], s.log
assert (s.history(limit=2)["start"], s.history(before=1)["end"], len(s.history(after=2)["items"])) == (1, 1, 1)

# 全履歴: スクロールしながら読んだ画面を重複なくつなぎ、位置を元に戻す
buf = [str(n) for n in range(25)]
pos = {"offset": 3}


def fake_rpc(method, params):
    if method == "pane.get":
        return {"pane": {"scroll": {"viewport_rows": 10, "max_offset_from_bottom": 15, "offset_from_bottom": 3}}}
    if method == "pane.scroll":
        pos["offset"] = params["offset_from_bottom"]
        return {}
    end = len(buf) - pos["offset"]
    return {"read": {"text": "\n".join(buf[end - 10:end])}}


server.rpc = fake_rpc
assert server.scrollback("w1:p1").split("\n") == buf
assert pos["offset"] == 3
print("ok")
