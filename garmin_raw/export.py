"""One-shot экспорт для тиража: выгружает период (или одну активность) в JSON.

Модель тиража: коуч/атлет один раз авторизуется (garmin-raw-auth), затем гоняет
этот экспорт за нужный период и заливает JSON в чат для анализа. Тот же бэкенд, что
у MCP, — данные идентичны.

Примеры:
    garmin-raw-export --start 2026-06-01 --end 2026-06-21
    garmin-raw-export --start 2026-06-20 --end 2026-06-21 --activity 23321211303 --streams
"""
from __future__ import annotations

import argparse
import json
import sys

from .backend import GarminSource


def main() -> None:
    ap = argparse.ArgumentParser(description="Сырой экспорт Garmin -> JSON")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--sport", default="running")
    ap.add_argument("--activity", type=int, default=None,
                    help="ID одной активности (иначе весь период)")
    ap.add_argument("--streams", action="store_true",
                    help="тянуть посекундные потоки (тяжелее и медленнее)")
    ap.add_argument("--no-comments", action="store_true",
                    help="не тянуть комментарии/лактат (на 1 запрос меньше на активность)")
    ap.add_argument("--out", default="garmin_export.json")
    args = ap.parse_args()

    src = GarminSource()
    acts = src.list_activities(args.start, args.end, args.sport)
    if args.activity:
        acts = [a for a in acts if a.get("activityId") == args.activity]

    bundle = []
    for act in acts:
        aid = act.get("activityId")
        item = {"summary": act, "laps": src.get_activity_laps(aid)}
        if not args.no_comments:
            item["comment"] = src.get_activity_comment(aid)
        if args.streams:
            item["streams"] = src.get_activity_streams(aid)
        bundle.append(item)
        print(
            f"  + {aid} {act.get('activityName')} "
            f"{round(act.get('distance', 0) / 1000, 2)}км",
            file=sys.stderr,
        )

    payload = {"period": [args.start, args.end], "sport": args.sport, "activities": bundle}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    print(f"\nГотово -> {args.out} ({len(bundle)} активностей). Залей файл в чат.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
