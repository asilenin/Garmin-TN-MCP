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
import re
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
        "avg_cadence,avg_hr_raw,has_biomech_sensor,lap_count,hr_source,device_model "
        "FROM activities WHERE activity_id=?", (aid,)
    ).fetchone()
    if r is None:
        return None
    row = dict(r)
    # hr_source/device_model — условная эмиссия (7.6-2a): NULL (не посчитано,
    # нет enriched) → ключа НЕТ; 'unknown' (посчитано: источник не разложен, Q12/Q15)
    # → отдаётся КАК ЗНАЧЕНИЕ. Схлопнуть NULL и unknown нельзя: LLM обязан видеть
    # «не знаю» как факт (§5.4 — пульсовая динамика под флагом источника).
    for k in ("hr_source", "device_model"):
        if row.get(k) is None:
            row.pop(k, None)
    return row


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
        # user_data подмешивается на ЧТЕНИИ (§3.6): из хранимого validation+раствора,
        # streams/laps НЕ трогаем. Ключ только при наличии меток (чистота обзора).
        av = st.meta_get("algo_version")
        user_marks = st.get_user_marks_resolved(activity_id, av or "")
    if enr is None:
        base = {**cat, "enriched": False, "note": "обогащение отсутствует"}
        if user_marks:
            base["user_marks"] = user_marks
        return base
    if enr.get("no_stream"):
        base = {**cat, "enriched": True, "no_stream": True,
                "note": "детальный поток отсутствовал — только каталожные поля"}
        if user_marks:
            base["user_marks"] = user_marks
        return base
    out = {
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
    if user_marks:
        out["user_marks"] = user_marks   # рукотворные — отдельно от watch/comment lactate
    return out


def get_activity_full(slug: str, activity_id: int) -> dict:
    """Полное обогащение одной тренировки, включая СЫРЫЕ гистограммы (§3.1).

    Для глубокого разбора, когда нужна точная форма распределения, а не свёртка.
    """
    with Store(profiles.resolve(slug).db_path) as st:
        cat = _catalog_row(st, activity_id)
        if cat is None:
            return {"activity_id": activity_id, "error": "не найдено в каталоге"}
        enr = _load_enriched(st, slug, activity_id)
        av = st.meta_get("algo_version")
        user_marks = st.get_user_marks_resolved(activity_id, av or "")
    if enr is None:
        base = {**cat, "enriched": False}
        if user_marks:
            base["user_marks"] = user_marks
        return base
    enr.pop("computed_at", None)
    out = {**cat, "enriched": True, **enr}
    if user_marks:
        out["user_marks"] = user_marks
    return out


def cache_status(slug: str) -> dict:
    """Состояние кэша профиля (для LLM: что вообще есть, без сети). Профиль-нейтрально:
    store.status() отдаёт schema_version/algo_version/counts/range/last_sync/db_bytes,
    без slug/путей. Тонкая обёртка над Store.status()."""
    with Store(profiles.resolve(slug).db_path) as st:
        return st.status()


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


# --------------------------------------------------------------------------- #
# user_data — запись рукотворных меток (этап 7, §3.6). Форма = MCP-контракт минус
# декоратор: slug явный (не env/дефолт), возврат структурный, ошибки — значения.
# Этап 8 обернёт @mcp.tool + прокинет slug из профильного endpoint (тонко).
# CACHE-ONLY: в сеть НЕ ходим (закачка сырья — только sync). Промах кэша → честный
# pending, дозакачка (sync) + recompute добивают. §границы tool↔сеть.
# --------------------------------------------------------------------------- #
_LAP_REF_RE = re.compile(r"\s*lap\s*\d+\s*", re.IGNORECASE)


def _stream_first_ts(stream: Optional[dict]) -> Optional[float]:
    """Первая секунда потока (directTimestamp[0]) — тот же clock, что argmin резолвера.
    Лёгкий парс без numpy (нужно одно число, не весь массив)."""
    if not stream:
        return None
    rows = stream.get("activityDetailMetrics") or []
    descs = stream.get("metricDescriptors") or []
    if not rows:
        return None
    ts_idx = next((m.get("metricsIndex") for m in descs
                   if m.get("key") == "directTimestamp"), None)
    if ts_idx is None:
        return None
    mv = rows[0].get("metrics", [])
    v = mv[ts_idx] if ts_idx < len(mv) else None
    return float(v) if v is not None else None


def _gmt_to_ms(s: Optional[str]) -> Optional[int]:
    """GMT-строка ('YYYY-MM-DD HH:MM:SS' / ISO с 'T' и долями) → эпоха-мс UTC.
    Последний фолбэк якоря: парсинг строки, точность зависит от формата."""
    if not s:
        return None
    from datetime import datetime, timezone
    t = str(s).strip().replace("T", " ")
    if "." in t:
        t = t.split(".", 1)[0]
    try:
        dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _activity_start_ms(st: Store, activity_id: int,
                       stream: Optional[dict]) -> tuple[Optional[int], str]:
    """Якорь старта для конвертации elapsed→wall-clock. Возвращает (start_ms, source).

    Порядок (QA этап7): ts[0] когда streams есть (тот же ноль, что argmin — рассинхрон
    невозможен по построению; поток уже открыт для резолва → бесплатно) → beginTimestamp
    из summary_json (эпоха-мс, без потока; streams нет → метка pending, at_time для
    будущего резолва) → start_time GMT-строка (парсинг) → None.
    'человек пишет elapsed от НАЧАЛА ЗАПИСИ = ts[0]' — ts[0] и семантически ближе.
    """
    ts0 = _stream_first_ts(stream)
    if ts0 is not None:
        return int(ts0), "stream_ts0"
    row = st.conn.execute(
        "SELECT summary_json, start_time FROM activities WHERE activity_id=?",
        (activity_id,)).fetchone()
    if row is not None:
        if row["summary_json"]:
            try:
                bt = json.loads(row["summary_json"]).get("beginTimestamp")
                if bt is not None:
                    return int(bt), "beginTimestamp"
            except (ValueError, TypeError):
                pass
        gm = _gmt_to_ms(row["start_time"])
        if gm is not None:
            return gm, "start_time_gmt"
    return None, "none"


def add_lactate(slug: str, activity_id: int, mmol: float,
                at_ms: Optional[int] = None,
                at_elapsed_s: Optional[float] = None,
                user_ref: Optional[str] = None,
                source: str = "llm") -> dict:
    """Внести лактатный замер к тренировке (namerenie). Возвращает {mark_id, status}.

    Три формы указания секунды (все канонизируются в at_time = wall-clock UTC мс,
    хранится ОДИН формат — §B2):
      at_ms        — сырой wall-clock UTC мс (эталонный/точный путь);
      at_elapsed_s — секунды ОТ СТАРТА ЗАПИСИ (как человек пишет: '36:30'→2190);
                     конвертится якорем старта (ts[0]→beginTimestamp→start_time);
      user_ref     — 'lapN' (структурный, без времени; конец Garmin-круга N).
    Приоритет at_ms > at_elapsed_s (оба → at_time); at_time приоритетнее user_ref.

    Валидация входа (проверка фактов, не угадывание):
      activity_id не в каталоге          → error activity not found
      at_elapsed_s задан, якоря нет      → error cannot anchor elapsed
      ни времени, ни user_ref            → error need at_time or user_ref
      user_ref не 'lapN'                 → error user_ref malformed
      user_ref='lapN', laps есть, N нет  → error lap not found (has M) (опечатка)
    Иначе пишет namerenie с validation (ok/deferred) и НЕМЕДЛЕННО резолвит из кэша
    под текущей algo_version (streams есть → resolved; нет → pending_resolve). CACHE-ONLY.
    """
    from enrich import resolve_mark, validate_mark

    with Store(profiles.resolve(slug).db_path) as st:
        if st.conn.execute("SELECT 1 FROM activities WHERE activity_id=?",
                            (activity_id,)).fetchone() is None:
            return {"error": f"activity {activity_id} not found"}

        laps = st.get_raw(activity_id, "laps")            # только кэш, без сети
        stream = st.get_raw(activity_id, "streams")       # один раз: якорь + резолв

        # канонизация времени входа → at_time (wall-clock UTC мс)
        at_time: Optional[int] = None
        if at_ms is not None:
            at_time = int(at_ms)                          # уже wall-clock
        elif at_elapsed_s is not None:
            start_ms, _src = _activity_start_ms(st, activity_id, stream)
            if start_ms is None:
                return {"error": "cannot anchor elapsed (no start in cache)"}
            at_time = int(start_ms + float(at_elapsed_s) * 1000.0)

        if at_time is None and not user_ref:
            return {"error": "need at_time or user_ref"}
        if user_ref is not None and not _LAP_REF_RE.fullmatch(str(user_ref)):
            return {"error": f"user_ref malformed: {user_ref!r} (expected 'lapN')"}

        intent = {"at_time": at_time, "user_ref": user_ref}
        validation, lap_count = validate_mark(laps, intent)
        # свежий вход: 'invalid' = опечатка (круга N доказуемо нет) → ошибка, НЕ пишем
        if validation == "invalid":
            return {"error": f"lap not found in {user_ref!r} "
                              f"(activity has {lap_count} laps)"}

        mark_id = st.add_user_lactate(activity_id, mmol, at_time=at_time,
                                      user_ref=user_ref, source=source,
                                      validation=validation, lap_count=lap_count)

        # немедленный резолв ТОЛЬКО для ok + streams в кэше, под ТЕКУЩЕЙ версией
        status = "pending_validation" if validation == "deferred" else "pending_resolve"
        av = st.meta_get("algo_version")
        binding = None
        if validation == "ok" and av is not None and stream is not None:
            binding = resolve_mark(stream, laps, intent, av)
            if binding is not None:
                st.put_user_lactate_resolved(mark_id, av, binding["lap"],
                                             binding["hr_at"], binding["pace_at"])
                status = "resolved"
        out = {"mark_id": mark_id, "status": status, "at_time": at_time}
        if status == "resolved":
            out.update({k: binding[k] for k in ("lap", "hr_at", "pace_at")})
        return out


def add_note(slug: str, activity_id: int, text: str, source: str = "llm") -> dict:
    """Внести заметку (свободный текст) к тренировке. Возвращает {mark_id}.
    id-чек обязателен (иначе осиротевшая вечная заметка на галлюцинированном id)."""
    with Store(profiles.resolve(slug).db_path) as st:
        if st.conn.execute("SELECT 1 FROM activities WHERE activity_id=?",
                            (activity_id,)).fetchone() is None:
            return {"error": f"activity {activity_id} not found"}
        mark_id = st.add_note(activity_id, text, source=source)
        return {"mark_id": mark_id}


def delete_lactate(slug: str, mark_id: int) -> dict:
    """Удалить метку/заметку по mark_id (жёстко, каскадом чистит раствор).
    Возвращает {deleted: bool}."""
    with Store(profiles.resolve(slug).db_path) as st:
        return {"deleted": st.delete_user_mark(mark_id)}


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
    elif cmd == "add-lactate":
        # python tools.py <slug> add-lactate <activity_id> <mmol>
        #   [--at-ms <wall-clock-мс> | --at-elapsed <mm:ss|сек> | --user-ref lapN]
        aid, mmol = int(sys.argv[3]), float(sys.argv[4])
        at_ms = at_elapsed_s = user_ref = None
        rest = sys.argv[5:]
        for i, a in enumerate(rest):
            if a == "--at-ms" and i + 1 < len(rest):
                at_ms = int(rest[i + 1])
            elif a == "--at-elapsed" and i + 1 < len(rest):
                v = rest[i + 1]
                if ":" in v:                      # mm:ss (как человек пишет)
                    mm, ss = v.split(":", 1)
                    at_elapsed_s = int(mm) * 60 + int(ss)
                else:
                    at_elapsed_s = float(v)       # сырые секунды
            elif a == "--user-ref" and i + 1 < len(rest):
                user_ref = rest[i + 1]
        res = add_lactate(slug, aid, mmol, at_ms=at_ms,
                          at_elapsed_s=at_elapsed_s, user_ref=user_ref)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        sys.exit(1 if "error" in res else 0)   # ошибка входа → ненулевой код (CLI-контракт)
    elif cmd == "add-note":
        # python tools.py <slug> add-note <activity_id> <text...>
        aid = int(sys.argv[3])
        text = " ".join(sys.argv[4:])
        res = add_note(slug, aid, text)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        sys.exit(1 if "error" in res else 0)
    elif cmd == "delete-lactate":
        # python tools.py <slug> delete-lactate <mark_id>
        print(json.dumps(delete_lactate(slug, int(sys.argv[3])), ensure_ascii=False, indent=2))
    else:
        print(f"неизвестная команда: {cmd}")
        sys.exit(1)
