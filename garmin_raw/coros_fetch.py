"""coros_fetch.py — CorosFetch: каталог (инкремент 1) через Coros MCP (T-v2-2).

Инкремент 1: fetch_catalog (querySportRecords → нормализованные строки activities).
querySportRecords НЕ жжёт FIT-квоту → тестируется вживую в любой день.
DRY-RUN main: печатает строки, в БД НЕ пишет.
Запуск: uv run python garmin_raw/coros_fetch.py <user> [region] [start yyyymmdd] [end yyyymmdd]
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import profiles  # noqa: E402
from coros_client import coros_session, result_text, result_structured  # noqa: E402

# sportType → (sport, sport_class). Из карты тулов Coros (см. querySportRecords).
# PROV-SPECIFIC-PROVENANCE: провайдер-специфика словарём, не суждение.
_SPORT = {
    100: ("outdoor_run", "run"), 101: ("indoor_run", "run"),
    102: ("trail_run", "run"), 103: ("track_run", "run"),
    104: ("hike", "other"), 105: ("mountain_climb", "other"),
    200: ("outdoor_bike", "ride"), 201: ("indoor_bike", "ride"), 202: ("e_bike", "ride"),
    203: ("gravel_bike", "ride"), 204: ("mtb", "ride"), 205: ("mtb_e", "ride"),
    299: ("helmet_bike", "ride"),
    300: ("pool_swim", "swim"), 301: ("open_water_swim", "swim"),
    400: ("gym_cardio", "strength"), 401: ("gps_cardio", "strength"), 402: ("strength", "strength"),
    900: ("walk", "other"), 901: ("jump_rope", "other"), 902: ("stair", "other"),
    903: ("elliptical", "other"), 904: ("yoga", "other"), 905: ("pilates", "other"),
    906: ("boxing", "other"),
}


def _sport(code: int):
    return _SPORT.get(code, (f"coros_{code}", "other"))


def _hms_to_s(hms: str | None):
    if not hms:
        return None
    parts = [int(x) for x in hms.split(":")]
    s = 0
    for p in parts:
        s = s * 60 + p
    return s


def _parse_records_text(txt: str) -> list[dict]:
    """Грубый парс текстового querySportRecords → нормализованные строки. ФРАГИЛЬНО
    (формат человекочитаемый) — используется, только если structuredContent нет."""
    rows = []
    # сплит по началу строки "N. " (multiline): отделяет шапку и записи надёжно
    for b in re.split(r"(?m)^\s*\d+\.\s", txt):
        m = re.search(r"LabelId:\s*(\d+)\s*\|\s*SportType:\s*(\d+)", b)
        if not m:
            continue
        st = int(m.group(2))
        sport, sclass = _sport(st)
        g = lambda rx: (re.search(rx, b) or [None, None])[1]  # noqa: E731
        rows.append({
            "activity_id": int(m.group(1)),
            "sportType": st, "sport": sport, "sport_class": sclass,
            "date": g(r"—\s*(\d{4}-\d{2}-\d{2})"),
            "start_ts": int(g(r"startTimestamp=(\d+)") or 0) or None,
            "distance_km": float(g(r"Distance:\s*([\d.]+)\s*km") or 0) or None,
            "duration_s": _hms_to_s(g(r"Duration:\s*([\d:]+)")),
            "avg_hr_raw": int(g(r"Avg HR:\s*(\d+)") or 0) or None,
            "avg_pace": g(r"Average Pace:\s*([\d:]+)\s*/km"),
        })
    return rows


async def fetch_catalog(user: str, region: str, start: str, end: str):
    slug = profiles.build_slug(user, "coros")
    prof = profiles.resolve(slug); prof.ensure_dirs()
    async with coros_session(prof.tokens_dir, region) as session:
        res = await session.call_tool("querySportRecords", {
            "startDate": start, "endDate": end, "sportTypeCodes": [65535],
            "minDistanceKm": 0, "maxDistanceKm": 100000, "minDurationMinutes": 0,
            "maxDurationMinutes": 1000000, "maxAveragePace": "", "locationKeyword": "",
            "limit": 300, "timezone": "Europe/Moscow"})
        struct = result_structured(res)
        if struct:
            return {"structured": struct}
        return {"rows": _parse_records_text(result_text(res)), "raw_head": result_text(res)[:300]}


async def main():
    user = sys.argv[1] if len(sys.argv) > 1 else "andrey"
    region = sys.argv[2] if len(sys.argv) > 2 else "eu"
    start = sys.argv[3] if len(sys.argv) > 3 else "20210101"
    end = sys.argv[4] if len(sys.argv) > 4 else "20260709"
    out = await fetch_catalog(user, region, start, end)
    if "structured" in out:
        print("[catalog] structuredContent ЕСТЬ (чистый парс возможен). Форма:")
        print(json.dumps(out["structured"], ensure_ascii=False)[:1200])
        return
    rows = out["rows"]
    print(f"[catalog] structuredContent НЕТ → парсим текст. Нормализовано строк: {len(rows)}")
    for r in rows[:6]:
        print("  ", json.dumps(r, ensure_ascii=False))
    print("  … (первые 6). DRY-RUN — в БД не писал.")


if __name__ == "__main__":
    asyncio.run(main())
