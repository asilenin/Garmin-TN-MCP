"""aggregate.py — кросс-активностные агрегаты по периодам (этап 5, ТЗ §3.5, МЕТОД §5.4).

ДЕМАРКАЦИЯ (сквозной принцип, ТЗ §1.1, МЕТОД §1.8): aggregate считает якорь-нейтральные,
БЕЗЫМЯННЫЕ агрегаты и прикладывает провенанс. Всё, что требует порога-якоря, имени или
суждения (intensity_distribution, фиксированный pace_at_fixed_hr, «база держит»,
«сравнимо ли») — НЕ здесь, это LLM. Журнал решений: TN_Garmin_MCP_QA.md, этап 5.

Вход — ТОЛЬКО enriched (компактные строки) + каталог. streams массово НЕ читаются
(это сделало бы aggregate тяжёлым и вывело формулы из-под версионирования — см. QA Q6).
Всё streams/laps-зависимое уже посчитано в enrich (decoupling, hr_recovery, бакеты).

Что считает (все якорь-нейтральные):
- volume_7d/28d        — скользящие окна км (якоря нет)
- max_hr_accumulated   — ~97-й перцентиль распределения per-activity max (НЕ max(), §2.4)
- pace_by_hr_grid       — темп по сетке HR, BY_SOURCE (§3.5.1); ячейка {center,dispersion,
                          seconds,n_activities} из сложенных sum/sum_sq (Q3)
- gct_by_pace_grid      — биомеханика по сетке темпа, одной строкой (железо-незав.)
- decoupling            — 2D (value × Δpace по бинам-осям) + факты; БЕЗ счётчиков (Q11)
- hr_recovery           — распределение hr_drop+duration, не схлопывать (Q8/Q9)
- provenance            — hr_sources/device_models с долями и n; unknown как «не знаю» (Q12)

period_key НЕ хардкодит кварталы — допускает другие гранулярности (Q5).
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from store import Store
from enrich import ALGO_VERSION


# --- Параметры агрегации (версионируемы вместе с ALGO_VERSION) -----------------
ARCHIVE_MAX_HR_PERCENTILE = 97.0   # §2.4: потолок = 97-й перцентиль per-activity max, не max()
DECOUPLING_DELTA_PACE_BINS = [-1e9, -20, -8, 8, 20, 1e9]  # бины Δpace (сек/км) как ОСИ (Q11)
# Δpace = pace_2nd − pace_1st. Отрицательный = 2-я половина быстрее (разгон/прогрессив).
# Бины: сильный разгон / разгон / держался / замедление / сильное замедление.
# Это ОСИ гистограммы, не вердикт «прогрессив» (Q11: бин ≠ счётчик-порог).
HR_DROP_BINS = [-1e9, 0, 10, 20, 30, 40, 1e9]  # бины падения HR (уд) как оси для hr_recovery


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные: перцентиль, бинаризация, period_key
# ─────────────────────────────────────────────────────────────────────────────

def _percentile(values: list[float], q: float) -> Optional[float]:
    """q-й перцентиль (линейная интерполяция). None на пустом."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    pos = (q / 100.0) * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(s[lo])
    frac = pos - lo
    return float(s[lo] * (1 - frac) + s[hi] * frac)


def _quarter_key(d: str) -> str:
    """YYYY-MM-DD → YYYY-Qn. period_key допускает и другие гранулярности (Q5):
    меняется только эта функция, схема не трогается."""
    y, m, _ = d.split("-")
    q = (int(m) - 1) // 3 + 1
    return f"{y}-Q{q}"


def _bin_index(value: float, edges: list[float]) -> int:
    """Индекс бина для значения по краям edges (как оси гистограммы)."""
    for i in range(len(edges) - 1):
        if edges[i] <= value < edges[i + 1]:
            return i
    return len(edges) - 2


# ─────────────────────────────────────────────────────────────────────────────
# Сложение достаточной статистики бакетов (Q3: sum/sum_sq/seconds аддитивны)
# ─────────────────────────────────────────────────────────────────────────────

def _fold_suffstat(acc: dict, cell: dict) -> None:
    """Складывает {sum,sum_sq,seconds} ячейки в аккумулятор (in-place).
    Суммы аддитивны — точное объединение по периоду (Q3)."""
    if not cell:
        return
    acc["sum"] = acc.get("sum", 0.0) + cell.get("sum", 0.0)
    acc["sum_sq"] = acc.get("sum_sq", 0.0) + cell.get("sum_sq", 0.0)
    acc["seconds"] = acc.get("seconds", 0.0) + cell.get("seconds", 0.0)
    acc["n_activities"] = acc.get("n_activities", 0) + 1


def _suffstat_to_center_dispersion(acc: dict) -> Optional[dict]:
    """{sum,sum_sq,seconds,n} → {center, dispersion, seconds, n_activities}.
    center = sum/seconds (взвешенное среднее); dispersion = √(sum_sq/seconds − center²)
    (std пула). Оба выводятся на ЧТЕНИИ из аддитивной статистики (Q3 — не фиксируем).
    dispersion — ФЛАГ достоверности center (широкий → LLM не берёт точку), не сигнал."""
    sec = acc.get("seconds", 0.0)
    if sec <= 0:
        return None
    center = acc["sum"] / sec
    var = acc["sum_sq"] / sec - center * center
    disp = math.sqrt(var) if var > 0 else 0.0
    return {
        "center": round(center, 1),
        "dispersion": round(disp, 1),
        "seconds": round(sec, 1),
        "n_activities": acc.get("n_activities", 0),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Сетки pace_by_hr_grid (by_source) и gct_by_pace_grid (одной строкой)
# ─────────────────────────────────────────────────────────────────────────────

def _aggregate_pace_by_hr(enr_rows: list[dict], hr_source_by_id: dict) -> dict:
    """pace_by_hr_grid BY_SOURCE (§3.5.1): складываю pace_by_hr_bucket периода РАЗДЕЛЬНО
    по hr_source. HR — ось числа: смешивать источники на входе нельзя (оптика врёт на
    высоком пульсе, среднее с нагрудником — мусор; provenance снаружи это не расцепит).

    Сейчас hr_source NULL у всех → одна под-сетка 'unknown' (Q12). unknown = «не знаю,
    возможно менялся», НЕ «однородно»: LLM обязан пометить пульсовую динамику
    недостоверной, пока источник не разложен. Структура оживёт без переписывания, когда
    hr_source заполнится (следующий пункт после aggregate)."""
    # {source: {hr_bucket: {sum,sum_sq,seconds,n_activities}}}
    by_source: dict[str, dict] = defaultdict(lambda: defaultdict(dict))
    for row in enr_rows:
        buckets = row.get("pace_by_hr_bucket") or {}
        if not buckets:
            continue
        src = hr_source_by_id.get(row["activity_id"]) or "unknown"
        for hr_bucket, cell in buckets.items():
            pace_cell = cell.get("pace") if isinstance(cell, dict) else None
            if pace_cell:
                _fold_suffstat(by_source[src][hr_bucket], pace_cell)
    # сворачиваем в center/dispersion
    out: dict[str, dict] = {}
    for src, grid in by_source.items():
        out[src] = {}
        for hr_bucket, acc in grid.items():
            cd = _suffstat_to_center_dispersion(acc)
            if cd:
                out[src][hr_bucket] = cd
    return out


def _aggregate_gct_by_pace(enr_rows: list[dict]) -> dict:
    """gct_by_pace_grid ОДНОЙ СТРОКОЙ: биомеханика железо-независима (§4 МЕТОД — каденс/
    GCT слабо зависят от модели часов), источники не дробим. Складываю
    biomech_by_pace_bucket периода по темп-бакетам. Каждая метрика (gct/vert_ratio/
    stride) — своя достаточная статистика. Источник датчика (нагрудник/foot-pod) —
    в provenance как факт наличия, не дробит сетку."""
    # {pace_bucket: {metric: {sum,sum_sq,seconds,n}}}
    grid: dict[str, dict] = defaultdict(lambda: defaultdict(dict))
    for row in enr_rows:
        buckets = row.get("biomech_by_pace_bucket") or {}
        for pace_bucket, metrics in buckets.items():
            if not isinstance(metrics, dict):
                continue
            for mname in ("gct", "vert_ratio", "stride"):
                cell = metrics.get(mname)
                if cell:
                    _fold_suffstat(grid[pace_bucket][mname], cell)
    out: dict[str, dict] = {}
    for pace_bucket, metrics in grid.items():
        cell_out = {}
        for mname, acc in metrics.items():
            cd = _suffstat_to_center_dispersion(acc)
            if cd:
                cell_out[mname] = cd
        if cell_out:
            out[pace_bucket] = cell_out
    return out


# ─────────────────────────────────────────────────────────────────────────────
# decoupling: 2D (value × Δpace), БЕЗ счётчиков категорий (Q11)
# ─────────────────────────────────────────────────────────────────────────────

def _aggregate_decoupling(enr_rows: list[dict]) -> dict:
    """decoupling периода как 2D-РАСПРЕДЕЛЕНИЕ (value × Δpace), НЕ счётчики (Q11).

    Счётчик n_progressive прятал бы порог: чтобы посчитать «сколько прогрессивов»,
    aggregate должен классифицировать тренировку по границе Δpace = зашитое суждение
    (§3.5.2). Вместо этого — распределение decoupling.value, размеченное по ОСИ Δpace
    (бины Δpace как оси гистограммы, не вердикт «прогрессив»). LLM сам режет: бин Δpace≈0
    → чистый дрейф базы; бин большой Δpace → прогрессив, игнор. Граница — у LLM.

    Δpace = pace_2nd_half − pace_1st_half. Отрицательный = 2-я половина быстрее (разгон).
    Каждый бин Δpace несёт распределение value (перцентили) + сумму времени + n.
    + pace_variance-сводка (ловит пилу) как факт.
    """
    # по бинам Δpace собираем value и duration
    bins: dict[int, dict] = {}
    n_bins = len(DECOUPLING_DELTA_PACE_BINS) - 1
    for i in range(n_bins):
        lo, hi = DECOUPLING_DELTA_PACE_BINS[i], DECOUPLING_DELTA_PACE_BINS[i + 1]
        bins[i] = {"delta_pace_range": [None if lo <= -1e8 else lo,
                                        None if hi >= 1e8 else hi],
                   "values": [], "durations": [], "pace_variances": []}
    total = 0
    for row in enr_rows:
        dec = row.get("decoupling")
        if not dec or dec.get("reason") != "ok" or dec.get("value") is None:
            continue
        p1, p2 = dec.get("pace_1st_half"), dec.get("pace_2nd_half")
        if p1 is None or p2 is None:
            continue
        delta = p2 - p1
        bi = _bin_index(delta, DECOUPLING_DELTA_PACE_BINS)
        bins[bi]["values"].append(dec["value"])
        if dec.get("duration_s") is not None:
            bins[bi]["durations"].append(dec["duration_s"])
        if dec.get("pace_variance") is not None:
            bins[bi]["pace_variances"].append(dec["pace_variance"])
        total += 1
    # сворачиваем каждый бин в распределение value (перцентили — это ЧТЕНИЕ, не хранимое
    # производное; здесь итоговый агрегат, перцентили легитимны как сводка распределения)
    dist = []
    for i in range(n_bins):
        b = bins[i]
        vals = b["values"]
        if not vals:
            continue
        dist.append({
            "delta_pace_range": b["delta_pace_range"],   # ось: где 2-я половина по темпу
            "n_activities": len(vals),
            "value_p25": round(_percentile(vals, 25), 4),
            "value_median": round(_percentile(vals, 50), 4),
            "value_p75": round(_percentile(vals, 75), 4),
            "median_pace_variance": round(_percentile(b["pace_variances"], 50), 1)
                if b["pace_variances"] else None,   # ловит пилу (Q10)
            "median_duration_s": round(_percentile(b["durations"], 50), 1)
                if b["durations"] else None,
        })
    return {"by_delta_pace": dist, "n_total": total}


# ─────────────────────────────────────────────────────────────────────────────
# hr_recovery: распределение hr_drop+duration, не схлопывать (Q8/Q9)
# ─────────────────────────────────────────────────────────────────────────────

def _aggregate_hr_recovery(enr_rows: list[dict]) -> dict:
    """hr_recovery периода как РАСПРЕДЕЛЕНИЕ hr_drop, размеченное по оси recovery_duration
    — НЕ схлопывать в одно число (несравнимость кругов разной длины — суждение LLM §3.5.2:
    duration едет рядом с drop). Считаем по recovery-СОБЫТИЯМ всех тренировок периода.

    Также прикладываем счётчики reason как ФАКТ доступности (no_laps/single_lap/
    no_fast_laps/ok) — это НЕ классификация-порог, а механическая определимость (§3.5.2):
    сколько тренировок дали recovery-события vs структурно не могли. Это факт данных, не
    суждение о значимости."""
    events = []   # все recovery-события периода
    reason_counts: dict[str, int] = defaultdict(int)
    for row in enr_rows:
        rec = row.get("hr_recovery")
        if not rec:
            continue
        reason_counts[rec.get("reason", "unknown")] += 1
        for ev in rec.get("events", []):
            events.append(ev)
    if not events:
        return {"events_total": 0, "reason_counts": dict(reason_counts),
                "by_duration": []}
    # распределение hr_drop по бинам длительности восстановления (ось = duration,
    # потому что drop за 60с и за 120с несравнимы — LLM нормирует по duration сам)
    dur_bins: dict[int, list] = defaultdict(list)
    DUR_EDGES = [0, 30, 60, 90, 120, 1e9]
    for ev in events:
        dur = ev.get("recovery_duration_s")
        drop = ev.get("hr_drop")
        if dur is None or drop is None:
            continue
        bi = _bin_index(dur, DUR_EDGES)
        dur_bins[bi].append(drop)
    by_dur = []
    for i in range(len(DUR_EDGES) - 1):
        drops = dur_bins.get(i, [])
        if not drops:
            continue
        lo, hi = DUR_EDGES[i], DUR_EDGES[i + 1]
        by_dur.append({
            "duration_range_s": [lo, None if hi >= 1e8 else hi],  # ось
            "n_events": len(drops),
            "drop_p25": round(_percentile(drops, 25), 1),
            "drop_median": round(_percentile(drops, 50), 1),
            "drop_p75": round(_percentile(drops, 75), 1),
        })
    return {
        "events_total": len(events),
        "reason_counts": dict(reason_counts),  # факт доступности, не классификация
        "by_duration": by_dur,
    }


# ─────────────────────────────────────────────────────────────────────────────
# volume, max_hr_accumulated, provenance
# ─────────────────────────────────────────────────────────────────────────────

def _volume_windows(catalog: list[dict], period_dates: list[str]) -> dict:
    """volume_7d/28d — скользящие окна км. Якоря нет (км — факт, не порог).
    Для периода берём ПОСЛЕДНЕЕ окно периода (макс date) как репрезентативное —
    7д/28д объём к концу периода. Каталог должен быть отсортирован по date."""
    if not period_dates:
        return {"7d": None, "28d": None}
    end = max(date.fromisoformat(d) for d in period_dates)
    def window_km(days: int) -> float:
        start = end - timedelta(days=days)
        km = 0.0
        for a in catalog:
            try:
                ad = date.fromisoformat(a["date"])
            except (ValueError, TypeError):
                continue
            if start < ad <= end and a.get("distance_m"):
                km += a["distance_m"] / 1000.0
        return round(km, 2)
    return {"7d": window_km(7), "28d": window_km(28)}


def _provenance(catalog_period: list[dict], enr_rows: list[dict]) -> dict:
    """ФАКТ происхождения (§3.5.1): hr_sources/device_models с долями времени и числом
    тренировок n. БЕЗ суждения о сравнимости — это LLM по факту.

    unknown отдаётся как «НЕ ЗНАЮ» (Q12), не как «однородно». LLM, увидев
    hr_sources={unknown:1.0}, ОБЯЗАН пометить пульсовую динамику «источник не разложен,
    сравнение через годы недостоверно» (§5.4/§1.7). Не зашиваем «unknown → сравнивать
    можно» — пустота должна быть честной, не притворяться чистотой."""
    n = len(catalog_period)
    if n == 0:
        return {"hr_sources": {}, "device_models": {}, "n_activities": 0}
    hr_src: dict[str, int] = defaultdict(int)
    dev: dict[str, int] = defaultdict(int)
    for a in catalog_period:
        hr_src[a.get("hr_source") or "unknown"] += 1
        dev[a.get("device_model") or "unknown"] += 1
    return {
        "hr_sources": {k: round(v / n, 3) for k, v in hr_src.items()},
        "device_models": {k: round(v / n, 3) for k, v in dev.items()},
        "n_activities": n,
        # явная пометка, что источник не разложен (Q12) — чтобы LLM не принял за чистоту
        "hr_source_resolved": any(k != "unknown" for k in hr_src),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Главная функция: проход по архиву, строка на квартал
# ─────────────────────────────────────────────────────────────────────────────

def aggregate_profile(slug: str, db_path: str) -> dict:
    """Весь архив → period_aggregates, строка на квартал (Q5). Один проход по enriched,
    без сети, без streams массово. Возвращает сводку {period_key: счётчики}."""
    st = Store(db_path)
    try:
        av = st.meta_get("algo_version") or ALGO_VERSION
        # каталог: date, distance, hr_source, device_model, max_hr (max_hr живёт в
        # каталоге activities — надёжное фильтруемое поле, НЕ в activity_enriched)
        cat_rows = st.conn.execute(
            "SELECT activity_id, date, distance_m, hr_source, device_model, max_hr "
            "FROM activities ORDER BY date"
        ).fetchall()
        catalog = [dict(r) for r in cat_rows]
        # группируем по кварталу
        by_period: dict[str, list[dict]] = defaultdict(list)
        for a in catalog:
            if a.get("date"):
                by_period[_quarter_key(a["date"])].append(a)

        hr_source_by_id = {a["activity_id"]: a.get("hr_source") for a in catalog}
        max_hr_by_id = {a["activity_id"]: a.get("max_hr") for a in catalog}
        summary = {}
        for period_key, period_cat in by_period.items():
            ids = [a["activity_id"] for a in period_cat]
            # читаем enriched текущей версии (компактные строки, не streams)
            enr_rows = []
            max_hrs = []
            for aid in ids:
                e = st.get_enriched(aid, av)
                if e:
                    e["activity_id"] = aid
                    enr_rows.append(e)
                mh = max_hr_by_id.get(aid)   # max_hr из каталога (§2.4 per-activity)
                if mh is not None:
                    max_hrs.append(float(mh))

            agg = {
                "max_hr_accumulated": (
                    int(round(_percentile(max_hrs, ARCHIVE_MAX_HR_PERCENTILE)))
                    if max_hrs else None),   # §2.4: перцентиль, НЕ max()
                "volume_7d": None, "volume_28d": None,
                "pace_by_hr_grid": _aggregate_pace_by_hr(enr_rows, hr_source_by_id),
                "gct_by_pace_grid": _aggregate_gct_by_pace(enr_rows),
                "decoupling": _aggregate_decoupling(enr_rows),
                "hr_recovery": _aggregate_hr_recovery(enr_rows),
                "provenance": _provenance(period_cat, enr_rows),
            }
            vol = _volume_windows(catalog, [a["date"] for a in period_cat if a.get("date")])
            agg["volume_7d"] = vol["7d"]
            agg["volume_28d"] = vol["28d"]

            st.put_aggregate(period_key, av, agg)
            summary[period_key] = {
                "activities": len(period_cat),
                "enriched": len(enr_rows),
                "max_hr_acc": agg["max_hr_accumulated"],
                "hr_grid_sources": list(agg["pace_by_hr_grid"].keys()),
                "decoupling_n": agg["decoupling"]["n_total"],
                "recovery_events": agg["hr_recovery"]["events_total"],
            }
        return {"algo_version": av, "periods": summary}
    finally:
        st.close()


if __name__ == "__main__":
    import sys
    import json as _json
    if len(sys.argv) < 2:
        print("usage: python aggregate.py <slug> [db_path]")
        sys.exit(1)
    import profiles
    slug = sys.argv[1]
    dbp = sys.argv[2] if len(sys.argv) > 2 else str(profiles.resolve(slug).db_path)
    result = aggregate_profile(slug, dbp)
    print(_json.dumps(result, ensure_ascii=False, indent=2))
