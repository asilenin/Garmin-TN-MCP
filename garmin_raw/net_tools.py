"""net_tools.py — СЕТЕВЫЕ тулы (этап 7.6, контракт QA INV-NET-GUARD/WELL-FRESHNESS-LLM).

Модуль-владелец точек входа в сеть из чата, симметричный fetch.py: fetch владеет
СЫРОЙ сетью (сокеты/throttle/retry), net_tools — ТУЛАМИ, которые в неё ходят. Оба
явно сетевые; tools.py по контрасту — cache-only ПО ПОСТРОЕНИЮ (не импортирует
Fetcher; замок test_cache_only проверяет это статически+динамически под
forbid_network).

Здесь живут все сетевые тулы: garmin_wellness сейчас, garmin_sync/garmin_sync_estimate
позже (тот же класс — контракт SYNC-EXPLICIT-RANGE). Разделение модулей, а не «tools.py + пометка»:
пометка дрейфует бесшумно (класс ошибки декоратора), импорт Fetcher — grep-проверяемый
структурный признак.

Классификация по оси INV-NET-GUARD: garmin_wellness — сетевой READ (ходит в сеть, но пишет только
в кэш-как-след-похода, не пользовательские данные; цель вызова — вернуть данные СЕЙЧАС,
не «оставить raw навсегда»). Симметрия с garmin_sync_estimate.

ВАЖНО — модуль сетевого ДОМЕНА, НЕ «все функции ходят в сеть». Импорт Fetcher/pace =
grep-признак сетевого КЛАССА модуля, но внутри есть ТРИ рода функций:
  • сетевое ДЕЙСТВИЕ (garmin_wellness, garmin_sync_catalog, garmin_enrich_fetch) —
    реально трогают сокет, под forbid_network КРАСНЫЕ (netguard ловит);
  • сетевой ДОМЕН ЗНАНИЯ (garmin_enrich_fetch_estimate) — владеет throttle-pace
    (сетевое знание, читать legal только здесь), но сеть НЕ трогает (count из кэша ×
    pace-константа); под forbid_network ЗЕЛЁНЫЙ. Живёт здесь по домену знания, не по
    факту вызова — иначе pace пришлось бы тащить в cache-only tools.py (протечка, INV-NO-DOMAIN-LEAK).
Не добавлять функцию сюда «по аналогии, домен же тот же», не проверив, к какому роду
она относится — иначе «net_tools = сетевой» размоется в «net_tools = разное про сеть».
Тест-контраст (test_enrich_fetch): действие пробивает forbid_network, домен-знания
проходит — граница видима, не только задекларирована.
"""
from __future__ import annotations

from typing import Any, Optional

import profiles
from store import Store

# Зонды wellness — имена берём из Fetcher (единый источник, не дублируем список).
from fetch import Fetcher


def garmin_wellness(slug: str, date: str, *, refresh: bool = False) -> dict:
    """Wellness за дату: сон/HRV/RHR/стресс/BodyBattery. СЕТЕВОЙ read.

    Порядок (КРИТИЧНО для cache-only-инварианта, INV-NET-GUARD): кэш проверяется ДО создания
    Fetcher — при полном валидном кэше сеть (и ленивый login-сокет) не трогается
    вовсе. Fetcher создаётся ТОЛЬКО когда есть чего докачивать.

    Свежесть НЕ судится здесь (WELL-FRESHNESS-LLM): возвращаем данные + fetched_at + возраст даты,
    «дозрело или перекачать» решает LLM. refresh=True — принудительно перекачать все
    зонды (LLM решил, что кэш устарел), иначе качаем только отсутствующие зонды.

    Возврат: {date, requested_at_age_days, probes: {probe: {status, detail,
    payload, fetched_at, derived_fields}}}. derived_fields помечает поля Garmin-
    производные (body_battery/stress) — факт для LLM, не резка (WELL-FRESHNESS-LLM разв. C).
    """
    from datetime import date as _date

    prof = profiles.resolve(slug)
    all_probes = list(Fetcher.WELLNESS_PROBES.keys())

    # --- фаза 1: читаем кэш (БЕЗ сети) ---
    with Store(prof.db_path) as st:
        cached = st.get_wellness_date(date)

    # Какие зонды надо (до)качать: refresh → все; иначе те, чьей строки НЕТ.
    # 'empty'/'error' в кэше НЕ перекачиваем автоматически (это состоявшийся факт;
    # решение перекачать error — LLM через refresh, иначе транзиентный сбой гонял бы
    # сеть на каждом чтении). Отсутствие строки = не ходили → качаем.
    if refresh:
        to_fetch = all_probes
    else:
        to_fetch = [p for p in all_probes if p not in cached]

    # --- фаза 2: сеть ТОЛЬКО если есть что качать (иначе Fetcher не создаём) ---
    login_error: Optional[dict] = None
    blocked: set = set()
    if to_fetch:
        f = Fetcher(tokenstore=prof.tokens_dir)
        # Login — ОДИН раз до цикла зондов, отдельной фазой. Login-сбой ≠ зондовый
        # error: это отказ ВСЕГО похода (протухли токены / сеть легла при входе), не
        # свойство зонда. Разводим по ФАЗЕ, не по типу: сбой здесь → все to_fetch
        # помечаются blocked_by_auth В ВОЗВРАТЕ (не в кэше — blocked описывает этот
        # вызов, не дату; класть в кэш = кэшировать транзакционный сбой с датным
        # fetched_at, TTL-двусмысленность). Валидный кэш (cached фазы-1) отдаётся как есть.
        try:
            _ = f.client   # триггерит ленивый login
        except Exception as e:  # noqa: BLE001
            login_error = {"reason": "auth_or_connect_failed", "message": str(e)}
            blocked = set(to_fetch)   # до этих зондов не дошли — пометка в возврате
        if login_error is None:
            with Store(prof.db_path) as st:
                for probe in to_fetch:
                    try:
                        body = f.get_wellness_probe(probe, date)
                    except ValueError as e:
                        st.put_wellness_probe(date, probe, "error", detail=str(e))
                        continue
                    except Exception as e:  # noqa: BLE001
                        st.put_wellness_probe(date, probe, "error",
                                              detail=f"{type(e).__name__}: {e}")
                        continue
                    if body is None or (isinstance(body, (list, dict)) and len(body) == 0):
                        st.put_wellness_probe(date, probe, "empty")
                    else:
                        st.put_wellness_probe(date, probe, "ok", payload=body)
            # перечитываем кэш ТОЛЬКО при успешном login (были записи)
            with Store(prof.db_path) as st:
                cached = st.get_wellness_date(date)
        # при login-сбое cached остаётся из фазы-1 (кэш не менялся) — не перечитываем

    # --- фаза 3: сборка ответа (возраст даты как факт свежести, WELL-FRESHNESS-LLM) ---
    try:
        y, m, d = (int(x) for x in date.split("-"))
        age_days = (_date.today() - _date(y, m, d)).days
    except (ValueError, TypeError):
        age_days = None

    # derived-поля (Garmin-производные) — помечаем, НЕ режем (WELL-FRESHNESS-LLM разв. C).
    derived_by_probe = {
        "body_battery": ["bodyBatteryValuesArray", "charged", "drained"],
        "stress": ["overallStressLevel", "stressQualifier"],
    }

    probes_out = {}
    # зонды из кэша (состоявшийся факт — ok/empty/error с fetched_at):
    for probe, rec in cached.items():
        probes_out[probe] = {
            "status": rec["status"],
            "detail": rec["detail"],
            "payload": rec["payload"],
            "fetched_at": rec["fetched_at"],
            "derived_fields": derived_by_probe.get(probe, []),
        }
    # зонды, до которых login не дал дойти — blocked_by_auth ТОЛЬКО в возврате:
    for probe in blocked:
        probes_out[probe] = {
            "status": "blocked_by_auth",
            "detail": "login не удался — зонд не запрошен (не в кэше)",
            "payload": None,
            "fetched_at": None,
            "derived_fields": derived_by_probe.get(probe, []),
        }

    out = {
        "date": date,
        "requested_at_age_days": age_days,   # факт свежести — суждение LLM (WELL-FRESHNESS-LLM)
        "probes": probes_out,
    }
    if login_error is not None:
        out["login_error"] = login_error   # сообщение reauth; причинность — в blocked_by_auth
    return out


def garmin_sync_catalog(slug: str, start: str, end: str) -> dict:
    """Инкрементальное наполнение каталога за ЯВНЫЙ диапазон [start, end] (ISO).
    СЕТЕВОЙ WRITE (net_tools, не cache-only): цель — сходить в Garmin и дописать
    каталог, сеть трогается всегда (в отличие от wellness cache-hit).

    Диапазон ОБЯЗАТЕЛЕН, без дефолта (контракт SYNC-EXPLICIT-RANGE): скрытый дефолт «от last_sync до
    today» молча вырос бы после простоя. LLM формулирует диапазон сам — типично «от
    cache_status.garmin_range[1] (max дата каталога) до сегодня» для инкремента
    («докачай свежее»). Источник «докуда есть» — наблюдаемая max-дата каталога, НЕ
    записанный last_sync_window (наблюдаемое состояние вернее чекпойнта; last_sync_window
    — отдельная задача для CLI-архива, не для этого тула).

    Тонкая обёртка над sync_catalog(start_date, end_date) — тот же обход окон/retry/
    останов, что CLI-путь, только явные границы. Идемпотентно (upsert), resumable
    (per-window commit): повторный вызов того же диапазона дёшев (окна перекрываются).

    Возврат (факт результата для LLM, не вердикт):
      {windows, activities_upserted, range: [lo, hi], stopped_early, stop_reason,
       elapsed_s} — сколько окон обошли, сколько активностей записали, новый диапазон
      каталога после синка. Ошибка сети/login — пробрасывается (тул честно падает,
      не маскирует: в отличие от wellness, здесь нет частичного кэша для спасения).
    """
    from sync import sync_catalog

    prof = profiles.resolve(slug)  # noqa: F841  (валидация slug + единый путь резолва)
    rep = sync_catalog(slug, start_date=start, end_date=end)
    return {
        "windows": rep.windows,
        "activities_upserted": rep.activities_upserted,
        "range": [rep.range_start, rep.range_end],
        "stopped_early": rep.stopped_early,
        "stop_reason": rep.stop_reason,
        "elapsed_s": round(rep.elapsed_s, 2),
    }


def garmin_enrich_fetch(slug: str, activity_id: int) -> dict:
    """Скачать сырьё ОДНОЙ активности из сети и обогатить. СЕТЕВОЙ WRITE (точечный).

    Замыкает цепочку sync→streams→enrich: catalog-sync даёт summary, этот тул тянет
    streams+laps+watch-лактат из Garmin и обогащает. Для случая (б) TOOL-READ-NET-SPLIT (raw нет в кэше).

    ТОЧЕЧНЫЙ (один activity_id) — для малого N. Оценку объёма/времени для множества
    даёт garmin_enrich_fetch_estimate (сетевой домен); большой N дорог (N×throttle) →
    CLI enrich_batch, не N MCP-вызовов. Реши по count_missing_raw (garmin_enrich_estimate)
    + времени (garmin_enrich_fetch_estimate), НЕ вслепую.

    Watch-лактат передаётся СЫРЫМ в enrich_activity (единая точка входа — оба
    потребителя, enrich_batch и этот тул, наследуют обработку через движок, без разъезда).
    ВНИМАНИЕ: дедупликация полок watch-лактата НЕ реализована (backlog LACTATE-PLATEAU-SEG,
    ждёт реальных образцов) → при наличии замеров count_watch завышен (сырые точки полки,
    не отметки). Пока замеров нет (полки пустые, value≤0 отфильтрован) — точек нет, корректно.

    Возврат: {status, activity_id, [raw_fetched, enriched]} —
      enriched   — скачано+обогащено (streams не было, докачали, enrich прошёл);
      already    — уже обогащено текущей версией (сеть не трогали);
      not_found  — активности нет в каталоге;
      error      — сбой скачивания/обогащения (detail; login-сбой — тоже error здесь,
                   в отличие от wellness: точечный тул, частичного кэша для спасения нет).
    """
    from enrich import ALGO_VERSION, enrich_activity as _enrich_engine
    from backend import GarminSource

    prof = profiles.resolve(slug)
    with Store(prof.db_path) as st:
        # not_found: активности нет в каталоге
        if st.conn.execute("SELECT 1 FROM activities WHERE activity_id=?",
                            (activity_id,)).fetchone() is None:
            return {"status": "not_found", "activity_id": activity_id}
        # already: обогащено текущей версией — сеть не трогаем (cache-hit до Fetcher)
        if st.has_enriched(activity_id, ALGO_VERSION):
            return {"status": "already", "activity_id": activity_id}

    # сеть: Fetcher (streams/laps) + GarminSource (watch-лактат/comment)
    try:
        f = Fetcher(tokenstore=prof.tokens_dir)
        gs = GarminSource(tokenstore=str(prof.tokens_dir))
        with Store(prof.db_path) as st:
            # streams — обязательное сырьё; качаем и СРАЗУ сохраняем
            stream = st.get_raw(activity_id, "streams")
            raw_fetched = False
            if stream is None:
                stream = f.get_streams(activity_id)
                st.put_raw(activity_id, "streams", stream)
                raw_fetched = True
                if not st.has_raw(activity_id, "laps"):
                    try:
                        st.put_raw(activity_id, "laps", f.get_laps(activity_id))
                    except Exception:  # noqa: BLE001
                        pass
            # watch-лактат + comment (оба необязательны; value≤0 фильтруется в backend)
            lact = []
            try:
                lact = gs.get_activity_lactate(activity_id).get("points", [])
            except Exception:  # noqa: BLE001
                lact = []
            comment = []
            try:
                craw = gs.get_activity_comment(activity_id)
                comment = craw.get("lactate_mmol", [])
                st.put_raw(activity_id, "comment", craw)
            except Exception:  # noqa: BLE001
                comment = []
            laps = st.get_raw(activity_id, "laps")
            # sport из каталога → gps_type (согласован с ЭТИМ id)
            _sp = st.conn.execute("SELECT sport FROM activities WHERE activity_id=?",
                                  (activity_id,)).fetchone()
            # обогащение — ЕДИНАЯ точка входа: сырой watch-лактат в движок, без шортката
            enriched = _enrich_engine(
                stream, laps=laps,
                lactate_watch_points=lact, lactate_comment_values=comment,
                sport=(_sp[0] if _sp else None),
            )
            st.put_enriched(activity_id, enriched)
            st.backfill_device_model(activity_id)
        return {"status": "enriched", "activity_id": activity_id,
                "raw_fetched": raw_fetched}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "activity_id": activity_id,
                "detail": f"{type(exc).__name__}: {exc}"}


def garmin_enrich_fetch_estimate(slug: str, *, start: Optional[str] = None,
                                 end: Optional[str] = None,
                                 sport: Optional[str] = None) -> dict:
    """Оценка ВРЕМЕНИ сетевой докачки enrich (для решения N-точечных-vs-CLI).

    Живёт в net_tools по ДОМЕНУ ЗНАНИЯ (владеет throttle-pace), НЕ потому что ходит в
    сеть — НЕ ходит (count из кэша, pace — константа). Третий класс: сетевой домен,
    не сетевое действие (см. docstring модуля). Под forbid_network проходит зелёным.

    count_missing_raw — тот же единый предикат (count_enrich_pending, cache-only), что и
    garmin_enrich_estimate (TOOL-READ-NET-SPLIT). Время = count × pace_s (throttle, _best_case: без
    retry/backoff — нижняя граница, как sync-estimate SYNC-EXPLICIT-RANGE). Это ЗАКРЫВАЕТ INV-NO-DOMAIN-LEAK-дыру «время
    сетевой части — домен сетевого estimate, которого нет»: теперь есть, по адресу.

    Пара к garmin_enrich_estimate: тот даёт СКОЛЬКО (два count, cache-only), этот —
    СКОЛЬКО ВРЕМЕНИ на сетевую часть. Зови этот, если count_missing_raw > 0 и решаешь
    N-точечных-fetch vs CLI.
    """
    from store import Store as _Store
    from enrich import ALGO_VERSION
    # pace — сетевое знание, здесь legal (net_tools). fetch.py владеет им; читаем как факт
    # throttle (не создаём Fetcher, сеть не трогаем).
    from fetch import DEFAULT_PACE_S

    prof = profiles.resolve(slug)
    with _Store(prof.db_path) as st:
        counts = st.count_enrich_pending(ALGO_VERSION, start=start, end=end, sport=sport)
    missing = counts["missing_raw"]
    est_hours = missing * DEFAULT_PACE_S / 3600.0
    return {
        "count_missing_raw": missing,
        "estimated_hours_best_case": round(est_hours, 3),
        "note": "best_case: без retry/backoff (нижняя граница). Большой count → CLI "
                "enrich_batch, не N MCP-вызовов.",
    }


def garmin_sync_estimate(slug: str, start: str, end: str) -> dict:
    """Оценка объёма/времени синка каталога за диапазон [start, end] ДО тяжёлой закачки
    (контракт SYNC-EXPLICIT-RANGE). СЕТЕВОЙ read (в отличие от garmin_enrich_fetch_estimate — тот домен-
    знания из кэша, зелёный под forbid_network; ЭТОТ ходит в сеть — list_activities за
    окна — чтобы узнать, сколько Garmin отдаст за диапазон, которого каталог ещё не видел;
    КРАСНЫЙ под forbid_network).

    Идемпотентный: обходит те же окна, что garmin_sync_catalog (через sync_catalog(
    dry_run=True) — тот же login-fail-fast, retry, останов, БЕЗ мутации каталога/meta).
    Ценность — факт объёма БЕЗ мутации, не экономия сети (сетевая цена ~= синку: оба
    обходят окна; разница — запись в БД). LLM решает «делать сейчас / CLI» по факту, не
    по зашитому порогу.

    Возврат: {count, windows, estimated_hours_best_case, catalog_range}:
      count      — сколько активностей Garmin отдал за диапазон (факт объёма);
      windows    — число окон обхода (ЕДИНИЦА времени синка: throttle между окнами, НЕ
                   между активностями — время растёт с окнами, не с count);
      estimated_hours_best_case — windows × pace / 3600 (best_case: без retry/backoff,
                   нижняя граница; расхождение с реальным — ожидаемое свойство _best_case,
                   не дефект, как в sync/enrich estimate);
      catalog_range — [lo, hi] каталога КАК ЕСТЬ сейчас (dry_run не менял) — для контекста,
                   не «что будет после».
    Диапазон обязателен (SYNC-EXPLICIT-RANGE, без дефолта). count время — РАЗНЫЕ единицы: count=активности
    (объём), время=окна (стоимость обхода). Не путать (единица sync — окна, не активности,
    в отличие от enrich где единица — активность).
    """
    from sync import sync_catalog, _iter_windows, WINDOW_MONTHS
    from fetch import DEFAULT_PACE_S
    from datetime import date as _date

    # dry_run: обход окон через тот же sync_catalog (login-fix/retry/останов наследуются),
    # без мутации каталога/meta. count = сколько Garmin отдал бы.
    rep = sync_catalog(slug, start_date=start, end_date=end, dry_run=True)

    # время — по ОКНАМ (единица стоимости синка), не по count. windows берём из отчёта
    # (фактически обойдённые, с учётом раннего останова), pace — throttle между окнами.
    windows = rep.windows
    est_hours = windows * DEFAULT_PACE_S / 3600.0
    return {
        "count": rep.activities_upserted,   # под dry_run = сколько отдал бы (len rows)
        "windows": windows,
        "estimated_hours_best_case": round(est_hours, 4),
        "catalog_range": [rep.range_start, rep.range_end],
        "note": "count=активности (объём), время=windows×pace (стоимость обхода — разные "
                "единицы). best_case без retry. Большой объём → CLI, реши по факту.",
    }
