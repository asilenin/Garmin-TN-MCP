"""coros_auth_probe.py — auth-спайк T-v2-2: наш headless MCP-клиент к mcp.coros.com.

Доказывает крус: OAuth (браузер ОДИН раз) → токен в profiles/coros-<user>/tokens/ →
вызов queryUserInfo. Второй запуск БЕЗ браузера = headless-рефреш работает.
Запуск: uv run python garmin_raw/coros_auth_probe.py [user]   (default: andrey)
"""
from __future__ import annotations

import asyncio
import os
import sys
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import profiles  # noqa: E402

from mcp.client.auth import OAuthClientProvider, TokenStorage  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.shared.auth import (  # noqa: E402
    OAuthClientMetadata, OAuthToken, OAuthClientInformationFull,
)

SERVER_URL = "https://mcpeu.coros.com/mcp"   # EU-регион (Andrey); mcp.coros.com объявляет ресурс региональным
CALLBACK_PORT = 8765
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"


class FileTokenStorage(TokenStorage):
    """OAuth-токен и client-info — файлами в tokens-каталоге подключения."""

    def __init__(self, tokens_dir: Path):
        self.tok = tokens_dir / "coros_oauth_token.json"
        self.cli = tokens_dir / "coros_oauth_client.json"

    async def get_tokens(self):
        return OAuthToken.model_validate_json(self.tok.read_text()) if self.tok.exists() else None

    async def set_tokens(self, tokens: OAuthToken):
        self.tok.write_text(tokens.model_dump_json(indent=2))

    async def get_client_info(self):
        return (OAuthClientInformationFull.model_validate_json(self.cli.read_text())
                if self.cli.exists() else None)

    async def set_client_info(self, info: OAuthClientInformationFull):
        self.cli.write_text(info.model_dump_json(indent=2))


class _CB(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        if "code" in q:
            _CB.result = {"code": q["code"][0], "state": q.get("state", [None])[0]}
            body = "<h2>COROS авторизация принята. Вернись в терминал.</h2>"
        else:
            body = "<h2>Жду callback…</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *a):
        pass


async def _redirect(url: str) -> None:
    print(f"\n[auth] Открываю браузер для входа в COROS…\n"
          f"[auth] Если не открылось — вставь вручную:\n{url}\n")
    webbrowser.open(url)


async def _callback():
    def _serve():
        srv = HTTPServer(("localhost", CALLBACK_PORT), _CB)
        while not _CB.result:
            srv.handle_request()
        return _CB.result
    r = await asyncio.to_thread(_serve)
    return r["code"], r["state"]


async def main(user: str) -> None:
    slug = profiles.build_slug(user, "coros")
    prof = profiles.resolve(slug); prof.ensure_dirs()
    print(f"[probe] подключение={slug}  токены={prof.tokens_dir}")
    had = (prof.tokens_dir / "coros_oauth_token.json").exists()
    print(f"[probe] токен уже есть? {had}  (True → должно пройти БЕЗ браузера)")

    storage = FileTokenStorage(prof.tokens_dir)
    oauth = OAuthClientProvider(
        server_url=SERVER_URL,
        client_metadata=OAuthClientMetadata(
            client_name="TN Run MCP",
            redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=None,
        ),
        storage=storage,
        redirect_handler=_redirect,
        callback_handler=_callback,
    )

    async with streamablehttp_client(SERVER_URL, auth=oauth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"[probe] initialize OK, тулов у Coros MCP: {len(tools.tools)}")
            res = await session.call_tool("queryUserInfo", {})
            txt = "".join(getattr(c, "text", "") for c in res.content)
            print(f"[probe] queryUserInfo →\n{txt}")

    tok = await storage.get_tokens()
    print(f"\n[probe] refresh_token присутствует? {bool(tok and tok.refresh_token)}  "
          f"(True → headless-рефреш возможен, браузер только раз)")
    print("[probe] ГОТОВО. Запусти второй раз — должно пройти БЕЗ браузера.")


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "andrey"
    asyncio.run(main(u))
