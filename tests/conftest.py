import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from watchpoint.app import App
from watchpoint.gui import make_server


class FakeServer:
    """A local HTTP server whose responses a test sets per path."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep test output quiet
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                server.requests.append({"path": parts.path, "query": query, "headers": dict(self.headers)})
                route = server.routes.get(parts.path)
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                status, ctype, body, *more = route(query, dict(self.headers)) if callable(route) else route
                data = (
                    body
                    if isinstance(body, bytes)
                    else (json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body).encode()
                )
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (more[0] if more else {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    # Requests to the local fake server must not go through a proxy from the environment.
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def gui(tmp_path: Path):
    src = tmp_path / "spec.md"
    src.write_text("# 仕様\n- 保持は 30 日\n", encoding="utf-8")
    cfg = tmp_path / "targets.yaml"
    cfg.write_text(
        f"targets:\n  - name: 仕様\n    source: '{src}'\n    tags: [案件A]\n    points: 保持の日数を見る\n",
        encoding="utf-8",
    )
    app = App(cfg)
    server = make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    port = server.server_address[1]

    def call(path: str, body: dict | None = None, token: str | None = app.token, host: str | None = None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        headers = {"Host": host or f"127.0.0.1:{port}"}
        if token:
            headers["X-Watchpoint-Token"] = token
        data = None
        if body is not None or raw is not None:
            data = raw if raw is not None else json.dumps(body).encode()
            headers["Content-Type"] = "text/plain" if raw is not None else "application/json"
        conn.request("POST" if data is not None else "GET", path, body=data, headers=headers)
        res = conn.getresponse()
        payload = res.read()
        conn.close()
        ctype = res.getheader("Content-Type", "")
        return res.status, (json.loads(payload) if ctype.startswith("application/json") else payload.decode())

    call.port = port

    def check(body: dict) -> dict:
        status, job = call("/api/check", body)
        assert status == 200, job
        deadline = time.monotonic() + 30
        while job["active"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
            job = call("/api/job")[1]["job"]
        return job

    call.check = check
    yield call, src, app
    server.shutdown()
    server.server_close()
