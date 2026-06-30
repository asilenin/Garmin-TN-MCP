"""tools.py — тулзы доступа к обогащённому кэшу (этап 4 финал, §8 ТЗ).

Слой, через который LLM ЧИТАЕТ обогащённое из локальной БД, не из Garmin и не
держа всё в контексте. Три уровня детализации (ответ на «LLM забывает» — модель
никогда не грузит всё, спускается по уровням только где нужно):

  query_index(фильтры)      — массовый дешёвый: каталожные поля по МНОГИМ
                              тренировкам, БЕЗ гистограмм. «Оглавление» архива.
  get_activity_compact(id)  — сводка ОДНОЙ: числа + гистограммы СВЁРНУТЫ в форму
                              (модальность + полки с весами + разброс). Для обзора.
  get_activity_full(id)     — полное обогащение ОДНОЙ, включая сырые гистограммы.
                              Для глубокого разбора.

Фильтры query_index — ФИКСИРОВАННЫЙ набор предикатов, НЕ свободный SQL (защита от
инъекций/полного скана). Обманчивые поля (avg_hr_raw, training_load) в выдаче, но
НЕ фильтруются (§3.1). Всё из БД, без сети.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import profiles
from enrich import histogram_shape
from store import Store


# Фильтруемые поля query_index: только надёжные (§3.1). Обманчивые сюда не входят.
_FILTERABLE = {
    "date_from": ("date >= ?", str),
    "date_to": ("date <= ?", str),
    "sport": ("sport = ?", str),
    "distance_m_min": ("distance_m >= ?", float),
    "distance_m_max": ("distance_m <= ?", float),
    "duration_s_min": ("duration_s >= ?", float),
    "duration_s_max": ("duration_s <= ?", float),
    "moving_time_s_min": ("moving_time_s >= ?", float),
    "max_hr_min": ("max_hr >= ?", int),
    "max_hr_max": ("max_hr <= ?", int),
    "avg_cadence_min": ("avg_cadence >= ?", float),  # каденс надёжен (§5.1)
    "avg_cadence_max": ("avg_cadence <= ?", float),
    "has_biomech_sensor": ("has_biomech_sensor = ?", int),
}

# Поля каталога в выдаче query_index (включая обманчивые — для ЧТЕНИЯ, не фильтра)
_INDEX_OUT = (
    "activity_id", "date", "sport", "distance_m", "duration_s", "moving_time_s",
    "max_hr", "avg_cadence", "avg_hr_raw", "avg_speed_raw", "avg_gct",
    "avg_vert_ratio", "garmin_training_load_derived", "has_biomech_sensor",
    "lap_count",
)


def query_index(slug: str, *, limit: int = 50, order: str = "date_desc",
                **filters) -> dict:
    """Каталожные поля по фильтру (фиксированные предикаты). БЕЗ гистограмм.

    Пример: query_index('anton', date_from='2026-01-01', sport='running',
                         max_hr_min=180, limit=20)
    Неизвестные/нефильтруемые ключи игнорируются (обманчивые поля не фильтруются).
    """
    clauses, params = [], []
    ignored = []
    for key, val in filters.items():
        if key not in _FILTERABLE:
            ignored.append(key)
            continue
        sql_frag, caster = _FILTERABLE[key]
        clauses.append(sql_frag)
        params.append(caster(val))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    order_sql = {"date_desc": "date DESC", "date_asc": "date ASC",
                 "distance_desc": "distance_m DESC",
                 "max_hr_desc": "max_hr DESC"}.get(order, "date DESC")
    cols = ",".join(_INDEX_OUT)
    sql = f"SELECT {cols} FROM activities{where} ORDER BY {order_sql} LIMIT ?"
    params.append(int(limit))

    with Store(profiles.resolve(slug).db_path) as st:
        rows = [dict(r) for r in st.conn.execute(sql, params).fetchall()]
    out: dict[str, Any] = {"count": len(rows), "activities": rows}
    if ignored:
        out["ignored_filters"] = ignored  # честно: эти поля не фильтруются
    return out


def _load_enriched(st: Store, slug: str, aid: int) -> Optional[dict]:
    av = st.meta_get("algo_version")
    if av is None:
        return None
    return st.get_enriched(aid, av)


def _catalog_row(st: Store, aid: int) -> Optional[dict]:
    r = st.conn.execute(
        "SELECT activity_id,date,sport,distance_m,duration_s,moving_time_s,max_hr,"
        "avg_cadence,avg_hr_raw,has_biomech_sensor,lap_count "
        "FROM activities WHERE activity_id=?", (aid,)
    ).fetchone()
    return dict(r) if r else None


def get_activity_compact(slug: str, activity_id: int) -> dict:
    """Сводка одной тренировки: числа + гистограммы СВЁРНУТЫ в форму (§3.1).

    Гистограммы не отдаются сырыми — сворачиваются в {модальность, полки с весами,
    разброс}. Для обзора нескольких тренировок без раздувания контекста.
    Полная форма — через get_activity_full.
    """
    with Store(profiles.resolve(slug).db_path) as st:
        cat = _catalog_row(st, activity_id)
        if cat is None:
            return {"activity_id": activity_id, "error": "не найдено в каталоге"}
        enr = _load_enriched(st, slug, activity_id)
    if enr is None:
        return {**cat, "enriched": False, "note": "обогащение отсутствует"}
    if enr.get("no_stream"):
        return {**cat, "enriched": True, "no_stream": True,
                "note": "детальный поток отсутствовал — только каталожные поля"}
    return {
        **cat,
        "enriched": True,
        "hr_shape": histogram_shape(enr.get("hr_histogram") or {}),
        "pace_shape": histogram_shape(enr.get("pace_histogram") or {}),
        "clusters": enr.get("clusters"),
        "pace_variance": enr.get("pace_variance"),
        "hr_variance": enr.get("hr_variance"),
        "biomech_by_pace": enr.get("biomech_by_pace"),
        "lactate_marks": enr.get("lactate_marks"),
        "elevation": enr.get("elevation"),
    }


def get_activity_full(slug: str, activity_id: int) -> dict:
    """Полное обогащение одной тренировки, включая СЫРЫЕ гистограммы (§3.1).

    Для глубокого разбора, когда нужна точная форма распределения, а не свёртка.
    """
    with Store(profiles.resolve(slug).db_path) as st:
        cat = _catalog_row(st, activity_id)
        if cat is None:
            return {"activity_id": activity_id, "error": "не найдено в каталоге"}
        enr = _load_enriched(st, slug, activity_id)
    if enr is None:
        return {**cat, "enriched": False}
    enr.pop("computed_at", None)
    return {**cat, "enriched": True, **enr}


def get_period_aggregates(slug: str, period_key: Optional[str] = None) -> dict:
    """Кросс-агрегаты по периодам (этап 5, §3.5). Якорь-нейтральные, БЕЗ имён/зон.

    Без period_key — ВСЕ периоды (для динамики формы §5.4: сравнение кварталов).
    С period_key (напр. '2026-Q2') — один период.

    ВАЖНО для LLM при чтении (демаркация — суждения здесь, не в данных):
    - pace_by_hr_grid by_source: если hr_sources={unknown:1.0} в provenance — источник
      НЕ разложен, пульсовая динамика через годы НЕДОСТОВЕРНА (§5.4/§1.7), пометь это.
      unknown = «не знаю, возможно менялся источник», НЕ «однородно».
    - decoupling.by_delta_pace: бины Δpace — ОСИ, не вердикт. Δpace≈0 → дрейф базы
      (decoupling осмыслен); большой Δpace → прогрессив (decoupling = разгон, не дрейф,
      не читай как «база уехала»). Граница — твоя, не зашита.
    - имена/зоны/пороги (easy/threshold, intensity_distribution) — накладываешь ТЫ.
    """
    with Store(profiles.resolve(slug).db_path) as st:
        av = st.meta_get("algo_version")
        if av is None:
            return {"error": "версия не определена"}
        if period_key:
            agg = st.get_aggregate(period_key, av)
            if agg is None:
                return {"period_key": period_key, "error": "период не агрегирован"}
            agg.pop("computed_at", None)
            return agg
        periods = st.all_aggregates(av)
        for p in periods:
            p.pop("computed_at", None)
        return {"algo_version": av, "n_periods": len(periods), "periods": periods}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python tools.py <slug> <index|compact|full|aggregates> [arg]")
        sys.exit(1)
    slug, cmd = sys.argv[1], sys.argv[2]
    if cmd == "index":
        # пример: python tools.py <slug> index sport=running max_hr_min=185 limit=10
        kw = {}
        for arg in sys.argv[3:]:
            if "=" in arg:
                k, v = arg.split("=", 1)
                kw[k] = v
        print(json.dumps(query_index(slug, **kw), ensure_ascii=False, indent=2))
    elif cmd == "aggregates":
        # python tools.py <slug> aggregates [period_key]
        pk = sys.argv[3] if len(sys.argv) > 3 else None
        print(json.dumps(get_period_aggregates(slug, pk), ensure_ascii=False, indent=2))
    elif cmd == "compact":
        print(json.dumps(get_activity_compact(slug, int(sys.argv[3])),
                         ensure_ascii=False, indent=2))
    elif cmd == "full":
        print(json.dumps(get_activity_full(slug, int(sys.argv[3])),
                         ensure_ascii=False, indent=2))
