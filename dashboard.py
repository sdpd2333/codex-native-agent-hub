"""Token-protected loopback web UI; all operations are dispatched to the agent event loop."""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler

import common


class PanelServer:
    def __init__(self, dispatch, loop: asyncio.AbstractEventLoop, html_path: Path | None = None):
        self.dispatch = dispatch
        self.loop = loop
        self.html = (html_path or common.APP_DIR / "dashboard.html").read_bytes()
        self.script = (common.APP_DIR / "dashboard.js").read_bytes()
        self.token = secrets.token_urlsafe(32)
        self.contexts: dict[str, dict] = {}
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # Never put a local access token or configuration into HTTP logs.

            def reply(self, status, body, content_type="application/json; charset=utf-8"):
                if isinstance(body, dict):
                    body = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def route(self, method):
                if self.headers.get("Host") != panel.host:
                    self.reply(403, {"error": "仅允许本机面板访问。"})
                    return
                parsed = urlsplit(self.path)
                prefix = "/" + panel.token + "/"
                if parsed.query or not parsed.path.startswith(prefix):
                    self.reply(404, {"error": "面板地址已失效。请重新获取面板地址。"})
                    return
                action = parsed.path[len(prefix):]
                context = {}
                if action.startswith("views/"):
                    parts = action.split("/", 2)
                    if len(parts) != 3 or parts[1] not in panel.contexts:
                        self.reply(404, {"error": "对话面板地址已失效，请重新获取。"})
                        return
                    context = panel.contexts[parts[1]]
                    action = parts[2]
                if method == "GET" and action == "":
                    self.reply(200, panel.html, "text/html; charset=utf-8")
                    return
                if method == "GET" and action == "dashboard.js":
                    self.reply(200, panel.script, "text/javascript; charset=utf-8")
                    return
                if method == "GET" and action == "api/health":
                    self.reply(200, {"ok": True, "application": "native-agent-hub"})
                    return
                allowed = {"GET": {"api/jobs", "api/config", "api/storage", "api/archive-policy"},
                           "POST": {"api/models", "api/config", "api/test", "api/tasks", "api/cancel", "api/changes", "api/job",
                                    "api/jobs", "api/archive", "api/archive-policy", "api/archive-preview", "api/archive-bulk",
                                    "api/storage-policy", "api/storage-preview", "api/storage-cleanup"}}
                if action not in allowed.get(method, set()):
                    self.reply(404, {"error": "接口不存在。"})
                    return
                payload = {}
                if method == "POST":
                    origin = self.headers.get("Origin")
                    if (self.headers.get("X-Agent-Hub") != "1" or
                            origin is not None and origin != "http://" + panel.host):
                        self.reply(403, {"error": "请求来源无效。"})
                        return
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        self.reply(415, {"error": "请求必须为 JSON。"})
                        return
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        # A 100,000-character task can exceed 256 KiB in UTF-8 or escaped JSON.
                        limit = 2 * 1024 * 1024 if action == "api/tasks" else 256 * 1024
                        if not 0 < length <= limit:
                            self.reply(413, {"error": "请求大小超出范围。"})
                            return
                        payload = json.loads(self.rfile.read(length))
                        if not isinstance(payload, dict):
                            raise ValueError()
                    except (ValueError, UnicodeError):
                        self.reply(400, {"error": "请求 JSON 无效。"})
                        return
                payload["_panel_context"] = dict(context)
                future = asyncio.run_coroutine_threadsafe(panel.dispatch(method, action[4:], payload), panel.loop)
                try:
                    value = future.result(timeout=150)
                    self.reply(200, value)
                except FutureTimeout:
                    future.cancel()
                    self.reply(504, {"error": "操作超时，请稍后重试。"})
                except Exception as exc:
                    # Known errors are already sanitized at the operation boundary.
                    detail = str(exc) if isinstance(exc, common.RelayError) else "本地服务操作失败，请重试。"
                    self.reply(409 if isinstance(exc, common.ConfigurationConflict) else 400, {"error": detail})

            def do_GET(self):
                self.route("GET")

            def do_POST(self):
                self.route("POST")

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.host = f"127.0.0.1:{self.http.server_port}"
        self.url = f"http://{self.host}/{self.token}/"
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def register_context(self, conversation_id: str, project_dir: str = "") -> str:
        context = {"conversation_id": conversation_id, "project_dir": project_dir}
        for token, existing in self.contexts.items():
            if existing == context:
                return self.url + "views/" + token + "/"
        token = secrets.token_urlsafe(24)
        self.contexts[token] = context
        return self.url + "views/" + token + "/"

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)


def local_request(url: str, data: dict | None = None, *, timeout: int = 5) -> dict:
    """Call another local worker without using system HTTP proxies or following redirects."""
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.username or parsed.query:
        raise common.RelayError("本地面板地址无效。")
    headers = {"Accept": "application/json", "Content-Type": "application/json", "X-Agent-Hub": "1"}
    request = Request(url, headers=headers, data=None if data is None else json.dumps(data).encode("utf-8"))
    # The only server used here is the project's own loopback server, which never redirects.
    with build_opener(ProxyHandler({}), common._NoCrossOriginRedirect()).open(request, timeout=timeout) as response:
        if response.geturl() != url:
            raise common.RelayError("本地面板发生重定向。")
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise common.RelayError("本地响应过大。")
        return json.loads(raw)


def existing_panel() -> str | None:
    try:
        data = json.loads((common.STATE_DIR / "panel.json").read_text(encoding="utf-8"))
        url = data["url"]
        if local_request(url + "api/health").get("application") == "native-agent-hub":
            return url
    except (OSError, ValueError, KeyError, TypeError, common.RelayError):
        pass
    return None
