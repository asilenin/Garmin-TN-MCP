"""MCP-сервер обогащённого слоя (этап 7.5): compact/full/query/aggregates/status +
add_lactate/add_note/delete через MCP. Профиль-aware по схеме (B): ОДИН коннектор =
ОДНО подключение, slug из env TN_USER/TN_PROVIDER при старте (legacy GARMIN_TN_PROFILE) — НЕ параметр тула.

Инварианты (QA 7.5):
- I1: профиль выбирается транспортом (какой коннектор), не моделью. slug из env.
- I2: slug виден функции, невидим модели — НЕ в сигнатуре тула (схема), НЕ в возврате,
  НЕ в ошибках (нейтральность возвратов сторожит tests/test_integration.py). Имена тулов
  доменные (garmin_*), БЕЗ профиля в имени (профиль в имени = slug в поверхность модели).
- I4: cache-only — тул в сеть не ходит (закачка сырья = sync CLI).
- I5: тонкая обёртка — функции tools.py не переписываются, адаптер подставляет env-slug.

Запуск: TN_USER=<user> TN_PROVIDER=<provider> garmin-tn-mcp (stdio). Без подключения — падение при старте
(молчаливый дефолт = профиль-угадывание, запрещено I1).
"""
from __future__ import annotations

import os
import sys

# tools.py/profiles.py — ПЛОСКИЕ импорты (import profiles). Кладём garmin_raw/ на путь
# ДО импорта tools, чтобы его плоские импорты резолвились и в пакетном, и в плоском
# запуске (тот же приём, что tests/test_integration.py). Якорь — __file__, не cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP  # noqa: E402
import profiles  # noqa: E402
import tools  # noqa: E402
import net_tools  # noqa: E402  (сетевые тулы: wellness, sync_catalog)

# Имя сервера КОНСТАНТНО (не включает slug): различение профилей — по ключу коннектора
# в конфиге Claude (транспорт), не по имени сервера. Иначе slug утёк бы в идентичность,
# видимую модели.
mcp = FastMCP("garmin-tn")

_SLUG: str | None = None  # ставится в main() из env; тулы читают через _slug()


def _slug() -> str:
    if _SLUG is None:
        # защитно: тул вызван до инициализации (не должно случаться — mcp.run() в main
        # после установки). Не подставляем дефолт (I1: угадывание запрещено).
        raise RuntimeError("подключение не инициализировано (TN_USER/TN_PROVIDER)")
    return _SLUG


# ── read ────────────────────────────────────────────────────────────────────
@mcp.tool()
def garmin_status() -> dict:
    """Состояние кэша профиля (schema/версия/счётчики/диапазон/last_sync). Без сети."""
    return tools.cache_status(_slug())


@mcp.tool()
def garmin_query(limit: int = 50, order: str = "date_desc",
                 filters: dict | None = None) -> dict:
    """Каталог тренировок по фильтру (БЕЗ гистограмм). filters — объект предикатов,
    напр. {"date_from":"2026-01-01","sport":"running","max_hr_min":180}."""
    return tools.query_index(_slug(), limit=limit, order=order, **(filters or {}))


@mcp.tool()
def garmin_compact(activity_id: int) -> dict:
    """Обзор тренировки: формы гистограмм, кластеры, дисперсии, лактат-метки (watch+
    user), биомеханика. Средний уровень — без посекундного потока."""
    return tools.get_activity_compact(_slug(), activity_id)


@mcp.tool()
def garmin_full(activity_id: int) -> dict:
    """Полное обогащение тренировки (все derived-поля). Тяжелее compact."""
    return tools.get_activity_full(_slug(), activity_id)


@mcp.tool()
def garmin_aggregates(period_key: str | None = None) -> dict:
    """Кросс-агрегаты по периодам (динамика формы). Без period_key — все периоды;
    с ним (напр. '2026-Q2') — один."""
    return tools.get_period_aggregates(_slug(), period_key)


# ── write ───────────────────────────────────────────────────────────────────
@mcp.tool()
def garmin_add_lactate(activity_id: int, mmol: float,
                       at_ms: int | None = None,
                       at_elapsed_s: float | None = None,
                       user_ref: str | None = None) -> dict:
    """Внести лактатный замер к тренировке. Укажи РОВНО ОДНУ форму секунды замера:
      at_elapsed_s — секунды от старта записи (как в комментарии '36:30' → 2190);
      at_ms        — абсолютный wall-clock UTC мс (точный/сверочный путь);
      user_ref     — 'lapN' (конец Garmin-круга N).
    Привязка (hr/темп секунды) резолвится немедленно из кэша; нет streams → pending.
    """
    n = sum(x is not None for x in (at_ms, at_elapsed_s, user_ref))
    if n == 0:
        return {"error": "need one of at_ms / at_elapsed_s / user_ref"}
    if n >= 2:
        # НЕ прячем конфликт приоритетом — вскрываем: две формы = путаница цели.
        return {"error": "specify exactly one of at_ms / at_elapsed_s / user_ref"}
    return tools.add_lactate(_slug(), activity_id, mmol, at_ms=at_ms,
                             at_elapsed_s=at_elapsed_s, user_ref=user_ref)


@mcp.tool()
def garmin_add_note(activity_id: int, text: str) -> dict:
    """Внести свободнотекстовую заметку к тренировке (контекст для анализа)."""
    return tools.add_note(_slug(), activity_id, text)


@mcp.tool()
def garmin_delete_mark(mark_id: int) -> dict:
    """Удалить метку/заметку по mark_id (жёстко, каскадом чистит привязку)."""
    return tools.delete_lactate(_slug(), mark_id)


# ── этап 7.6: wellness / enrich / sync ───────────────────────────────────────
@mcp.tool()
def garmin_wellness(date: str, refresh: bool = False) -> dict:
    """Wellness за дату (сон/HRV/RHR/стресс/BodyBattery). СЕТЕВОЙ read: при валидном
    кэше сеть не трогается. date — 'YYYY-MM-DD'. refresh=true форсит перекачку всех
    зондов. Свежесть суди сам по fetched_at и requested_at_age_days (порога в коде нет)."""
    return net_tools.garmin_wellness(_slug(), date, refresh=refresh)


@mcp.tool()
def garmin_enrich_activity(activity_id: int) -> dict:
    """Обогатить ОДНУ активность из уже скачанного сырья (streams в кэше). CACHE-ONLY,
    без сети. status: enriched/already/no_raw/not_found/error. no_raw = streams нет в
    кэше (нужен сетевой путь/sync), НЕ ошибка тула."""
    return tools.enrich_activity(_slug(), activity_id)


@mcp.tool()
def garmin_enrich_estimate(start: str | None = None, end: str | None = None,
                           sport: str | None = None) -> dict:
    """Оценка объёма недостающего обогащения (CACHE-ONLY). Два count:
    count_has_raw_no_enrich (cache-only точечный enrich) + count_missing_raw (нужен
    сетевой fetch). БЕЗ времени: cache-only часть — количество единиц (реши точечно/
    терминал по числу), сетевая — оценивается сетевым estimate (пока не реализован)."""
    return tools.enrich_estimate(_slug(), start=start, end=end, sport=sport)


@mcp.tool()
def garmin_sync_catalog(start: str, end: str) -> dict:
    """Инкрементально дописать каталог за ЯВНЫЙ диапазон [start, end] (ISO). СЕТЕВОЙ
    write. Диапазон обязателен: для «докачай свежее» возьми garmin_status.garmin_range[1]
    (max дата каталога) как start, сегодня как end. Возврат: сколько окон/активностей
    записано, новый диапазон каталога. Большой архив — НЕ через этот тул (терминал)."""
    return net_tools.garmin_sync_catalog(_slug(), start, end)


@mcp.tool()
def garmin_sync_estimate(start: str, end: str) -> dict:
    """Оценка объёма/времени синка каталога за [start, end] ДО закачки. СЕТЕВОЙ read
    (ходит в Garmin list_activities — сколько активностей за диапазон; НЕ мутирует
    каталог). Возврат: count (активностей отдаст), windows, estimated_hours_best_case
    (по окнам, не по count), catalog_range. Зови перед garmin_sync_catalog, если не
    уверен в объёме; реши «сейчас/CLI» по факту. Диапазон обязателен."""
    return net_tools.garmin_sync_estimate(_slug(), start, end)


@mcp.tool()
def garmin_enrich_fetch(activity_id: int) -> dict:
    """Скачать сырьё ОДНОЙ активности (streams+laps+watch-лактат) из Garmin и обогатить.
    СЕТЕВОЙ write, точечный. Замыкает цепочку sync→streams→enrich: для активностей, что
    в каталоге есть, но без streams (garmin_enrich_activity вернул no_raw). Для малого N;
    большой объём — CLI enrich_batch. Оцени объём: garmin_enrich_estimate (count) +
    garmin_enrich_fetch_estimate (время). status: enriched/already/not_found/error."""
    return net_tools.garmin_enrich_fetch(_slug(), activity_id)


@mcp.tool()
def garmin_enrich_fetch_estimate(start: str | None = None, end: str | None = None,
                                 sport: str | None = None) -> dict:
    """Оценка ВРЕМЕНИ сетевой докачки enrich (count_missing_raw × throttle-pace,
    best_case без retry). Пара к garmin_enrich_estimate: тот — СКОЛЬКО (count), этот —
    СКОЛЬКО ВРЕМЕНИ на сетевую часть. Зови, если count_missing_raw > 0 и решаешь
    N-точечных-fetch vs CLI. Сеть НЕ трогает (оценка из кэша)."""
    return net_tools.garmin_enrich_fetch_estimate(_slug(), start=start, end=end, sport=sport)


def main() -> None:
    global _SLUG
    try:
        _SLUG = profiles.current_slug()   # TN_USER/TN_PROVIDER → <provider>-<user> (+legacy)
    except RuntimeError as e:
        raise SystemExit(str(e))
    mcp.run()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        # проверка обёрток БЕЗ stdio: инициализируем профиль синтетикой, дёргаем тулы.
        import tempfile, json
        import numpy as np
        tmp = tempfile.mkdtemp()
        os.environ["GARMIN_TN_HOME"] = tmp
        import profiles
        from pathlib import Path
        profiles.ROOT = Path(tmp)          # ← в temp ДО resolve (reload не перечитывает ROOT
        profiles.REGISTRY = Path(tmp) / "profiles.json"  #   → иначе сорит в реальный ~/.garmin-tn)
        from store import Store
        SLUG = "testp"; AV = "enrich-0.6.0"; base = 1_700_000_000_000
        profiles.resolve(SLUG).ensure_dirs()
        with Store(profiles.resolve(SLUG).db_path) as st:
            st.conn.execute("INSERT INTO activities(activity_id,date,sport) VALUES(?,?,?)",
                            (111, "2026-06-27", "running"))
            n = 300
            ts = base + np.arange(n) * 1000.0
            st.put_raw(111, "streams", {
                "metricDescriptors": [{"key": "directTimestamp", "metricsIndex": 0},
                                      {"key": "directSpeed", "metricsIndex": 1},
                                      {"key": "directHeartRate", "metricsIndex": 2}],
                "activityDetailMetrics": [{"metrics": [ts[k], 3.0, 150.0 + k * 0.1]}
                                          for k in range(n)]})
            st.meta_set("algo_version", AV); st.conn.commit()

        _SLUG = SLUG  # эмуляция main()-инициализации
        globals()["_SLUG"] = SLUG

        # 1) slug НЕ в схеме тулов (I2.1): у обёрток нет параметра slug
        import inspect
        for fn in (garmin_compact, garmin_full, garmin_query, garmin_aggregates,
                   garmin_status, garmin_add_lactate, garmin_add_note, garmin_delete_mark,
                   garmin_wellness, garmin_enrich_activity, garmin_enrich_estimate,
                   garmin_sync_catalog, garmin_sync_estimate, garmin_enrich_fetch,
                   garmin_enrich_fetch_estimate):
            assert "slug" not in inspect.signature(fn).parameters, f"slug в схеме {fn.__name__}"
        print("1 slug НЕ в сигнатуре тулов ✓ (вкл. 7.6: wellness/enrich/sync)")

        # 2) read-тулы работают под env-профилем
        assert garmin_status()["schema_version"] == 6
        assert garmin_compact(111)["activity_id"] == 111
        assert "enriched" in garmin_full(111)
        assert isinstance(garmin_query(limit=5), dict)
        # cache-only 7.6 (без сети): enrich_estimate/enrich_activity зовём; wellness/
        # sync_catalog СЕТЕВЫЕ — в self-test не вызываем (нужны токены), только slug-страж выше.
        assert isinstance(garmin_enrich_estimate(), dict)
        assert garmin_enrich_activity(999999)["status"] == "not_found"
        print("2 read-тулы + cache-only 7.6 OK")

        # 3) add_lactate: РОВНО одна форма — ноль/две → явная ошибка, одна → работает
        assert "error" in garmin_add_lactate(111, 5.0)                          # ноль
        assert "need one" in garmin_add_lactate(111, 5.0)["error"]
        assert "exactly one" in garmin_add_lactate(111, 5.0, at_ms=base, user_ref="lap1")["error"]  # две
        assert "exactly one" in garmin_add_lactate(111, 5.0, at_ms=base, at_elapsed_s=60)["error"]  # две
        r = garmin_add_lactate(111, 5.0, at_ms=base + 60_000)                   # одна
        assert r["status"] == "resolved", r
        print("3 add_lactate ровно-одна-форма OK")

        # 4) write/delete под профилем
        nid = garmin_add_note(111, "заметка")["mark_id"]
        assert garmin_delete_mark(nid)["deleted"] is True
        print("4 add_note/delete OK")

        # 5) падение без профиля (main без env)
        globals()["_SLUG"] = None
        try:
            garmin_status(); assert False, "должно упасть без профиля"
        except RuntimeError:
            pass
        for _k in ("TN_USER", "TN_PROVIDER", "GARMIN_TN_PROFILE"):
            os.environ.pop(_k, None)
        try:
            main(); assert False, "main без env должен SystemExit"
        except SystemExit:
            pass
        print("5 падение без env подключения ✓")
        print("server_enriched self-test OK")
        sys.exit(0)
    main()
