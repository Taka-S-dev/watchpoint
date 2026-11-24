"""The page for watchpoint: its HTML, the API routes it calls, and the local server for --browser.

The operations behind the routes are in app.py. The page is shown either in a window of its
own (desktop.py, which calls the routes directly) or, with --browser, served on 127.0.0.1,
where every API call must carry the token printed into the page so other sites open in the
same browser cannot drive it.
"""

from __future__ import annotations

import json
import os
import secrets
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import editing
from .app import ApiError, App

MAX_BODY = 1024 * 1024


FILE_TYPES = [
    ("見張れるファイル", "*.xlsx *.xlsm *.csv *.tsv *.docx *.txt *.md *.json *.html *.htm *.log *.yaml *.yml *.pdf"),
    ("すべてのファイル", "*.*"),
]


def pick_path(folder: bool, start: str | None) -> str | None:
    """The native dialog, run on its own thread with its own Tk root. None when cancelled."""
    import tkinter
    from tkinter import filedialog

    initial = None
    if start:
        p = Path(os.path.expanduser(start.strip().strip('"')))
        initial = str(p if p.is_dir() else p.parent) if p.parent.exists() else None
    root = tkinter.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)  # in front of the browser, not behind it
        root.update()
        if folder:
            chosen = filedialog.askdirectory(parent=root, initialdir=initial, title="見張るフォルダ", mustexist=True)
        else:
            chosen = filedialog.askopenfilename(
                parent=root, initialdir=initial, title="見張るファイル", filetypes=FILE_TYPES
            )
    finally:
        root.destroy()
    return os.path.normpath(chosen) if chosen else None


DENIED = (
    "<!doctype html><meta charset='utf-8'><title>watchpoint</title>"
    "<p>watchpoint gui を起動したターミナルに出た URL（?k= の付いたもの）から開いてください。</p>"
).encode()


def cookie_name(port: int) -> str:
    return f"watchpoint_{port}"  # cookies are not separated by port; two guis must not overwrite each other


def launch_url(app: App, server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/?k={app.token}"


CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def assemble_page() -> str:
    """gui.html with gui.css and gui.js put inside it: one page, as the window takes it (it loads no
    files), kept in three files only so each is easier to work on. No build step."""
    files = resources.files("watchpoint")
    html = files.joinpath("gui.html").read_text(encoding="utf-8")
    css = files.joinpath("gui.css").read_text(encoding="utf-8")
    js = files.joinpath("gui.js").read_text(encoding="utf-8")
    for name, text, closing in (("gui.css", css, "</style"), ("gui.js", js, "</script")):
        if closing in text.lower():
            raise ValueError(f"{name} contains {closing}>, which would end it early inside the page")
    return html.replace("/*__WATCHPOINT_CSS__*/\n", css, 1).replace("//__WATCHPOINT_JS__\n", js, 1)


def page_html(token: str, desktop: bool = False, prefs: dict[str, str] | None = None) -> str:
    """The page, with the key for the server or, in the window, the note that it talks to Python directly.

    The window's page has no storage of its own (it is not loaded from any address), so the few
    settings it remembers come in as prefs.
    """
    html = assemble_page()
    html = html.replace("__WATCHPOINT_TOKEN__", token).replace("__WATCHPOINT_DESKTOP__", "1" if desktop else "0")
    if desktop:
        data = json.dumps(prefs or {}, ensure_ascii=True).replace("<", "\\u003c")  # cannot end the <script>
        html = html.replace("/*prefs*/null/*prefs*/", data, 1)
    if desktop:  # no response headers in the window: the policy goes into the page, with no network at all
        policy = CSP.replace("connect-src 'self'", "connect-src 'none'").replace(" frame-ancestors 'none';", "")
        meta = f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
        html = html.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n' + meta, 1)
    return html


def _page(token: str) -> bytes:
    return page_html(token).encode("utf-8")


def _names(body: dict) -> list[str] | None:
    """None (absent or null) means every target; a list means exactly those, each once."""
    names = body.get("names")
    if names is None:
        return None
    if not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
        raise ValueError("names は文字列の並び")
    return list(dict.fromkeys(names))  # the same target twice would be fetched twice at once


def _seen(body: dict) -> dict[str, str] | None:
    seen = body.get("seen")
    if seen is None:
        return None
    if not (isinstance(seen, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in seen.items())):
        raise ValueError("seen は name → snapshot")
    return seen


def _via(body: dict) -> str:
    via = body.get("via") or "page"
    if via not in ("page", "page-auto", "added"):
        raise ValueError("via は page / page-auto / added")
    return via


def _str(body: dict, key: str, required: bool = True) -> str | None:
    v = body.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str):
        raise ValueError(f"{key} は文字列")
    return v


def api_routes(app: App) -> dict[str, dict]:
    """The operations the page can ask for, by method and path. The server and the window share them."""
    return {
        "GET": {
            "/api/overview": lambda q: app.overview(),
            "/api/target": lambda q: app.detail(q["name"]),
            "/api/diff": lambda q: app.diff(q["name"], int(q["from"]), int(q["to"])),
            "/api/text": lambda q: app.text(q["name"], int(q.get("gen", 1))),
            "/api/log": lambda q: app.log(),
            "/api/job": lambda q: app.job_view(),
            "/api/config": lambda q: app.config_text(),
            "/api/commands": lambda q: app.allowed_commands(),
            "/api/fields": lambda q: app.fields(q["name"]),
        },
        "POST": {
            "/api/check": lambda b: app.start_check(_names(b), bool(b.get("due")), _via(b)),
            "/api/ack": lambda b: app.ack(_names(b), _str(b, "note", required=False), _seen(b)),
            "/api/markdown": lambda b: app.markdown(_names(b), places=bool(b.get("places"))),
            "/api/preview": lambda b: app.preview(_str(b, "name"), _str(b, "text", required=False)),
            "/api/place": lambda b: app.open_place(_str(b, "name")),
            "/api/open": lambda b: app.open_copy(_str(b, "name"), bool(b.get("text"))),
            "/api/config/validate": lambda b: app.validate(_str(b, "text")),
            "/api/config/save": lambda b: app.save_config(_str(b, "text"), _str(b, "hash")),
            "/api/config/add-target": lambda b: app.add_target(_str(b, "text"), b.get("item")),
            "/api/config/describe": lambda b: editing.describe_source(_str(b, "source")),
            "/api/peek": lambda b: app.peek(_str(b, "source")),
            "/api/peek/office": lambda b: app.peek_office(_str(b, "source")),
            "/api/peek/sample": lambda b: app.peek_sample(_str(b, "path")),
            "/api/browse": lambda b: app.browse(bool(b.get("folder")), _str(b, "start", required=False)),
            "/api/targets/add": lambda b: app.add_and_save(b.get("item")),
            "/api/commands/revoke": lambda b: app.revoke_commands(b.get("ids")),
            "/api/commands/allow": lambda b: app.allow_command(_str(b, "name"), b.get("command")),
            "/api/rules/try": lambda b: app.try_rule(_str(b, "name"), b.get("rule")),
            "/api/rules/add": lambda b: app.add_rule(_str(b, "name"), b.get("rule")),
            "/api/rules/remove": lambda b: app.remove_rule(_str(b, "name"), int(b.get("index"))),
            "/api/rules/expect": lambda b: app.set_expect_update(_str(b, "name"), _str(b, "when", required=False)),
            "/api/targets/remove": lambda b: app.remove_target(_str(b, "name"), b.get("history") == "drop"),
            "/api/targets/every": lambda b: app.set_every(_str(b, "name"), _str(b, "every", required=False)),
        },
    }


def answer(run) -> tuple[int, dict]:
    """Run one operation and turn what happened into a status and a JSON-able body."""
    try:
        return HTTPStatus.OK, run()
    except ApiError as e:
        return e.status, {"error": str(e)}
    except (KeyError, ValueError, TypeError) as e:
        return HTTPStatus.BAD_REQUEST, {"error": f"要求の形が違います: {e}"}
    except Exception as e:  # noqa: BLE001 - an answer instead of a dropped connection
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(e).__name__}: {e}"}


def make_server(app: App, port: int = 0) -> ThreadingHTTPServer:
    page = _page(app.token)
    routes = api_routes(app)

    class Handler(BaseHTTPRequestHandler):
        server_version = "watchpoint"

        def log_message(self, *a):  # the terminal shows only what the user needs
            pass

        def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            # Refuse requests that reach this port under another host name (DNS rebinding).
            port_ = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"127.0.0.1:{port_}", f"localhost:{port_}")

        def _authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Watchpoint-Token", ""), app.token)

        def _cookie_ok(self) -> bool:
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except CookieError:
                return False
            morsel = jar.get(cookie_name(self.server.server_address[1]))
            return morsel is not None and secrets.compare_digest(morsel.value, app.token)

        def _page(self, query: str) -> None:
            """The page holds the key, so it goes only to whoever opened the URL printed in the terminal.

            That URL carries the key once; it is swapped for a cookie (HttpOnly, SameSite=Strict) and
            dropped from the address bar. Another program or user on this PC that only knows the port
            gets nothing.
            """
            key = parse_qs(query).get("k", [""])[0]
            if key:
                if not secrets.compare_digest(key, app.token):
                    self._send(HTTPStatus.FORBIDDEN, DENIED, "text/html; charset=utf-8")
                    return
                cookie = f"{cookie_name(self.server.server_address[1])}={app.token}; Path=/; HttpOnly; SameSite=Strict"
                self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie})
                return
            if self._cookie_ok():
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            else:
                self._send(HTTPStatus.FORBIDDEN, DENIED, "text/html; charset=utf-8")

        def _handle(self, method: str) -> None:
            if not self._host_ok():
                self._json(HTTPStatus.FORBIDDEN, {"error": "この名前ではつなげません"})
                return
            parts = urlsplit(self.path)
            if method == "GET" and parts.path in ("/", "/index.html"):
                self._page(parts.query)
                return
            if parts.path not in routes[method]:
                self._json(HTTPStatus.NOT_FOUND, {"error": "ありません"})
                return
            if not self._authorized():
                self._json(
                    HTTPStatus.FORBIDDEN, {"error": "ページを開き直してください（watchpoint gui を起動し直した）"}
                )
                return
            run = routes[method][parts.path]
            if method == "GET":
                status, data = answer(lambda: run({k: v[0] for k, v in parse_qs(parts.query).items()}))
            else:
                status, data = answer(lambda: run(self._body()))
            self._json(status, data)

        def _body(self) -> dict:
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON で送ってください")
            length = self.headers.get("Content-Length") or "0"
            if not length.isdigit():
                raise ApiError(HTTPStatus.BAD_REQUEST, "Content-Length が数ではありません")
            n = int(length)
            if n > MAX_BODY:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "大きすぎます")
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("オブジェクトではない")
            return data

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
