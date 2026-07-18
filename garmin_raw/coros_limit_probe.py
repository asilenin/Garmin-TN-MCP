"""coros_limit_probe.py — эмпирический замер дневного лимита FIT-запросов Coros.

Через уже-авторизованный токен (headless) запрашивает FIT-URL по активностям по одному
(limit=1 = 1 единица квоты) и считает, сколько пройдёт до отказа. ЖЖЁТ КВОТУ НАМЕРЕННО
(заявлено 50/день). Запуск: uv run python garmin_raw/coros_limit_probe.py [user]
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import profiles  # noqa: E402
from coros_auth_probe import (  # noqa: E402
    SERVER_URL, REDIRECT_URI, FileTokenStorage, _redirect, _callback,
)
from mcp.client.auth import OAuthClientProvider  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.shared.auth import OAuthClientMetadata  # noqa: E402


def _text(res) -> str:
    return "".join(getattr(c, "text", "") for c in res.content)


async def main(user: str) -> None:
    slug = profiles.build_slug(user, "coros")
    prof = profiles.resolve(slug); prof.ensure_dirs()
    oauth = OAuthClientProvider(
        server_url=SERVER_URL,
        client_metadata=OAuthClientMetadata(
            client_name="TN Run MCP", redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"], scope=None),
        storage=FileTokenStorage(prof.tokens_dir),
        redirect_handler=_redirect, callback_handler=_callback)

    async with streamablehttp_client(SERVER_URL, auth=oauth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            rec = await session.call_tool("querySportRecords", {
                "startDate": "20210101", "endDate": "20260709", "sportTypeCodes": [65535],
                "minDistanceKm": 0, "maxDistanceKm": 100000, "minDurationMinutes": 0,
                "maxDurationMinutes": 1000000, "maxAveragePace": "", "locationKeyword": "",
                "limit": 300, "timezone": "Europe/Moscow"})
            acts = re.findall(r"LabelId:\s*(\d+)\s*\|\s*SportType:\s*(\d+)", _text(rec))
            print(f"[limit] активностей в списке: {len(acts)}")
            if not acts:
                print("[limit] пустой список, сырой ответ:\n", _text(rec)[:500]); return

            ok = 0
            for i, (label, st) in enumerate(acts, 1):
                r = await session.call_tool(
                    "queryActivityFitFileDownloadUrls",
                    {"labelId": label, "sportType": int(st), "limit": 1})
                rt = _text(r)
                if "http" in rt.lower() and ".fit" in rt.lower():
                    ok += 1
                    if ok % 5 == 0:
                        print(f"[limit] выдано FIT-URL: {ok}")
                else:
                    print(f"\n[limit] ОТКАЗ на запросе #{i} (успешных ДО него: {ok}).")
                    print(f"[limit] ответ сервера:\n{rt[:1000]}")
                    break
                await asyncio.sleep(0.3)
            else:
                print(f"\n[limit] лимит НЕ достигнут за {ok} запросов (активности кончились). "
                      f"Фактический дневной лимит ≥ {ok} (заявлено 50).")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "andrey"))
