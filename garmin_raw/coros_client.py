"""coros_client.py — MCP-клиент к официальному Coros MCP (T-v2-2, FETCH-ADAPTER/Coros).

Auth: клиентский OAuth mcp-пакета (DCR + PKCE, scope openid mcp.tools offline_access →
refresh-token → браузер ОДИН раз), per-connection токен в profiles/coros-<user>/tokens/.
Регион в URL ОБЯЗАТЕЛЕН (OAuth protected-resource региональный): mcpeu/mcpus/mcpcn.
Факты — QA: ЭТАП МУЛЬТИПРОВАЙДЕР, Coros-fetch РЕШЕНО ФАКТОМ (FETCH-ADAPTER).
"""
from __future__ import annotations

import asyncio
import webbrowser
from contextlib import asynccontextmanager
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession
from mcp.shared.auth import (
    OAuthClientMetadata, OAuthToken, OAuthClientInformationFull,
)

REGION_URL = {
    "eu": "https://mcpeu.coros.com/mcp",
    "us": "https://mcpus.coros.com/mcp",
    "cn": "https://mcpcn.coros.com/mcp",
}
_CALLBACK_PORT = 8765
_REDIRECT_URI = f"http://localhost:{_CALLBACK_PORT}/callback"


class FileTokenStorage(TokenStorage):
    """OAuth-токен и client-info — файлами в tokens-каталоге подключения."""

    def __init__(self, tokens_dir: Path):
        self._t = tokens_dir / "coros_oauth_token.json"
        self._c = tokens_dir / "coros_oauth_client.json"

    async def get_tokens(self):
        return OAuthToken.model_validate_json(self._t.read_text()) if self._t.exists() else None

    async def set_tokens(self, tokens: OAuthToken):
        self._t.write_text(tokens.model_dump_json(indent=2))

    async def get_client_info(self):
        return (OAuthClientInformationFull.model_validate_json(self._c.read_text())
                if self._c.exists() else None)

    async def set_client_info(self, info: OAuthClientInformationFull):
        self._c.write_text(info.model_dump_json(indent=2))


class _CB(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        if "code" in q:
            _CB.result = {"code": q["code"][0], "state": q.get("state", [None])[0]}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h2>COROS авторизация принята. Вернись в терминал.</h2>".encode("utf-8"))

    def log_message(self, *a):
        pass


async def _redirect(url: str):
    print(f"\n[coros-auth] Открой в браузере (должно открыться само):\n{url}\n")
    webbrowser.open(url)


async def _callback():
    def serve():
        _CB.result = {}
        srv = HTTPServer(("localhost", _CALLBACK_PORT), _CB)
        while not _CB.result:
            srv.handle_request()
        return _CB.result
    r = await asyncio.to_thread(serve)
    return r["code"], r["state"]


@asynccontextmanager
async def coros_session(tokens_dir: Path, region: str = "eu"):
    """Авторизованная ClientSession к региональному Coros MCP. Токен переиспользуется из
    tokens_dir (headless); браузер — только если токена нет (первый раз)."""
    tokens_dir.mkdir(parents=True, exist_ok=True)
    if region not in REGION_URL:
        raise ValueError(f"регион {region!r} не из {list(REGION_URL)}")
    url = REGION_URL[region]
    oauth = OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name="TN Run MCP", redirect_uris=[_REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"], scope=None),
        storage=FileTokenStorage(tokens_dir),
        redirect_handler=_redirect, callback_handler=_callback)
    async with streamablehttp_client(url, auth=oauth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def result_text(res) -> str:
    return "".join(getattr(c, "text", "") for c in res.content)


def result_structured(res):
    """structuredContent тула, если он есть (иначе None → парсим текст)."""
    return getattr(res, "structuredContent", None)
