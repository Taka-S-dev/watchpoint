"""HTTP GET for the web fetchers: credentials, redirects, size and time limits, retries."""

from __future__ import annotations

import base64
import time
from typing import TYPE_CHECKING

from ..credentials import get_secret

if TYPE_CHECKING:
    from ..config import Target

HTTP_RETRY_SECONDS = 2.0
USER_AGENT = "watchpoint/0.1"


def _ssl_verify(target: Target):
    if target.verify is False:
        return False
    if isinstance(target.verify, str):
        return target.verify
    try:  # use the OS certificate store, so a TLS-inspecting proxy's certificate is trusted
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001
        return True


def _auth_headers(target: Target) -> dict[str, str]:
    a = target.auth
    if a is None:
        return {}
    secret = get_secret(a.secret)
    if a.type == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    if a.type == "basic":
        token = base64.b64encode(f"{a.user}:{secret}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {a.header or "": secret}


MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 50 * 1024 * 1024


def _origin(url) -> tuple[str, str, int | None]:
    return (url.scheme, url.host, url.port)


def _redact(text: str, secrets_: list[str]) -> str:
    for s in secrets_:
        if s:
            text = text.replace(s, "***")
    return text


def _get_once(c, target: Target, url: str, params, headers: dict[str, str], private: set[str]):
    """One GET with redirects followed by hand, read under a total deadline and a size cap."""
    import httpx

    deadline = time.monotonic() + max(target.timeout * 3, 30.0)
    start = c.build_request("GET", url, params=params).url
    req_url, send = start, dict(headers)
    for _ in range(MAX_REDIRECTS + 1):
        r = c.send(c.build_request("GET", req_url, headers=send), stream=True)
        try:
            if r.is_redirect and r.has_redirect_location:
                nxt = req_url.join(r.headers["location"])
                if _origin(nxt) != _origin(start):
                    if private:
                        raise PermissionError(
                            f"別のサイト（{nxt.host}）へ転送されたので、資格情報を送らずに止めました: {url}"
                        )
                    send = {k: v for k, v in send.items() if k.lower() in ("user-agent", "accept")}
                req_url = nxt
                continue
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError(f"応答が {MAX_RESPONSE_BYTES // 1024 // 1024} MB を超えました: {url}")
                if time.monotonic() > deadline:
                    raise httpx.ReadTimeout("total deadline")
                chunks.append(chunk)
            final = httpx.Response(r.status_code, headers=r.headers, content=b"".join(chunks), request=r.request)
            final.moved_from = start if req_url != start else None
            return final
        finally:
            r.close()
    raise ConnectionError(f"転送が {MAX_REDIRECTS} 回を超えました: {url}")


def http_get(target: Target, url: str, params=None, accept: str | None = None):
    import httpx

    auth = _auth_headers(target)
    headers = {"User-Agent": USER_AGENT, **target.headers, **auth}
    private = {k for k in target.headers} | set(auth)
    hidden = list(auth.values()) + list(target.headers.values())
    if accept:
        headers.setdefault("Accept", accept)
    r = None
    for attempt in (1, 2):
        try:
            with httpx.Client(
                timeout=target.timeout, verify=_ssl_verify(target), follow_redirects=False, trust_env=True
            ) as c:
                r = _get_once(c, target, url, params, headers, private)
        except httpx.TimeoutException:
            raise TimeoutError(f"{target.timeout:.0f} 秒待っても応答が終わりませんでした: {url}") from None
        except (httpx.LocalProtocolError, httpx.UnsupportedProtocol) as e:
            # Built wrongly here (a header value with a stray space or control character, a bad URL):
            # retrying cannot help, and the error text may quote the secret.
            raise ValueError(
                f"要求を組み立てられませんでした（{type(e).__name__}: {_redact(str(e), hidden)}）。"
                f"ヘッダーや資格情報の値に空白や制御文字が入っていないか確かめてください: {url}"
            ) from None
        except httpx.TransportError as e:
            if attempt == 1:  # a dropped connection is often gone on the next try
                time.sleep(HTTP_RETRY_SECONDS)
                continue
            raise ConnectionError(
                f"接続できませんでした（{type(e).__name__}: {_redact(str(e), hidden)}）: {url}"
            ) from None
        if r.status_code in (429, 502, 503, 504) and attempt == 1:
            wait = r.headers.get("retry-after", "")
            time.sleep(min(float(wait), 30.0) if wait.isdigit() else HTTP_RETRY_SECONDS)
            continue
        break
    assert r is not None
    if r.status_code in (401, 403):
        hint = (
            "資格情報（auth / secret）を確かめてください"
            if target.auth
            else "ログインが要るページです。auth を設定してください"
        )
        raise PermissionError(f"HTTP {r.status_code}。{hint}: {url}")
    if r.status_code >= 400:
        raise ConnectionError(f"HTTP {r.status_code}: {url}")
    moved = getattr(r, "moved_from", None)
    if moved is not None and r.request.url.path.rstrip("/") != moved.path.rstrip("/"):
        # Typically a sign-in page answering 200 after the session expired: comparing it
        # would report the whole page as changed.
        raise PermissionError(
            f"別のページ（{r.request.url.path}）へ転送されました。ログイン画面の可能性があります。"
            f"転送先が正しいページなら source をその URL にしてください: {url}"
        )
    return r
