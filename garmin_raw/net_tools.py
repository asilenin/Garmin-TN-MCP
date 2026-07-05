"""net_tools.py — СЕТЕВЫЕ тулы (этап 7.6, контракт QA Q4/Q6).

Модуль-владелец точек входа в сеть из чата, симметричный fetch.py: fetch владеет
СЫРОЙ сетью (сокеты/throttle/retry), net_tools — ТУЛАМИ, которые в неё ходят. Оба
явно сетевые; tools.py по контрасту — cache-only ПО ПОСТРОЕНИЮ (не импортирует
Fetcher; замок test_cache_only проверяет это статически+динамически под
forbid_network).

Здесь живут все сетевые тулы: garmin_wellness сейчас, garmin_sync/garmin_sync_estimate
позже (тот же класс — контракт Q5). Разделение модулей, а не «tools.py + пометка»:
пометка дрейфует бесшумно (класс ошибки декоратора), импорт Fetcher — grep-проверяемый
структурный признак.

Классификация по оси Q4: garmin_wellness — сетевой READ (ходит в сеть, но пишет только
в кэш-как-след-похода, не пользовательские данные; цель вызова — вернуть данные СЕЙЧАС,
не «оставить raw навсегда»). Симметрия с garmin_sync_estimate.
"""
from __future__ import annotations

from typing import Any, Optional

import profiles
from store import Store

# Зонды wellness — имена берём из Fetcher (единый источник, не дублируем список).
from fetch import Fetcher


def garmin_wellness(slug: str, date: str, *, refresh: bool = False) -> dict:
    """Wellness за дату: сон/HRV/RHR/стресс/BodyBattery. СЕТЕВОЙ read.

    Порядок (КРИТИЧНО для cache-only-инварианта, Q4): кэш проверяется ДО создания
    Fetcher — при полном валидном кэше сеть (и ленивый login-сокет) не трогается
    вовсе. Fetcher создаётся ТОЛЬКО когда есть чего докачивать.

    Свежесть НЕ судится здесь (Q6): возвращаем данные + fetched_at + возраст даты,
    «дозрело или перекачать» решает LLM. refresh=True — принудительно перекачать все
    зонды (LLM решил, что кэш устарел), иначе качаем только отсутствующие зонды.

    Возврат: {date, requested_at_age_days, probes: {probe: {status, detail,
    payload, fetched_at, derived_fields}}}. derived_fields помечает поля Garmin-
    производные (body_battery/stress) — факт для LLM, не резка (Q6 разв. C).
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

    # --- фаза 3: сборка ответа (возраст даты как факт свежести, Q6) ---
    try:
        y, m, d = (int(x) for x in date.split("-"))
        age_days = (_date.today() - _date(y, m, d)).days
    except (ValueError, TypeError):
        age_days = None

    # derived-поля (Garmin-производные) — помечаем, НЕ режем (Q6 разв. C).
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
        "requested_at_age_days": age_days,   # факт свежести — суждение LLM (Q6)
        "probes": probes_out,
    }
    if login_error is not None:
        out["login_error"] = login_error   # сообщение reauth; причинность — в blocked_by_auth
    return out
