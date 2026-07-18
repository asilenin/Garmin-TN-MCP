"""sync.py — драйвер синхронизации (этап 3, §14).

Сейчас наполняет ТОЛЬКО каталог (сводки всех тренировок) из list_activities →
store. Сырьё/laps/streams/обогащение — следующие этапы.

Решения (из обсуждения):
  - Каталог тянется ОКНАМИ по 3 месяца (Garmin нестабилен под нагрузкой —
    полевой опыт пользователя), а не одним широким запросом.
  - Чекпойнт ПОСЛЕ каждого окна: упавшее окно при перезапуске докачивается,
    предыдущие уже в БД (upsert идемпотентен).
  - Двухуровневый retry:
      * fetch.py — короткий backoff на мелкие сбои (~1 мин);
      * здесь — ОКОННЫЙ retry: если окно упало, растущее ожидание
        (60→120→180...) с повтором окна, пока СУММАРНОЕ ожидание от первого
        отказа по окну не достигнет WINDOW_RETRY_BUDGET_S (6 минут). Достигло —
        чистая остановка: всё до этого окна сохранено, перезапуск продолжит.

Идемпотентность: повторный sync не дублирует (upsert по activity_id) и не качает
лишнего (окна уже в БД перезапишутся теми же данными — дёшево; на этапах сырья
появится пропуск по has_raw).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import profiles
from fetch import Fetcher, RateLimited
from store import Store, activity_row_from_summary

# Горизонт истории: нижняя планка одного «прохода» (§12.1). Аккаунты старше —
# поднять. 20 лет с запасом покрывает любой бытовой архив Garmin.
HISTORY_YEARS = 20
# Размер окна каталога.
WINDOW_MONTHS = 3
# Останов обхода: сколько ПУСТЫХ окон подряд считать концом архива.
# 8 кварталов = 2 года непрерывной пустоты — у бегущего человека не бывает;
# а перерыв на сезон-два (травма/зима) НЕ оборвёт обход раньше времени.
EMPTY_WINDOWS_STOP = 8
# Печать прогресса обогащения каждые N тренировок (видно, что идёт, не «висит»).
PROGRESS_EVERY = 25
# Оконный retry: суммарный бюджет ожидания от первого отказа по окну.
WINDOW_RETRY_BUDGET_S = 6 * 60          # 6 минут — порог «остановиться и подумать»
WINDOW_RETRY_STEPS_S = (60, 120, 180)   # растущее ожидание; дальше — последний шаг


@dataclass
class SyncReport:
    windows: int = 0
    activities_upserted: int = 0
    stopped_early: bool = False
    stop_reason: Optional[str] = None
    range_start: Optional[str] = None
    range_end: Optional[str] = None
    elapsed_s: float = 0.0
    window_log: list[str] = field(default_factory=list)


def _iter_windows(start: date, end: date, months: int):
    """Окна [w_start, w_end] по `months`, от СВЕЖИХ к старым.

    Свежие первыми: позволяет остановить обход по серии пустых окон, не доходя
    до пустых лет до начала архива (§ останов EMPTY_WINDOWS_STOP). Сначала
    нарезаем по возрастанию, потом отдаём в обратном порядке — чтобы границы
    месяцев считались чисто."""
    forward = []
    cur = start
    while cur <= end:
        y, m = cur.year, cur.month + months
        while m > 12:
            m -= 12
            y += 1
        w_end = min(date(y, m, 1) - timedelta(days=1), end)
        forward.append((cur, w_end))
        cur = w_end + timedelta(days=1)
    return list(reversed(forward))


def _fetch_window_with_retry(
    fetcher: Fetcher, w_start: str, w_end: str, log: list[str]
) -> list[dict]:
    """Тянет одно окно. Оконный retry с растущим ожиданием до бюджета 6 минут.

    Возвращает список сводок. Бросает TimeoutError, если бюджет исчерпан —
    драйвер ловит и останавливается чисто (чекпойнт предыдущих окон уже в БД).
    """
    waited = 0.0
    attempt = 0
    while True:
        try:
            # sport="" — ВСЕ типы (CROSS-TRAINING): не-беговое (силовые/вело/плавание)
            # нужно как контекст нагрузки. Раньше дефолт "running" фильтровал на входе
            # Garmin → не-беговое не попадало в каталог (недобор: mila 1648 из 2558).
            # Не-тренировки (stop_watch/incident) отсеиваются позже через is_trackable.
            return fetcher.list_activities(w_start, w_end, "")
        except (RateLimited, Exception) as exc:  # noqa: BLE001
            # SYNC-RETRY-AUTH: login-сбой (протухли токены между окнами) НЕ ретраим —
            # не транзиентный. Признак — текст _connect (fetch.py:88). Пробрасываем сразу.
            if "Не удалось войти" in str(exc):
                raise RuntimeError(f"окно {w_start}..{w_end}: login-сбой в обходе "
                                   f"(токены протухли?), не ретраим. {exc}") from exc
            # fetch.py уже отработал короткий backoff на мелочь и сдался —
            # значит это «Garmin прилёг». Ждём по-крупному.
            step = WINDOW_RETRY_STEPS_S[min(attempt, len(WINDOW_RETRY_STEPS_S) - 1)]
            if waited + step > WINDOW_RETRY_BUDGET_S:
                raise TimeoutError(
                    f"окно {w_start}..{w_end}: суммарное ожидание превысило "
                    f"{WINDOW_RETRY_BUDGET_S//60} мин (последняя ошибка: {exc})"
                ) from exc
            log.append(
                f"  окно {w_start}..{w_end}: отказ ({exc}); ждём {step}с "
                f"(всего ожидания {int(waited)}с)"
            )
            time.sleep(step)
            waited += step
            attempt += 1


def sync_catalog(slug: str, *, history_years: int = HISTORY_YEARS,
                 start_date: Optional[str] = None,
                 end_date: Optional[str] = None,
                 dry_run: bool = False) -> SyncReport:
    """Наполняет каталог профиля окнами по 3 месяца. Идемпотентно, resumable.

    Границы — ДВА взаимоисключающих пути:
      • history_years (дефолт) — CLI-архивный путь: [1 янв (today.year−N), today].
        Старый код, продиагностирован; при явных датах НЕ используется.
      • start_date/end_date (ISO 'YYYY-MM-DD') — явный диапазон для MCP-инкремента
        (garmin_sync_catalog): «докачай от X до Y». Обязателен обязательным диапазоном
        по контракту SYNC-EXPLICIT-RANGE (без скрытого дефолта-в-сеть). Оба или ни одного.
    Обход окон (_iter_windows), retry, останов по пустым — ОБЩИЕ для обоих путей,
    не переписаны: меняется ТОЛЬКО вычисление границ.

    dry_run — ПРИВАТНЫЙ флаг для garmin_sync_estimate (обходит окна через list_activities,
    считает объём, но НЕ мутирует состояние). Пропускает ВСЮ запись: upsert И все meta_set
    (last_sync/last_sync_window/range/policy) — иначе estimate молча продвинул бы чекпойнт
    синка, хотя каталог не тронут (тихий рассинхрон meta↔каталог). count под dry_run =
    len(rows) (сколько Garmin отдал бы), не результат upsert.
      КРИТЕРИЙ безопасности флага (в отличие от TOOL-READ-NET-SPLIT-режима, отвергнутого): dry_run НЕ меняет
      классификацию ПУБЛИЧНОГО имени — sync_catalog остаётся write, sync_estimate остаётся
      read, каждое имя классифицировано однозначно на поверхности; dry_run невидим снаружи,
      netguard-класс обёрток от него не зависит. TOOL-READ-NET-SPLIT-флаг был опасен именно тем, что менял
      класс ОДНОГО публичного имени (сеть/не-сеть плавала). Флаг безопасен как приватная
      деталь, вызываемая из двух публичных обёрток с фиксированной семантикой; опасен когда
      создаёт двусмысленность классификации на публичной поверхности."""
    prof = profiles.resolve(slug)
    t0 = time.time()
    rep = SyncReport()

    if start_date is not None or end_date is not None:
        # явный диапазон (MCP-инкремент): оба обязательны вместе
        if start_date is None or end_date is None:
            raise ValueError("start_date и end_date задаются только вместе")
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if start > end:
            raise ValueError(f"start_date {start_date} позже end_date {end_date}")
    else:
        # CLI-архивный путь (старая формула, нетронута)
        end = date.today()
        start = date(end.year - history_years, 1, 1)

    fetcher = Fetcher(tokenstore=prof.tokens_dir)
    # SYNC-RETRY-AUTH: login ЯВНО до цикла окон (симметрично wellness login-фазе).
    # login-сбой (протухшие токены) — НЕ транзиентный: ретраить бессмысленно (токены
    # не «войдут обратно» за паузу). Раньше он тонул в per-window retry → 6-мин
    # зависание. Теперь падает СРАЗУ, вне retry. Успешный login кэшируется
    # (fetcher.client), повторный доступ в окнах не логинится снова — не лишний вызов.
    try:
        _ = fetcher.client   # ленивый login происходит здесь; сбой → RuntimeError наружу
    except RuntimeError as exc:
        # быстрый auth-отказ вместо зависания: не ретраим login
        raise RuntimeError(
            f"sync прерван: login не удался (токены?). {exc}"
        ) from exc
    empty_streak = 0
    with Store(prof.db_path) as st:
        for w_start, w_end in _iter_windows(start, end, WINDOW_MONTHS):
            ws, we = w_start.isoformat(), w_end.isoformat()
            try:
                acts = _fetch_window_with_retry(fetcher, ws, we, rep.window_log)
            except TimeoutError as exc:
                rep.stopped_early = True
                rep.stop_reason = str(exc)
                break

            # is_trackable отсеивает НЕ-тренировки (stop_watch/incident_detected) —
            # служебные события Garmin, не summary тренировки. Неизвестный typeKey
            # проходит (может быть новой тренировкой, не теряем).
            from sport_taxonomy import is_trackable
            rows = [activity_row_from_summary(a) for a in acts
                    if a and is_trackable((a.get("activityType") or {}).get("typeKey"))]
            # count объёма: под dry_run — сколько Garmin ОТДАЛ БЫ (len), не результат upsert
            if dry_run:
                n = len(rows)
            else:
                n = st.upsert_activities(rows) if rows else 0
            rep.windows += 1
            rep.activities_upserted += n
            rep.window_log.append(f"  окно {ws}..{we}: +{n}")
            # чекпойнт прогресса — ТОЛЬКО при реальном синке (dry_run не продвигает meta,
            # иначе тихий рассинхрон: meta говорит «синкали», каталог не тронут)
            if not dry_run:
                st.meta_set("last_sync", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                st.meta_set("last_sync_window", we)

            # останов по серии пустых окон = достигли начала архива (см. EMPTY_WINDOWS_STOP).
            # Длинная пауза в тренировках (сезон-два) НЕ оборвёт: порог = 2 года пустоты.
            if n == 0:
                empty_streak += 1
                if empty_streak >= EMPTY_WINDOWS_STOP:
                    rep.window_log.append(
                        f"  {EMPTY_WINDOWS_STOP} пустых окон подряд — конец архива, останов обхода"
                    )
                    break
            else:
                empty_streak = 0

        lo, hi = st.activity_date_range()   # read — для отчёта (range каталога КАК ЕСТЬ)
        rep.range_start, rep.range_end = lo, hi
        if not dry_run:
            st.meta_set("garmin_range_start", lo or "")
            st.meta_set("garmin_range_end", hi or "")
            if st.meta_get("download_policy") is None:
                st.meta_set("download_policy", "all")

    rep.elapsed_s = time.time() - t0
    return rep


def cache_status(slug: str) -> dict:
    """Тул cache_status: что в кэше профиля (§8). Без сети — только чтение БД."""
    prof = profiles.resolve(slug)
    with Store(prof.db_path) as st:
        return st.status()


# --------------------------------------------------------------------------- #
# Обогащение: streams → raw (сразу) → enrich (битые НЕ пишем, копим аномалии).
# Решение по сохранности: сырьё пишем СРАЗУ после скачивания (невосстановимо
# дёшево); упавшее обогащение не пишем (чиним enrich → пересчёт из ЛОКАЛЬНОГО
# сырья, без сети). Лимит батча — идём кусками (≤200), ловим аномалии порциями.
# --------------------------------------------------------------------------- #
@dataclass
class EnrichReport:
    requested: int = 0
    raw_fetched: int = 0
    enriched_ok: int = 0
    skipped_done: int = 0
    anomalies: list[dict] = field(default_factory=list)
    elapsed_s: float = 0.0


def enrich_batch(slug: str, *, limit: int = 200, start: Optional[str] = None,
                 end: Optional[str] = None, force: bool = False,
                 offline: bool = False) -> EnrichReport:
    """Обогащает порцию тренировок (свежие первыми). Идемпотентно:
    пропускает уже обогащённые текущей ALGO_VERSION (если не force).

    Сырьё (streams) кэшируется в activity_raw при первом скачивании и больше не
    тянется (lazy-fill через has_raw). Пересчёт после правки enrich берёт сырьё
    с диска — БЕЗ сети.

    offline=True — ПЕРЕСЧЁТ только из БД: не создаёт сетевых клиентов, не качает.
    Для массового пересчёта при смене ALGO_VERSION (сырьё уже всё в кэше).

    ИЗВЕСТНОЕ ПОВЕДЕНИЕ (не баг): offline до первого online-прохода с сохранением
    comment-сырья НЕ восстанавливает лактат из комментария — текста комментария нет
    в кэше (он кэшируется только online-веткой через put_raw(aid,'comment')). Это
    отсутствие источника, не ошибка. Лактат с часов читается из streams (они в БД),
    но offline-ветка его сейчас не достаёт — добавится при появлении реальных данных.
    """
    from enrich import ALGO_VERSION, enrich_activity  # локальный импорт: numpy тяжёлый

    prof = profiles.resolve(slug)
    t0 = time.time()
    rep = EnrichReport()
    fetcher = gs = None
    if not offline:
        from backend import GarminSource
        fetcher = Fetcher(tokenstore=prof.tokens_dir)
        gs = GarminSource(tokenstore=str(prof.tokens_dir))

    with Store(prof.db_path) as st:
        # берём с запасом и фильтруем уже обогащённые — так limit означает
        # «столько НЕОБРАБОТАННЫХ за раз», и команду можно звать подряд без счёта.
        from enrich import ALGO_VERSION as _AV
        all_ids = st.activity_ids(start=start, end=end, order_desc=True)
        if force:
            ids = all_ids[:limit]
        else:
            ids = [a for a in all_ids if not st.has_enriched(a, _AV)][:limit]
        rep.requested = len(ids)
        done = 0
        for aid in ids:
            done += 1
            if not force and st.has_enriched(aid, ALGO_VERSION):
                rep.skipped_done += 1
                continue
            try:
                # сырьё: из кэша или качаем и СРАЗУ сохраняем
                stream = st.get_raw(aid, "streams")
                if stream is None:
                    if offline:
                        # пересчёт без сети: нет сырья — пропускаем (докачать обычным прогоном)
                        rep.anomalies.append({"activity_id": aid, "error": "offline: нет сырья в БД"})
                        continue
                    stream = fetcher.get_streams(aid)
                    st.put_raw(aid, "streams", stream)   # сохраняем НЕМЕДЛЕННО
                    rep.raw_fetched += 1
                    # laps — несущее сырьё классификации (структура кругов), тоже кэшируем
                    if not st.has_raw(aid, "laps"):
                        try:
                            st.put_raw(aid, "laps", fetcher.get_laps(aid))
                        except Exception:  # noqa: BLE001
                            pass
                # laps (структура кругов): нужно hr_recovery И привязку lap watch-лактата
                # (offline). Один раз из БД (online докачан выше при первом streams).
                laps = st.get_raw(aid, "laps")
                # лактат двумя дорогами (обе необязательны)
                lact, comment = [], []
                if not offline:
                    try:
                        lact = gs.get_activity_lactate(aid).get("points", [])
                    except Exception:  # noqa: BLE001
                        lact = []
                    try:
                        craw = gs.get_activity_comment(aid)
                        comment = craw.get("lactate_mmol", [])
                        # сохраняем comment-сырьё в БД → offline-пересчёт найдёт лактат
                        st.put_raw(aid, "comment", craw)
                    except Exception:  # noqa: BLE001
                        comment = []
                else:
                    # offline: watch-лактат из КЭШИРОВАННОГО потока (get_activity_details
                    # уже в БД как 'streams') — recompute БЕЗ сети и БЕЗ потери from_watch;
                    # comment-лактат из сохранённого comment-сырья, если оно есть в БД
                    from enrich import lactate_from_stream
                    lact = lactate_from_stream(stream, laps)
                    craw = st.get_raw(aid, "comment")
                    if isinstance(craw, dict):
                        comment = craw.get("lactate_mmol", []) or []
                # sport из каталога → gps_type (согласован с ЭТИМ aid)
                _sp = st.conn.execute("SELECT sport FROM activities WHERE activity_id=?",
                                      (aid,)).fetchone()
                # обогащение
                enriched = enrich_activity(
                    stream, laps=laps,
                    lactate_watch_points=lact, lactate_comment_values=comment,
                    sport=(_sp[0] if _sp else None),
                )
                st.put_enriched(aid, enriched)
                # device_model из сохранённого summary_json (deviceId — факт железа).
                # Заполняется здесь, т.к. существующий каталог мог быть создан со
                # старым None; читаем из УЖЕ сохранённого summary, без сети (§5.4:
                # факт группировки, НЕ граница источника — та по hr_source).
                st.backfill_device_model(aid)
                rep.enriched_ok += 1
            except Exception as exc:  # noqa: BLE001
                # аномалия: обогащение НЕ пишем (сырьё, если скачалось, уже в БД).
                # Чиним enrich → пересчёт из локального сырья.
                rep.anomalies.append({"activity_id": aid, "error": repr(exc)})
            # прогресс на экран каждые PROGRESS_EVERY (видно, что идёт, не «висит»)
            if done % PROGRESS_EVERY == 0 or done == rep.requested:
                el = time.time() - t0
                print(f"  [{done}/{rep.requested}] обогащено {rep.enriched_ok}, "
                      f"скачано {rep.raw_fetched}, пропущено {rep.skipped_done}, "
                      f"аномалий {len(rep.anomalies)} ({el:.0f}c)", flush=True)

    # явно закрываем сетевые клиенты — иначе keep-alive сессии держат процесс
    # живым после возврата (печать сводки прошла, а приглашение не возвращалось).
    if fetcher is not None:
        _close_quiet(fetcher)
    if gs is not None:
        _close_quiet(gs)

    # фиксируем версию формул в meta (status честно покажет, чем обогащён кэш)
    with Store(prof.db_path) as st:
        st.meta_set("algo_version", ALGO_VERSION)

    rep.elapsed_s = time.time() - t0
    return rep


def fetch_aux(slug: str, *, limit: int = 10_000) -> dict:
    """Дотягивает НЕДОСТАЮЩЕЕ лёгкое сырьё (laps, comment) ко всем тренировкам,
    у которых оно ещё не в кэше. Разовый проход для архива, скачанного до того,
    как laps/comment стали кэшироваться. Пропускает уже имеющиеся (идемпотентно).

    laps — несущее сырьё классификации (структура кругов). comment — текст notes
    (для лактата из 'LA:'). Оба лёгкие; кэшируем целиком (не лениво — §инвариант
    «сырьё локально навсегда», лень только для тяжёлых streams).
    """
    prof = profiles.resolve(slug)
    fetcher = Fetcher(tokenstore=prof.tokens_dir)
    from backend import GarminSource
    gs = GarminSource(tokenstore=str(prof.tokens_dir))
    t0 = time.time()
    laps_fetched = comment_fetched = errors = 0

    with Store(prof.db_path) as st:
        ids = st.activity_ids(limit=limit, order_desc=True)
        total = len(ids)
        for n, aid in enumerate(ids, 1):
            if not st.has_raw(aid, "laps"):
                try:
                    st.put_raw(aid, "laps", fetcher.get_laps(aid))
                    laps_fetched += 1
                except Exception:  # noqa: BLE001
                    errors += 1
            if not st.has_raw(aid, "comment"):
                try:
                    st.put_raw(aid, "comment", gs.get_activity_comment(aid))
                    comment_fetched += 1
                except Exception:  # noqa: BLE001
                    errors += 1
            if n % 25 == 0 or n == total:
                print(f"  [{n}/{total}] laps +{laps_fetched}, comment +{comment_fetched}, "
                      f"ошибок {errors} ({time.time()-t0:.0f}c)", flush=True)

    _close_quiet(fetcher)
    _close_quiet(gs)
    return {"laps_fetched": laps_fetched, "comment_fetched": comment_fetched,
            "errors": errors, "elapsed_s": round(time.time() - t0, 1)}


def recompute_user_marks(slug: str) -> dict:
    """Пересчёт user-меток (этап 7, §3.6). ОТДЕЛЬНЫЙ проход по активностям-с-метками
    (их горстка: DISTINCT activity_id FROM user_data) — НЕ внутри enrich_batch, т.к.
    тот пропускает уже обогащённые, а метку могли добавить к обогащённой активности.

    ИНВАРИАНТ (QA этап7): validation ПЕРЕД резолвом, согласованно.
      validation = f(текущие laps), БЕЗверсионно (set_validation):
        at_time → ok; user_ref+laps есть+круг N есть → ok; +N нет → invalid; laps нет → deferred.
      затем по вердикту (раствор ×algo_version, из streams):
        ok      → resolve_mark из кэш-streams; binding → put_resolved; None → снять раствор (pending_resolve);
        invalid → снять раствор (invalid НЕ несёт привязку) — точечно;
        deferred→ снять раствор, ждать laps.
    Так «invalid с привязкой» невозможно, а deferred→invalid/ok состоится при дозакачке
    laps (зовётся после fetch_aux) или смене версии (после recompute). CACHE-ONLY.
    """
    from enrich import resolve_mark, validate_mark

    prof = profiles.resolve(slug)
    changed = {"ok_resolved": 0, "pending_resolve": 0, "invalid": 0, "deferred": 0}
    with Store(prof.db_path) as st:
        av = st.meta_get("algo_version")
        aids = [r[0] for r in st.conn.execute(
            "SELECT DISTINCT activity_id FROM user_data").fetchall()]
        for aid in aids:
            laps = st.get_raw(aid, "laps")          # только кэш
            stream = st.get_raw(aid, "streams")     # только кэш
            for mk in st.get_user_data(aid):
                if mk["kind"] != "lactate":
                    continue
                intent = {"at_time": mk["at_time"], "user_ref": mk["user_ref"]}
                validation, lap_count = validate_mark(laps, intent)
                st.set_validation(mk["mark_id"], validation, lap_count)  # versionless
                if av is None:
                    continue   # версии нет — раствором управлять нечем (обогащение впереди)
                if validation == "ok":
                    binding = resolve_mark(stream, laps, intent, av) if stream is not None else None
                    if binding is not None:
                        st.put_user_lactate_resolved(mk["mark_id"], av, binding["lap"],
                                                     binding["hr_at"], binding["pace_at"])
                        changed["ok_resolved"] += 1
                    else:
                        st.delete_user_lactate_resolved(mk["mark_id"], av)  # → pending_resolve
                        changed["pending_resolve"] += 1
                else:
                    # invalid/deferred НЕ несут привязку → точечный снос (не трогая валидные)
                    st.delete_user_lactate_resolved(mk["mark_id"], av)
                    changed[validation] += 1
    return {"activities": len(aids), **changed}


def _close_quiet(obj) -> None:
    """Закрывает HTTP-сессию клиента garminconnect, если она есть. Без шума."""
    for attr in ("_client", "client", "garth", "session"):
        c = getattr(obj, attr, None)
        if c is None:
            continue
        for sub in ("session", "sess"):
            s = getattr(c, sub, None)
            close = getattr(s, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("usage: python sync.py <slug> [status]")
        sys.exit(1)
    slug = sys.argv[1]
    if len(sys.argv) > 2 and sys.argv[2] == "status":
        print(json.dumps(cache_status(slug), ensure_ascii=False, indent=2))
        sys.exit(0)

    if len(sys.argv) > 2 and sys.argv[2] == "fetch-aux":
        # python sync.py <slug> fetch-aux — дотянуть laps+comment ко всему архиву
        print(f"дозагрузка laps+comment профиля {slug} (что недостаёт)...")
        r = fetch_aux(slug)
        print("-" * 60)
        print(f"laps дотянуто: {r['laps_fetched']}")
        print(f"comment дотянуто: {r['comment_fetched']}")
        print(f"ошибок: {r['errors']}")
        print(f"время: {r['elapsed_s']}c")
        # laps могли прийти → deferred-метки переоцениваются (deferred→ok/invalid) (§3.6)
        um = recompute_user_marks(slug)
        print(f"user-метки: активностей {um['activities']}, resolved {um['ok_resolved']}, "
              f"pending {um['pending_resolve']}, invalid {um['invalid']}, deferred {um['deferred']}")
        sys.stdout.flush()
        os._exit(0)

    if len(sys.argv) > 2 and sys.argv[2] == "recompute-user-marks":
        # python sync.py <slug> recompute-user-marks — только user-метки (validation+резолв),
        # без пересчёта enriched. Для отладки/точечного добивания отложенного.
        um = recompute_user_marks(slug)
        print(json.dumps(um, ensure_ascii=False, indent=2))
        sys.stdout.flush()
        os._exit(0)

    if len(sys.argv) > 2 and sys.argv[2] == "recompute":
        # python sync.py <slug> recompute — офлайн-пересчёт ВСЕХ из сырья (смена ALGO_VERSION)
        print(f"офлайн-пересчёт профиля {slug} из сохранённого сырья (без сети)...")
        r = enrich_batch(slug, limit=10_000, offline=True)
        print("-" * 60)
        print(f"пересчитано: {r.enriched_ok}")
        print(f"аномалий: {len(r.anomalies)}")
        for a in r.anomalies[:20]:
            print(f"  {a['activity_id']}: {a['error']}")
        if len(r.anomalies) > 20:
            print(f"  ... ещё {len(r.anomalies)-20}")
        print(f"время: {r.elapsed_s:.1f}c")
        # user-метки: validation из текущих laps + резолв под новой версией (§3.6)
        um = recompute_user_marks(slug)
        print(f"user-метки: активностей {um['activities']}, resolved {um['ok_resolved']}, "
              f"pending {um['pending_resolve']}, invalid {um['invalid']}, deferred {um['deferred']}")
        sys.stdout.flush()
        os._exit(0)

    if len(sys.argv) > 2 and sys.argv[2] == "aggregate":
        # python sync.py <slug> aggregate — кросс-агрегаты по периодам (этап 5).
        # Офлайн, без сети: читает только enriched + каталог (не streams).
        import profiles as _pf
        from aggregate import aggregate_profile
        print(f"агрегация профиля {slug} по периодам (из enriched, без сети)...")
        res = aggregate_profile(slug, str(_pf.resolve(slug).db_path))
        print("-" * 60)
        print(f"версия: {res['algo_version']}")
        print(f"периодов: {len(res['periods'])}")
        for pk in sorted(res["periods"]):
            s = res["periods"][pk]
            print(f"  {pk}: активностей {s['activities']}, обогащено {s['enriched']}, "
                  f"max_hr_acc {s['max_hr_acc']}, decoupling {s['decoupling_n']}, "
                  f"recovery-событий {s['recovery_events']}")
        sys.stdout.flush()
        os._exit(0)

    if len(sys.argv) > 2 and sys.argv[2] == "enrich":
        # python sync.py <slug> enrich [limit]
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        print(f"обогащение профиля {slug}, лимит {limit} (свежие первыми)...")
        r = enrich_batch(slug, limit=limit)
        print("-" * 60)
        print(f"запрошено: {r.requested}")
        print(f"скачано сырья (streams): {r.raw_fetched}")
        print(f"обогащено: {r.enriched_ok}")
        print(f"пропущено (уже сделано): {r.skipped_done}")
        print(f"аномалий: {len(r.anomalies)}")
        for a in r.anomalies:
            print(f"  АНОМАЛИЯ {a['activity_id']}: {a['error']}")
        print(f"время: {r.elapsed_s:.1f}c")
        sys.stdout.flush()
        # форсированный выход: сетевые keep-alive сессии garminconnect держат
        # фоновый поток живым после возврата → процесс висит без приглашения.
        # Работа и БД-коммиты уже завершены, выходим чисто.
        os._exit(0)

    # argv-safety: сюда доходим, только если argv[2] НЕ совпал ни с одной подкомандой
    # выше. Раньше любой нераспознанный argv[2] молча проваливался в полный сетевой
    # sync_catalog (горизонт 20 лет) — опечатка «statsu» = архивный синк. Теперь:
    # нераспознанная подкоманда → ошибка+usage; дефолтный синк ТОЛЬКО при голом slug
    # (len==2, без подкоманды — легитимное «синкни каталог»). Различитель — ЕСТЬ ли
    # argv[2], не «какой он».
    if len(sys.argv) > 2:
        print(f"неизвестная подкоманда: {sys.argv[2]!r}")
        print("usage: python sync.py <slug> "
              "[status|fetch-aux|recompute|recompute-user-marks|aggregate|enrich [limit]]")
        print("  без подкоманды — sync каталога (сетевой, полный горизонт)")
        sys.exit(2)

    print(f"sync каталога профиля {slug} (окна по {WINDOW_MONTHS} мес, "
          f"горизонт {HISTORY_YEARS} лет)...")
    r = sync_catalog(slug)
    for line in r.window_log:
        print(line)
    print("-" * 60)
    print(f"окон обработано: {r.windows}")
    print(f"активностей upsert: {r.activities_upserted}")
    print(f"диапазон в БД: {r.range_start}..{r.range_end}")
    print(f"время: {r.elapsed_s:.1f}c")
    if r.stopped_early:
        print(f"ОСТАНОВЛЕНО РАНО: {r.stop_reason}")
        print("перезапустите sync — продолжит с недокачанного окна (предыдущие в БД).")
