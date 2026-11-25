#!/usr/bin/env python3
"""herdr の状態を閲覧・操作するダッシュボード。標準ライブラリのみ。"""
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).parent
PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
PORT = int(os.environ.get("PORT", 8765))
BIND = os.environ.get("HOST", "0.0.0.0")
TOKEN_FILE = Path.home() / ".config" / "herdr-dashboard" / "token"
# tailscale serve などのプロキシ経由で使う場合のみ、追加ホスト名をカンマ区切りで指定する
HOSTS = {"localhost", socket.gethostname().lower(), f"{socket.gethostname().lower()}.local", *filter(None, os.environ.get("HERDR_DASH_HOSTS", "").split(","))}

PANE_RE = re.compile(r"\w[\w:-]*")
KEY_RE = re.compile(r"[a-z0-9][a-z0-9+_-]{0,19}")


def herdr(*args):
    r = subprocess.run([HERDR, *args], capture_output=True, text=True, errors="replace", timeout=10)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or f"herdr exit {r.returncode}")
    return r.stdout


def load_token():
    """他端末からのアクセスに必要なトークン。ブックマークが使えるよう再起動しても変えない。"""
    try:
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(16)
    with open(os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(token)
    return token


def own_ips():
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=5, check=True).stdout
        # コンテナ/VM 用の仮想インターフェースは他端末から届かないので除く
        return [a["local"] for i in json.loads(out) if not i["ifname"].startswith(("docker", "br-", "veth", "virbr"))
                for a in i["addr_info"] if "local" in a]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    try:  # macOS などの `ip` が無い環境
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    ips = re.findall(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", out, re.M)
    return [ip for ip in ips if not ip.startswith(("127.", "169.254."))]


def urls(token):
    ips = own_ips() if BIND == "0.0.0.0" else [] if BIND == "127.0.0.1" else [BIND]
    return [f"http://{ip}:{PORT}/?token={token}" for ip in ips]


def tag(text, name):
    m = re.search(f"<{name}>(.*?)</{name}>", text, re.S)
    return m.group(1) if m else None


class Session:
    """Claude Code の transcript(jsonl) を差分読みして subagent/shell/task/artifact を追う。"""

    # ponytail: Claude を再起動すると終了通知が来ず running のまま残る。必要ならプロセス開始時刻で除外する

    def __init__(self, path):
        self.path, self.offset = path, 0
        self.tasks, self.artifacts = {}, {}

    def update(self):
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            for line in f:
                if not line.endswith(b"\n"):
                    break  # 書き込み途中の行は次回読む
                self.offset += len(line)
                try:
                    self.feed(json.loads(line))
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass

    def feed(self, d):
        kind = d.get("type")
        if kind == "frame-link":
            if d.get("frameUrl"):
                self.artifact(d["frameUrl"], d.get("title"))
        elif kind == "queue-operation":
            self.notify(d.get("content"))
        elif kind == "attachment":
            self.notify(d["attachment"].get("prompt"))
        elif kind in ("assistant", "user"):
            content = d["message"]["content"]
            if isinstance(content, str):
                self.notify(content)
                return
            for b in content:
                if b.get("type") == "tool_use":
                    self.tool_use(b, d.get("timestamp"))
                elif b.get("type") == "tool_result":
                    self.tool_result(b, d.get("toolUseResult"))

    def artifact(self, url, title):
        old = self.artifacts.pop(url, None)
        self.artifacts[url] = title or old or url

    def tool_use(self, b, ts):
        kinds = {"Agent": "agent", "Task": "agent", "Bash": "shell", "Monitor": "monitor"}
        if b["name"] in kinds:
            i = b["input"]
            title = i.get("description") or i.get("command") or b["name"]
            self.tasks[b["id"]] = {"kind": kinds[b["name"]], "title": title[:200], "status": "running",
                                   "start": ts, "id": None, "bg": False}

    def tool_result(self, b, r):
        r = r if isinstance(r, dict) else {}
        if r.get("url") and r.get("title"):
            self.artifact(r["url"], r["title"])
        if r.get("task_id"):  # TaskStop
            self.finish(r["task_id"], "stopped")
        key = b.get("tool_use_id")
        t = self.tasks.get(key)
        if not t:
            return
        bg = r.get("backgroundTaskId") or r.get("taskId") or (r.get("isAsync") and r.get("agentId"))
        if bg:
            t.update(id=bg, bg=True)
        elif t["kind"] == "agent":
            t["status"] = "failed" if b.get("is_error") else "completed"
        else:
            del self.tasks[key]  # 終わったフォアグラウンドのコマンドは表示しない

    def notify(self, text):
        if not isinstance(text, str) or not text.startswith("<task-notification>"):
            return
        status = tag(text, "status") or ("expired" if "Monitor expired" in text else None)
        if status:
            self.finish(tag(text, "task-id"), status)

    def finish(self, task_id, status):
        for t in self.tasks.values():
            if task_id and t["id"] == task_id and t["status"] == "running":
                t["status"] = status

    def summary(self):
        tasks = list(self.tasks.values())
        running = [t for t in tasks if t["status"] == "running"]
        done = [t for t in tasks if t["status"] != "running"][-30:]
        return {"tasks": running[::-1] + done[::-1],
                "artifacts": [{"url": u, "title": t} for u, t in reversed(self.artifacts.items())]}


sessions = {}
lock = threading.Lock()


def session_info(sid):
    if not re.fullmatch(r"[\w-]+", sid):
        return None
    with lock:
        s = sessions.get(sid)
        if not s:
            path = next(PROJECTS.glob(f"*/{sid}.jsonl"), None)
            if not path:
                return None
            s = sessions[sid] = Session(path)
        try:
            s.update()
        except OSError:
            return None
        return s.summary()


def state():
    snap = json.loads(herdr("api", "snapshot"))["result"]["snapshot"]
    for p in snap["panes"]:
        sess = p.get("agent_session") or {}
        if sess.get("agent") == "claude":
            p["claude"] = session_info(sess.get("value", ""))
    return {k: snap[k] for k in ("workspaces", "tabs", "panes")}


def send(body):
    pane, text, keys = body.get("pane"), body.get("text"), body.get("keys")
    if not isinstance(pane, str) or not PANE_RE.fullmatch(pane):
        raise ValueError("bad pane")
    if keys is not None:
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and KEY_RE.fullmatch(k) for k in keys):
            raise ValueError("bad keys")
        herdr("pane", "send-keys", pane, *keys)
    elif isinstance(text, str):
        # run = テキスト + Enter
        herdr("pane", "run" if body.get("enter") else "send-text", pane, text)
    else:
        raise ValueError("text or keys required")


class Handler(BaseHTTPRequestHandler):
    token = None  # __main__ で設定する

    def reply(self, code, body, ctype="application/json", headers=()):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self):
        # DNS rebinding 対策。rebinding にはドメイン名が必要なので IP 直指定は許可する
        host = urlsplit("//" + self.headers.get("Host", "")).hostname or ""
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return host in HOSTS

    def matches(self, value):
        return bool(self.token) and hmac.compare_digest(value.encode(), self.token.encode())

    def authed(self):
        # 本端末からは不要。他端末はトークン(cookie)が必要
        if ipaddress.ip_address(self.client_address[0]).is_loopback:
            return True
        try:
            c = SimpleCookie(self.headers.get("Cookie", "")).get("token")
        except CookieError:
            return False
        return bool(c) and self.matches(c.value)

    def do_GET(self):
        if not self.host_ok():
            return self.reply(403, {"error": "forbidden host"})
        url = urlsplit(self.path)
        login = url.path == "/" and self.matches(parse_qs(url.query).get("token", [""])[0])
        if not (login or self.authed()):
            return self.reply(401, "トークン付きの URL (?token=...) で開いてください", "text/plain")
        try:
            if url.path == "/":
                cookie = [("Set-Cookie", f"token={self.token}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax")]
                return self.reply(200, (ROOT / "index.html").read_text(), "text/html", cookie if login else ())
            if url.path == "/api/state":
                return self.reply(200, {**state(), "urls": urls(self.token)})
            if url.path == "/api/pane":
                q = parse_qs(url.query)
                pane = q.get("id", [""])[0]
                if not PANE_RE.fullmatch(pane):
                    return self.reply(400, {"error": "bad pane"})
                source = "recent-unwrapped" if q.get("wrap") == ["1"] else "recent"
                out = herdr("pane", "read", pane, "--source", source, "--lines", "400", "--format", "ansi")
                return self.reply(200, {"text": out})
            self.reply(404, {"error": "not found"})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def do_POST(self):
        # このAPIは任意コマンド実行と同等。他サイトからのリクエスト(CSRF)は必ず弾く
        origin = self.headers.get("Origin")
        if (not self.host_ok()
                or self.headers.get_content_type() != "application/json"
                or (origin and urlsplit(origin).netloc != self.headers.get("Host"))):
            return self.reply(403, {"error": "forbidden"})
        if not self.authed():
            return self.reply(401, {"error": "unauthorized"})
        if self.path != "/api/input":
            return self.reply(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if not 0 < n <= 100_000:
                raise ValueError("bad length")
            body = json.loads(self.rfile.read(n))
            if not isinstance(body, dict):
                raise ValueError("bad body")
            send(body)
            self.reply(200, {"ok": True})
        except ValueError as e:
            self.reply(400, {"error": str(e)})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    Handler.token = load_token()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    if BIND in ("0.0.0.0", "127.0.0.1"):
        print(f"本端末:  http://localhost:{PORT}")
    for u in urls(Handler.token):
        print(f"他端末:  {u}")
        try:  # ponytail: segno が無い(uv を使わない)場合は QR を省略する
            import segno
            segno.make(u).terminal(compact=True)
        except ImportError:
            pass
    print(end="", flush=True)
    server.serve_forever()
