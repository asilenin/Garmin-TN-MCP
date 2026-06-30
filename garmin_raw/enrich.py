"""enrich.py — детерминированное обогащение per-activity (этап 4, §14; МЕТОД §3.1).

Из посекундного потока считает ЧИСЛА (не ярлыки): гистограммы пульса/темпа на
moving-time, кластеры темп↔пульс, дисперсии, переходы через медиану, биомеханику,
рельеф, лактат раздельно по источнику. Версионировано: ALGO_VERSION.

Ключевые решения (из обсуждения):
  moving-time — СВОЙ порог по сырой directSpeed (не grade-adjusted, не флаг
    Garmin): режем стоянки, а не «всё медленное». Параметры версионируемы.
  numpy/pandas — обязательны (массовый пересчёт архива должен быть дешёвым).
  лактат — РАЗДЕЛЬНО по источнику: from_watch (привязка к таймстампу → сразу
    подтягиваем hr/pace той секунды = калибровочная точка) vs from_comment
    (текстовая привязка). count по watch. watch/comment НЕ схлопываем.

Реальные ключи потока (сверено на живом аккаунте, FR-серия):
  directTimestamp(5) directSpeed(7) directHeartRate(24) directRunCadence(0)
  sumDistance(4) directGroundContactTime(16) directVerticalRatio(19)
  directStrideLength(18) directElevation(14) directGradeAdjustedSpeed(32)
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

# Версия ФОРМУЛ обогащения. Смена → массовый пересчёт уровня A (§7). НЕ путать
# со schema_version (структура таблиц). Любое изменение параметров ниже = bump.
# 0.2.2: свёртка гистограммы в форму переписана — полка собирается ростом от пика
#        (граница=впадина), доля=вся масса полки; mono/diffuse различаются по
#        КОНЦЕНТРАЦИИ массы в ядре (горб vs плато), не по абсолютной ширине.
# 0.3.0: добавлены две бакетные выжимки для сеток периода (этап 5) — НОВЫЕ поля,
#        старая калибровка (фильтр пульса, перцентили, свёртка формы, moving-time,
#        кластеры) НЕ ТРОНУТА. biomech_by_pace_bucket (ось=темп) и pace_by_hr_bucket
#        (ось=пульс): {median,p25,p75,seconds} на бакет, без метки источника.
#        Из них aggregate строит gct_by_pace_grid и pace_by_hr_grid (by_source).
ALGO_VERSION = "enrich-0.3.0"

# --- Параметры moving-time (версионируемы; не магические константы в коде) ----
# Порог остановки по СЫРОЙ скорости (м/с). ~1.35 м/с ≈ темп ~12:20 мин/км —
# заведомо ниже самого медленного бега, ловит стоянку/шаг, не трогает трусцу.
MOVING_SPEED_MIN_MPS = 1.35
# Остановка засчитывается только если держится ≥ N секунд подряд (фильтр
# одиночных просадок GPS на повороте).
STOP_MIN_DURATION_S = 4

# --- Отсев ЗАВЕДОМОГО не-пульса (узко, для всех потребителей; версионируемо) -----
# Принцип: чистка — свойство КАЖДОЙ метрики, не стена на входе. Здесь режем только
# то, что не пульс ни для кого (датчик соврал технически): значения вне
# человеческого диапазона и залипание (stuck-value). Робастные метрики (max_hr=
# перцентиль, взвешенная гистограмма) чистят себя сами конструкцией — доп-отсев им
# не нужен и ВРЕДЕН (агрессивный фильтр резал 58% реального разгона пульса).
HR_HUMAN_MIN_BPM = 40          # ниже = полная потеря контакта (не пульс)
HR_HUMAN_MAX_BPM = 215         # выше человеческого максимума = артефакт захвата
STUCK_IDENTICAL_COUNT = 4      # N идентичных подряд = залипание датчика (stuck-value)

# max_hr — РОБАСТНАЯ перцентильная оценка, не абсолют. Принцип: метрика, опирающаяся
# на единственную точку, хрупка (один выживший артефакт её ломает); перцентиль
# устойчив конструктивно. 99-й (не 95-й): отсекает артефакты, но СОХРАНЯЕТ короткий
# реальный финишный спурт (секунды на максимуме = <5% времени, 95-й бы его срезал).
# На длинных тестах даёт ~реальный максимум усилия. Версионируемо.
#
# ПРИНЦИП (распространяется на агрегаты, §5/aggregate.py): экстремумы по архиву
# считаются ПЕРЦЕНТИЛЬНО на ВСЕХ уровнях, не максимумом. archive max_hr = высокий
# перцентиль (~97-й) распределения per-activity максимумов, НЕ max() из них —
# иначе один выживший артефакт из 2000 тренировок станет «потолком атлета»
# (максимум усиливает выброс, перцентиль сглаживает). max и робастность
# несовместимы по построению — везде, где нужен «максимум за период», берём перцентиль.
MAX_HR_PERCENTILE = 99

# --- Свёртка гистограммы в форму (для compact-тулзы; МЕТОД §3.1) ----------------
# Гистограмма → модальность + значимые полки {позиция, доля} + разброс. Сохраняет
# ФОРМУ (не среднее), но компактно.
# Полка собирается РОСТОМ ОТ ПИКА: от самого высокого бакета идём в обе стороны,
# присоединяя бакеты, пока склон СПАДАЕТ; граница полки = впадина (локальный
# минимум, бакет пошёл вверх) или падение ниже шумового пола. Доля полки = СУММА
# всей собранной массы (не пиковый бакет — это и давало ложный diffuse на широкой
# полке). Адаптивно: узкая полка упрётся во впадину быстро, широкая — поздно.
SHELF_NOISE_FLOOR_SHARE = 0.04  # бакет ниже этой доли общего времени не входит в полку (хвост-дрожь)
SHELF_MIN_SHARE = 0.15          # собранная полка значима, если несёт ≥ этой доли времени
SHELF_MONO_SHARE = 0.5          # одна полка с долей ≥ этого = mono (иначе diffuse)
# Различение «широкая полка vs размазанный прогрессив» — по КОНЦЕНТРАЦИИ массы,
# не по абсолютной ширине (ширина в ударах не переносится между атлетами). Ширина —
# лишь дешёвый ПРЕДОХРАНИТЕЛЬ: узкие полки не проверяем. Вердикт — доля массы в ядре.
SHELF_WIDE_GUARD_SPREAD = 35    # полка шире этого (уд) → проверить концентрацию (предфильтр)
SHELF_CORE_HALFWIDTH = 10       # ядро = центр ± это (уд)
SHELF_CORE_CONCENTRATION = 0.55 # доля массы полки в ядре ниже этого = плато (diffuse), не горб

# --- Параметры гистограмм/кластеров (версионируемы) ---------------------------
HR_BUCKET_BPM = 5            # бакет гистограммы пульса (МЕТОД §3.1)
PACE_BUCKET_S_PER_KM = 10    # бакет гистограммы темпа (сек/км)
# Кластер темп↔пульс: участок, где и темп, и пульс стабильны N секунд подряд.
CLUSTER_MIN_DURATION_S = 30
CLUSTER_PACE_TOL_S = 8       # допуск стабильности темпа внутри кластера (сек/км)
CLUSTER_HR_TOL_BPM = 4       # допуск стабильности пульса (уд/мин)
CLUSTER_MAX = 3             # сколько кластеров отдаём (1–3, МЕТОД §3.1)

# --- Бакетные выжимки для сеток периода (enrich-0.3.0; ТЗ §3.5, §3.5.1) --------
# Две НЕЗАВИСИМЫЕ бакетизации одних streams по РАЗНЫМ осям (две физиологические
# связки, нельзя слить в одну ось):
#   biomech_by_pace_bucket — ось=ТЕМП: «GCT/vert-ratio/шаг на данном темпе»
#       (экономичность; gct_by_pace_grid периода агрегируется отсюда);
#   pace_by_hr_bucket      — ось=ПУЛЬС: «темп на данном пульсе»
#       (аэробная эффективность; pace_by_hr_grid by_source агрегируется отсюда).
# Значение бакета несёт ПЕРЦЕНТИЛИ (median/p25/p75 — ФОРМА, не момент: дисперсия
# смазала бы асимметричный GCT-дрейф под усталостью; перцентили робастны, §2.4) и
# СЕКУНДЫ в бакете (взвешенная агрегация по периоду: 5 мин и 40 мин на одном темпе
# вносят разный вес). Метки hr_source НЕТ — тренировка вся от одного источника;
# разбивку by_source делает aggregate по каталогу (демаркация §1.1).
# Малое seconds в бакете НЕ фильтруется здесь (§3.5.2: определимость, не значимость) —
# отдаём {значение, seconds} как есть, «достаточно ли времени» решает aggregate/LLM.
PACE_BIOMECH_BUCKET_S_PER_KM = 15  # ширина темп-бакета биомеханики (старт; бьётся с pace-гистограммой)
HR_PACE_BUCKET_BPM = 5             # ширина HR-бакета для pace_by_hr (как hr-гистограмма)

# Ключи потока
K_TS = "directTimestamp"
K_SPEED = "directSpeed"
K_HR = "directHeartRate"
K_CAD = "directDoubleCadence"   # суммарный беговой каденс (~170), НЕ directRunCadence (~85, половинный)
K_DIST = "sumDistance"
K_GCT = "directGroundContactTime"
K_VR = "directVerticalRatio"
K_STRIDE = "directStrideLength"
K_ELEV = "directElevation"


def _index_map(stream: dict) -> dict[str, int]:
    """key → metricsIndex по metricDescriptors."""
    out: dict[str, int] = {}
    for m in (stream.get("metricDescriptors") or []):
        k = m.get("key")
        if k is not None:
            out[k] = m["metricsIndex"]
    return out


def _column(rows: list[dict], idx: Optional[int]) -> np.ndarray:
    """Достаёт колонку по индексу из activityDetailMetrics → float-массив с NaN.

    Если метрики нет в потоке (idx is None — напр. нет бегового датчика), возвращает
    массив NaN ДЛИНЫ ПОТОКА, а не пустой — иначе сломается broadcast с маской.
    """
    n = len(rows)
    if idx is None:
        return np.full(n, np.nan)
    vals = []
    for r in rows:
        mv = r.get("metrics", [])
        vals.append(mv[idx] if idx < len(mv) and mv[idx] is not None else np.nan)
    return np.array(vals, dtype=float)


def _moving_mask(speed: np.ndarray, dt: np.ndarray) -> np.ndarray:
    """Маска «в движении»: True = бег, False = стоянка.

    Стоянка = скорость < порога, ДЕРЖАЩАЯСЯ ≥ STOP_MIN_DURATION_S подряд (в РЕАЛЬНЫХ
    секундах через dt — поток у Garmin прорежен, ~1 сэмпл/3с, не посекундный).
    Режем стоянки, не «всё медленное».

    Устойчивость к прорежению: NaN-скорость трактуем как «медленно» внутри
    стоп-сегмента (дыра датчика на паузе — обычно и есть стоянка), чтобы единичные
    NaN не разрывали сегмент и не мешали накопить длительность.
    """
    if speed.size == 0:
        return np.array([], dtype=bool)
    # медленно ИЛИ нет данных скорости (на паузе датчик часто молчит)
    slow = (speed < MOVING_SPEED_MIN_MPS) | np.isnan(speed)
    mask = np.ones(speed.size, dtype=bool)
    i = 0
    n = speed.size
    while i < n:
        if not slow[i]:
            i += 1
            continue
        j = i
        dur = 0.0
        while j < n and slow[j]:
            dur += dt[j] if j < dt.size and not np.isnan(dt[j]) else 1.0
            j += 1
        if dur >= STOP_MIN_DURATION_S:
            mask[i:j] = False  # подтверждённая стоянка → вон из moving-time
        i = j
    return mask


def _histogram(values: np.ndarray, weights: np.ndarray, bucket: float) -> dict:
    """Взвешенная по времени гистограмма: бакет → секунды. Форма, не среднее (§3.1)."""
    v = values[~np.isnan(values)]
    w = weights[~np.isnan(values)]
    if v.size == 0:
        return {}
    lo = np.floor(v.min() / bucket) * bucket
    hi = np.ceil(v.max() / bucket) * bucket + bucket
    edges = np.arange(lo, hi, bucket)
    hist, _ = np.histogram(v, bins=edges, weights=w)
    return {int(edges[k]): round(float(hist[k]), 1)
            for k in range(len(hist)) if hist[k] > 0}


def _hr_valid_mask(hr: np.ndarray, ts: np.ndarray) -> np.ndarray:
    """Маска ВАЛИДНОГО пульса: режем только ЗАВЕДОМЫЙ не-пульс (узко).

    НЕ агрессивный фильтр (тот резал 58% реального разгона). Принцип: чистка —
    свойство каждой метрики, не стена на входе. Здесь убираем лишь то, что не пульс
    ни для кого:
      (1) вне человеческого диапазона: HR < HR_HUMAN_MIN или > HR_HUMAN_MAX;
      (2) залипание: STUCK_IDENTICAL_COUNT идентичных значений подряд (stuck-датчик).
    Единичные шипы-в-дыре НЕ ловим здесь — их проглатывают робастные метрики на
    выходе (max_hr=99-й перцентиль, взвешенная гистограмма). Дублировать = вред.

    Применяется к HR-гистограмме/кластерам/max_hr; moving-time и pace — нет.
    ts оставлен в сигнатуре для совместимости (временная привязка может понадобиться).
    """
    n = hr.size
    valid = np.ones(n, dtype=bool)
    if n == 0:
        return valid

    # (1) вне человеческого диапазона + NaN
    valid[(hr < HR_HUMAN_MIN_BPM) | (hr > HR_HUMAN_MAX_BPM) | np.isnan(hr)] = False

    # (2) залипание: серия из STUCK_IDENTICAL_COUNT идентичных значений подряд
    i = 0
    while i < n:
        if np.isnan(hr[i]):
            i += 1
            continue
        j = i + 1
        while j < n and hr[j] == hr[i]:   # точное равенство = stuck
            j += 1
        if (j - i) >= STUCK_IDENTICAL_COUNT:
            valid[i:j] = False
        i = max(j, i + 1)

    return valid


def _pace_s_per_km(speed: np.ndarray) -> np.ndarray:
    """м/с → сек/км. Нулевая/битая скорость → NaN."""
    out = np.full(speed.shape, np.nan)
    good = (speed > 0.1) & ~np.isnan(speed)
    out[good] = 1000.0 / speed[good]
    return out


def _median_crossings(pace: np.ndarray) -> int:
    """Число переходов темпа через медиану — счётчик «пилы» (§3.1)."""
    p = pace[~np.isnan(pace)]
    if p.size < 2:
        return 0
    med = np.median(p)
    sign = np.sign(p - med)
    sign = sign[sign != 0]
    return int(np.sum(sign[1:] != sign[:-1])) if sign.size > 1 else 0


def _clusters(pace: np.ndarray, hr: np.ndarray, dt: np.ndarray) -> list[dict]:
    """1–3 кластера темп↔пульс: участки, где и темп, и пульс стабильны N сек."""
    n = pace.size
    if n == 0:
        return []
    segments = []
    i = 0
    while i < n:
        if np.isnan(pace[i]) or np.isnan(hr[i]):
            i += 1
            continue
        j = i + 1
        p_ref, h_ref = pace[i], hr[i]
        dur = dt[i] if i < dt.size and not np.isnan(dt[i]) else 1.0
        while j < n and not np.isnan(pace[j]) and not np.isnan(hr[j]) \
                and abs(pace[j] - p_ref) <= CLUSTER_PACE_TOL_S \
                and abs(hr[j] - h_ref) <= CLUSTER_HR_TOL_BPM:
            dur += dt[j] if j < dt.size and not np.isnan(dt[j]) else 1.0
            j += 1
        if dur >= CLUSTER_MIN_DURATION_S:
            seg_pace = pace[i:j]
            seg_hr = hr[i:j]
            segments.append({
                "pace_s_per_km": round(float(np.median(seg_pace)), 1),
                "hr_bpm": round(float(np.median(seg_hr)), 1),
                "duration_s": round(float(dur), 1),
            })
        i = max(j, i + 1)
    # топ-N по длительности
    segments.sort(key=lambda s: s["duration_s"], reverse=True)
    return segments[:CLUSTER_MAX]


def _biomech_by_pace(pace: np.ndarray, cad: np.ndarray, gct: np.ndarray,
                     vr: np.ndarray, stride: np.ndarray, mask: np.ndarray) -> dict:
    """Биомеханика раздельно для медленных/быстрых участков, привязка к темпу (§3.1).

    Делим moving-точки по медиане темпа на «медленные» и «быстрые», по каждой
    группе — медианы биомех-метрик. Это и есть «привязка к темпу», без которой
    голое среднее GCT смешивает разминку/работу/заминку."""
    mv = mask & ~np.isnan(pace)
    if mv.sum() == 0:
        return {}
    med_pace = np.median(pace[mv])

    def grp(sel):
        def med(arr):
            a = arr[sel & ~np.isnan(arr)]
            return round(float(np.median(a)), 1) if a.size else None
        return {"cadence": med(cad), "gct": med(gct),
                "vert_ratio": med(vr), "stride": med(stride)}

    slow_sel = mv & (pace >= med_pace)   # больше сек/км = медленнее
    fast_sel = mv & (pace < med_pace)
    return {
        "median_pace_split_s_per_km": round(float(med_pace), 1),
        "slow": grp(slow_sel),
        "fast": grp(fast_sel),
    }


def _percentile_cell(values: np.ndarray, weights: np.ndarray) -> Optional[dict]:
    """{median, p25, p75} по значениям, взвешенным временем + seconds в ячейке.

    Перцентили, НЕ среднее/дисперсия (§3.5.1: форма, не момент — асимметричный
    дрейф под усталостью виден только по перцентилям; дисперсия его смазывает,
    один выброс её раздувает). Взвешивание по времени: точка, провисевшая дольше,
    весит больше — иначе прорежённый поток даст ложный перекос. seconds = сумма
    времени в ячейке (нужна aggregate для взвешивания бакета по периоду).
    Малое seconds НЕ режется (§3.5.2 определимость, не значимость) — отдаём как есть.
    """
    good = ~np.isnan(values) & ~np.isnan(weights)
    v, w = values[good], weights[good]
    if v.size == 0 or w.sum() <= 0:
        return None
    # взвешенные по времени перцентили: сортируем, идём по накопленному весу
    order = np.argsort(v)
    vs, ws = v[order], w[order]
    cum = np.cumsum(ws) - 0.5 * ws          # центры весовых интервалов
    cum /= ws.sum()
    def wp(q: float) -> float:
        return round(float(np.interp(q, cum, vs)), 1)
    return {"median": wp(0.5), "p25": wp(0.25), "p75": wp(0.75),
            "seconds": round(float(w.sum()), 1)}


def _bucketize(axis: np.ndarray, dt: np.ndarray, bucket: float,
               metrics: dict[str, np.ndarray]) -> dict:
    """Раскладывает точки по бакетам ОСИ (темп или HR) и в каждом считает
    {median,p25,p75,seconds} по каждой метрике. Общий движок обеих выжимок —
    меняются только ось и набор метрик (демаркация осей §3.5.1, формат един).

    Возвращает {bucket_int: {metric_name: {median,p25,p75,seconds} | None}}.
    Бакет существует, только если в нём есть валидная ось-точка (определимость) —
    пустых бакетов не плодим, но и не сглаживаем разреженность (§3.5.2).
    """
    out: dict[int, dict] = {}
    good_axis = ~np.isnan(axis)
    if good_axis.sum() == 0:
        return out
    lo = np.floor(np.nanmin(axis) / bucket) * bucket
    hi = np.ceil(np.nanmax(axis) / bucket) * bucket + bucket
    edges = np.arange(lo, hi, bucket)
    bucket_idx = np.full(axis.shape, -1, dtype=int)
    bucket_idx[good_axis] = np.clip(
        ((axis[good_axis] - lo) // bucket).astype(int), 0, len(edges) - 1)
    for k in range(len(edges)):
        sel = bucket_idx == k
        if not sel.any():
            continue
        cell = {}
        any_metric = False
        for name, arr in metrics.items():
            c = _percentile_cell(arr[sel], dt[sel])
            cell[name] = c
            if c is not None:
                any_metric = True
        if any_metric:
            out[int(edges[k])] = cell
    return out


def _lactate_from_watch(lact_points: list[dict], ts: np.ndarray,
                        hr: np.ndarray, pace: np.ndarray) -> list[dict]:
    """Метки с часов → калибровочные точки: подтягиваем hr/pace той секунды.

    Привязка по таймстампу превращает метку в тройку (лактат+пульс+темп) —
    готовое сырьё для калибровки порога (§5.3). hr_at_mark/pace_at_mark — то,
    чего НЕТ и не может быть у меток из комментария."""
    out = []
    for p in lact_points:
        t = p.get("timestamp_ms")
        hr_at = pace_at = None
        if t is not None and ts.size:
            # ближайшая секунда потока к таймстампу метки
            k = int(np.argmin(np.abs(ts - float(t))))
            if not np.isnan(hr[k]):
                hr_at = round(float(hr[k]), 1)
            if k < pace.size and not np.isnan(pace[k]):
                pace_at = round(float(pace[k]), 1)
        out.append({
            "mmol": p.get("mmol"),
            "timestamp_ms": t,
            "lap": p.get("lap"),
            "hr_at_mark": hr_at,
            "pace_at_mark": pace_at,
        })
    return out


def histogram_shape(hist: dict) -> dict:
    """Сворачивает гистограмму {бакет: секунды} в ФОРМУ (МЕТОД §3.1), не среднее.

    Возвращает:
      modality: 'mono' | 'multi' | 'diffuse' | 'empty'
      shelves: [{center, share}] — полки, собранные РОСТОМ ОТ ПИКА (граница = впадина).
               center взвешен по массе, share = сумма ВСЕЙ собранной массы полки.
      spread: размах занятых бакетов

    Сбор от пика: берём наивысший бакет, идём в обе стороны пока склон спадает,
    стоп на впадине (бакет пошёл вверх) или ниже шумового пола. Доля = сумма массы,
    не пиковый бакет — иначе широкая полка ложно читается как diffuse.
    """
    if not hist:
        return {"modality": "empty", "shelves": [], "spread": None}
    items = sorted(((int(k), float(v)) for k, v in hist.items()), key=lambda x: x[0])
    buckets = [b for b, _ in items]
    secs = [s for _, s in items]
    total = sum(secs)
    if total <= 0:
        return {"modality": "empty", "shelves": [], "spread": None}

    spread = buckets[-1] - buckets[0]
    noise = SHELF_NOISE_FLOOR_SHARE * total
    used = [False] * len(secs)
    shelves = []

    while True:
        # самый высокий ещё не занятый бакет выше шумового пола = пик новой полки
        peak = -1
        peak_val = -1.0
        for i in range(len(secs)):
            if not used[i] and secs[i] > noise and secs[i] > peak_val:
                peak_val = secs[i]
                peak = i
        if peak < 0:
            break

        members = [peak]
        used[peak] = True
        # вправо: пока спадает и не занято и выше пола
        prev = secs[peak]
        j = peak + 1
        while j < len(secs) and not used[j] and secs[j] <= prev and secs[j] > noise:
            used[j] = True
            members.append(j)
            prev = secs[j]
            j += 1
        # влево: симметрично
        prev = secs[peak]
        j = peak - 1
        while j >= 0 and not used[j] and secs[j] <= prev and secs[j] > noise:
            used[j] = True
            members.append(j)
            prev = secs[j]
            j -= 1

        mass = sum(secs[k] for k in members)
        center = sum(buckets[k] * secs[k] for k in members) / mass
        # концентрация: доля массы полки в ядре center ± SHELF_CORE_HALFWIDTH
        core_mass = sum(secs[k] for k in members
                        if abs(buckets[k] - center) <= SHELF_CORE_HALFWIDTH)
        member_spread = buckets[max(members)] - buckets[min(members)]
        concentration = core_mass / mass if mass > 0 else 1.0
        # широкая полка с низкой концентрацией = плато (прогрессив), не горб
        is_plateau = (member_spread > SHELF_WIDE_GUARD_SPREAD
                      and concentration < SHELF_CORE_CONCENTRATION)
        shelves.append({
            "center": int(round(center)),
            "share": round(mass / total, 2),
            "_plateau": is_plateau,
        })

    # только значимые полки
    sig = [s for s in shelves if s["share"] >= SHELF_MIN_SHARE]
    sig.sort(key=lambda s: s["share"], reverse=True)

    any_plateau = any(s.get("_plateau") for s in sig)
    # чистим служебное поле из выдачи
    for s in sig:
        s.pop("_plateau", None)

    if len(sig) == 0:
        modality = "diffuse"
    elif len(sig) == 1:
        # одна полка: mono только если концентрирована (не плато) и держит ≥ доли;
        # размазанный прогрессив собирается в одну «полку», но это плато → diffuse
        if any_plateau or sig[0]["share"] < SHELF_MONO_SHARE:
            modality = "diffuse"
        else:
            modality = "mono"
    else:
        modality = "multi"
    return {"modality": modality, "shelves": sig, "spread": spread}


def enrich_activity(
    stream: dict,
    *,
    lactate_watch_points: Optional[list[dict]] = None,
    lactate_comment_values: Optional[list[float]] = None,
) -> dict:
    """Главная функция: сырой поток → числовые характеристики (МЕТОД §3.1).

    lactate_watch_points  — из backend.get_activity_lactate()['points'] (с таймстампом)
    lactate_comment_values — из backend.parse_lactate(описание) (значения из 'LA:x.x')
    Лактат необязателен: его отсутствие штатно (None), не ошибка.
    """
    rows = stream.get("activityDetailMetrics") or []
    descs = stream.get("metricDescriptors") or []

    # Поток отсутствует (ручная запись/импорт без посекундных данных, detailsAvailable=false).
    # Это ШТАТНО, не ошибка: возвращаем валидное «пустое» обогащение с пометкой,
    # а не падаем. Каталожные поля (дистанция, max из сводки) у такой тренировки
    # всё равно есть; гистограммы/кластеры/биомеханика — None, потому что считать не из чего.
    if not rows or not descs:
        return {
            "algo_version": ALGO_VERSION,
            "moving_time_s": None,
            "hr_histogram": {},
            "pace_histogram": {},
            "clusters": [],
            "pace_variance": None,
            "hr_variance": None,
            "median_crossings": 0,
            "biomech_by_pace": {},
            "biomech_by_pace_bucket": {},
            "pace_by_hr_bucket": {},
            "lactate_marks": None,
            "elevation": {"gain_m": None, "loss_m": None},
            "max_hr": None,
            "no_stream": True,   # пометка: детальный поток отсутствовал
        }

    idx = _index_map(stream)

    ts = _column(rows, idx.get(K_TS))
    speed = _column(rows, idx.get(K_SPEED))      # СЫРАЯ скорость для moving-time
    hr = _column(rows, idx.get(K_HR))
    cad = _column(rows, idx.get(K_CAD))
    gct = _column(rows, idx.get(K_GCT))
    vr = _column(rows, idx.get(K_VR))
    stride = _column(rows, idx.get(K_STRIDE))
    elev = _column(rows, idx.get(K_ELEV))

    # шаг времени между сэмплами (для взвешивания; обычно ~1с, но бывает разрежение)
    if ts.size > 1:
        dt = np.diff(ts, prepend=ts[0]) / 1000.0
        dt[dt <= 0] = 1.0
        dt[np.isnan(dt)] = 1.0
    else:
        dt = np.ones(ts.size)

    mask = _moving_mask(speed, dt)
    pace = _pace_s_per_km(speed)

    # всё считаем на moving-time
    mv = mask if mask.size == hr.size else np.ones(hr.size, dtype=bool)

    # отсев битого пульса по рассинхрону (только для HR — pace/moving не трогаем).
    # hr_clean = пульс с NaN там, где датчики противоречат → выпадает из гистограммы/кластеров.
    hr_valid = _hr_valid_mask(hr, ts)
    hr_clean = hr.copy()
    hr_clean[~hr_valid] = np.nan

    hr_mv, pace_mv, dt_mv = hr_clean[mv], pace[mv], dt[mv]

    moving_time_s = float(np.nansum(dt[mv])) if mv.any() else 0.0
    elev_gain = elev_loss = None
    if elev[~np.isnan(elev)].size > 1:
        de = np.diff(elev[~np.isnan(elev)])
        elev_gain = round(float(np.sum(de[de > 0])), 1)
        elev_loss = round(float(-np.sum(de[de < 0])), 1)

    # лактат раздельно по источнику (НЕ схлопываем; count по watch)
    watch = _lactate_from_watch(lactate_watch_points or [], ts, hr, pace)
    comment = [{"mmol": v, "ref_text": None, "lap_hint": None}
               for v in (lactate_comment_values or [])]
    lactate_marks = None
    if watch or comment:
        lactate_marks = {"from_watch": watch, "from_comment": comment,
                         "count_watch": len(watch)}

    return {
        "algo_version": ALGO_VERSION,
        "moving_time_s": round(moving_time_s, 1),
        "hr_histogram": _histogram(hr_mv, dt_mv, HR_BUCKET_BPM),
        "pace_histogram": _histogram(pace_mv, dt_mv, PACE_BUCKET_S_PER_KM),
        "clusters": _clusters(pace_mv, hr_mv, dt_mv),
        "pace_variance": (round(float(np.nanvar(pace_mv)), 2)
                          if pace_mv[~np.isnan(pace_mv)].size else None),
        "hr_variance": (round(float(np.nanvar(hr_mv)), 2)
                        if hr_mv[~np.isnan(hr_mv)].size else None),
        "median_crossings": _median_crossings(pace_mv),
        "biomech_by_pace": _biomech_by_pace(pace, cad, gct, vr, stride, mask),
        "biomech_by_pace_bucket": _bucketize(
            pace[mv], dt[mv], PACE_BIOMECH_BUCKET_S_PER_KM,
            {"gct": gct[mv], "vert_ratio": vr[mv], "stride": stride[mv]},
        ) if mv.any() else {},
        "pace_by_hr_bucket": _bucketize(
            hr_clean[mv], dt[mv], HR_PACE_BUCKET_BPM, {"pace": pace[mv]},
        ) if mv.any() else {},
        "lactate_marks": lactate_marks,
        "elevation": {"gain_m": elev_gain, "loss_m": elev_loss},
        "max_hr": (int(round(float(np.nanpercentile(hr_clean, MAX_HR_PERCENTILE))))
                   if hr_clean[~np.isnan(hr_clean)].size else None),
    }


if __name__ == "__main__":
    # самопроверка на синтетике (без сети): интервалка пила + стоянка
    rng = np.random.default_rng(0)
    n = 600
    # 0..200 быстрый бег (полка пульса ~185 с микровариацией — РЕАЛЬНЫЙ высокий пульс),
    # 200..260 СТОЯНКА, 260..600 трусца
    speed = np.concatenate([
        np.full(200, 4.5) + rng.normal(0, 0.1, 200),    # ~3:42/км
        np.full(60, 0.2),                                # стоянка >4с
        np.full(340, 2.6) + rng.normal(0, 0.1, 340),    # ~6:25/км трусца
    ])
    # полка 185 с микровариацией ±2 = реальный высокий пульс (подтверждён соседями)
    hr = np.concatenate([
        np.round(np.full(200, 185.0) + rng.normal(0, 1.5, 200)),
        np.full(60, 150.0),
        np.full(340, 135.0),
    ])
    # одиночный шип 210 (НЕ режется фильтром — в человеческом диапазоне), но
    # 99-й перцентиль max_hr его проглотит (робастность на выходе, не отсев на входе)
    hr[300] = 210.0
    hr[299] = np.nan
    hr[301] = np.nan
    # залипание: ЧЕТЫРЕ идентичных 205 подряд — stuck-value, режется
    hr[450] = hr[451] = hr[452] = hr[453] = 205.0
    cad = np.concatenate([np.full(200, 188.0), np.full(60, 0.0), np.full(340, 168.0)])
    ts = (np.arange(n) * 1000.0).astype(float)

    def md(key, i):
        return {"key": key, "metricsIndex": i}
    stream = {
        "metricDescriptors": [md("directTimestamp", 0), md("directSpeed", 1),
                              md("directHeartRate", 2), md("directRunCadence", 3)],
        "activityDetailMetrics": [
            {"metrics": [ts[k], speed[k], hr[k], cad[k]]} for k in range(n)
        ],
    }
    res = enrich_activity(
        stream,
        lactate_watch_points=[{"timestamp_ms": 150000.0, "mmol": 6.1, "lap": 3}],
        lactate_comment_values=[6.1],
    )
    import json
    print(json.dumps(res, ensure_ascii=False, indent=2))
    # проверки
    assert res["moving_time_s"] < 600, "стоянка должна вырезаться из moving-time"
    assert res["moving_time_s"] >= 540, res["moving_time_s"]  # ~540 = 600-60
    assert res["lactate_marks"]["count_watch"] == 1
    assert 182 <= res["lactate_marks"]["from_watch"][0]["hr_at_mark"] <= 188, \
        "лактат с часов должен подтянуть пульс той секунды (полка ~185)"
    assert res["median_crossings"] > 0, "пила должна дать переходы через медиану"
    # полка 185 (200 сэмплов) доминирует; одиночный 210 НЕ срезан фильтром, но
    # 99-й перцентиль его игнорирует; залипание 205×4 срезано. max_hr ≈ полка 185-189.
    assert 184 <= res["max_hr"] <= 191, \
        f"перцентиль держит полку 185, игнорит одиночный 210, max_hr={res['max_hr']}"

    # --- enrich-0.3.0: бакетные выжимки ---
    # pace_by_hr_bucket: ось=HR, в потоке есть pace и hr → должен заполниться.
    pbh = res["pace_by_hr_bucket"]
    assert pbh, "pace_by_hr_bucket должен считаться (есть pace+hr)"
    # секунды HR-бакетов сходятся с hr-ГИСТОГРАММОЙ (та же чистка пульса: hr_clean),
    # а НЕ с moving-time — пульс отфильтрован stuck/вне-диапазона, темп нет.
    # Это согласованность с тем, как считаются гистограмма и кластеры.
    tot = sum(c["pace"]["seconds"] for c in pbh.values() if c.get("pace"))
    hist_tot = sum(res["hr_histogram"].values())
    assert abs(tot - hist_tot) < 5, \
        f"секунды HR-бакетов ({tot}) должны сходиться с hr-гистограммой ({hist_tot})"
    # формат ячейки: median/p25/p75/seconds, p25<=median<=p75
    cell = next(c["pace"] for c in pbh.values() if c.get("pace") and c["pace"]["seconds"] > 10)
    assert cell["p25"] <= cell["median"] <= cell["p75"], f"перцентили не упорядочены: {cell}"
    # быстрый бег (~185 уд) должен лечь в темп заметно быстрее трусцы — связка HR↔темп жива
    fast_buckets = [int(k) for k in pbh if int(k) >= 180]
    slow_buckets = [int(k) for k in pbh if int(k) <= 140]
    if fast_buckets and slow_buckets:
        fast_pace = min(pbh[str(k)]["pace"]["median"] for k in fast_buckets)
        slow_pace = max(pbh[str(k)]["pace"]["median"] for k in slow_buckets)
        assert fast_pace < slow_pace, "на высоком HR темп должен быть быстрее (меньше сек/км)"
    # biomech_by_pace_bucket: в этом потоке биомеханики нет → бакеты есть (ось=темп
    # определима), но метрики внутри None. Это штатно, не ошибка.
    bbp = res["biomech_by_pace_bucket"]
    assert isinstance(bbp, dict), "biomech_by_pace_bucket должен быть dict даже без датчика"
    if bbp:
        any_cell = next(iter(bbp.values()))
        assert "gct" in any_cell, "формат бакета биомеханики: gct/vert_ratio/stride"
        assert any_cell["gct"] is None, "без датчика gct в бакете = None (нет данных)"
    print("\nself-test OK")
