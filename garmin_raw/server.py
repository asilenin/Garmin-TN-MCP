"""MCP-сервер: 6 сырьевых тулзов поверх GarminSource.

Только сырьё, никаких derived-метрик. Подключение к Garmin — ленивое, по токенам
из ~/.garminconnect (создаются командой garmin-raw-auth).
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from .backend import GarminSource

mcp = FastMCP("garmin-raw")
_src = GarminSource()  # ленивое подключение при первом вызове


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


@mcp.tool()
def list_activities(start_date: str, end_date: str, sport: str = "running") -> str:
    """Список тренировок за период (сырые сводки). Даты в ISO 'YYYY-MM-DD'."""
    return _dump(_src.list_activities(start_date, end_date, sport))


@mcp.tool()
def get_activity_laps(activity_id: int) -> str:
    """Данные по кругам: пульс/каденс/мощность/шаг/высота на каждый круг (lapDTOs)."""
    return _dump(_src.get_activity_laps(activity_id))


@mcp.tool()
def get_activity_streams(activity_id: int) -> str:
    """Посекундные потоки (HR, каденс, высота, уклон, мощность, шаг, дыхание...)."""
    return _dump(_src.get_activity_streams(activity_id))


@mcp.tool()
def get_activity_comment(activity_id: int) -> str:
    """Комментарий активности + распарсенный лактат (LA:x.x -> ммоль)."""
    return _dump(_src.get_activity_comment(activity_id))


@mcp.tool()
def get_activity_lactate(activity_id: int) -> str:
    """Числовые отметки лактата из developer-поля (TN Splits View): [(время, ммоль, круг)]."""
    return _dump(_src.get_activity_lactate(activity_id))


@mcp.tool()
def get_wellness(date: str) -> str:
    """Восстановление за день: сон, HRV, RHR, стресс, Body Battery. Дата 'YYYY-MM-DD'."""
    return _dump(_src.get_wellness(date))


@mcp.tool()
def get_personal_records() -> str:
    """Личные рекорды по дистанциям."""
    return _dump(_src.get_personal_records())


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
