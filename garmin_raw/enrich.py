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

import re
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
#        (ось=пульс). [0.3.0 хранил квантили median/p25/p75 — заменено в 0.4.0.]
# 0.4.0: формат бакета изменён на СКЛАДЫВАЮЩУЮСЯ достаточную статистику
#        {sum, sum_sq, seconds} вместо квантилей. Причина: квантили НЕ объединяются
#        взвешенно по периоду (median пула ≠ Σ wᵢ·medianᵢ), aggregate не мог бы
#        честно собрать квартальный разброс HR-бакета. Суммы аддитивны — складываются
#        точно. Центр = sum/seconds, разброс = √(sum_sq/seconds − центр²); оба
#        выводятся на чтении (не фиксируются). Подробности — в _suffstat_cell.
# 0.5.0: per-activity decoupling и hr_recovery (streams/laps-зависимые поля для
#        aggregate, этап 5). laps добавлены в сигнатуру как второе несущее сырьё.
#        [0.5.0 имел порог pace_not_steady — отсекал 88% (порог на континууме,
#        §2.5). Заменено в 0.5.1.]
# 0.5.1: decoupling без порога ровности — считается ВСЕГДА (континуум pace_variance,
#        порог произволен §2.5; «ровность» — суждение LLM §3.5.2). Самомаркируется
#        ПАРОЙ фактов: pace_variance (ловит пилу) + pace_1st_half/pace_2nd_half (ловят
#        прогрессив — direction, который variance не выдаёт; иначе decoupling врёт
#        правдоподобно на разгоне, путая его с дрейфом базы). hr_recovery: различены
#        no_laps / single_lap / no_fast_laps (три факта, не схлопывать).
# 0.6.0: hr_source из потока (наличие GCT-balance → chest, иначе unknown; §3.5.1).
#        balance физически только с нагрудника → chest НАДЁЖЕН; отсутствие = unknown
#        (НЕ optical — нечем подтвердить, прямого поля сенсора Garmin нет). chest —
#        чистая но неполная подгруппа (сравнение пульса внутри неё надёжно); unknown —
#        смесь. Переход chest→unknown ≠ смена железа (суждение LLM, не коннектора).
#        Поднимается в каталог через put_enriched (как max_hr) → by_source оживает.
# 0.6.1: формулы НЕ менялись. Bump — восстановление enrich-owned полей каталога
# (hr_source/moving_time_s/max_hr), стёртых wipe-багом upsert (fix 7.6-2a'):
# hr_source в enriched-строках не хранится, только поднимается в каталог put_enriched;
# перезапись той же версии запрещена (этап 6) -> честный путь восстановления = bump.
ALGO_VERSION = "enrich-0.6.3"

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
# Значение бакета несёт СКЛАДЫВАЮЩУЮСЯ достаточную статистику {sum, sum_sq, seconds}
# (взвешено по времени), НЕ квантили: квантили не объединяются по периоду, суммы —
# объединяются точно. Центр (sum/seconds) и разброс (√дисперсии пула) выводятся на
# чтении; разброс — флаг достоверности центра (широкий → LLM не берёт точку), не
# физиологический сигнал. Подробности и обоснование — в _suffstat_cell.
# Метки hr_source НЕТ — тренировка вся от одного источника; разбивку by_source делает
# aggregate по каталогу (демаркация §1.1). Малое seconds в бакете НЕ фильтруется
# (§3.5.2: определимость, не значимость) — отдаём как есть, «достаточно ли» решает LLM.
PACE_BIOMECH_BUCKET_S_PER_KM = 15  # ширина темп-бакета биомеханики (старт; бьётся с pace-гистограммой)
HR_PACE_BUCKET_BPM = 5             # ширина HR-бакета для pace_by_hr (как hr-гистограмма)

# --- Параметры decoupling / hr_recovery (enrich-0.5.0; ТЗ §3.5, МЕТОД §5.4) -----
# decoupling — механический ratio (2-я половина / 1-я). БЕЗ порога ровности:
#   распределение pace_variance непрерывно (нет «ровные/рваные»), порог произволен
#   (§2.5); «достаточно ли ровная» — суждение LLM (§3.5.2). Считается всегда, где
#   есть две половины. Самомаркировка — парой фактов: pace_variance (ловит пилу) +
#   pace_1st/2nd_half (ловят прогрессив, который variance не выдаёт). Без имени.
# hr_recovery — падение HR после кругов быстрее медианы темпа. Структурное окно
#   (границы кругов из laps, НЕ зашитые 60с — §2.5), робастные края из streams
#   (медиана HR последних N секунд, НЕ maxHR-пик — §2.4: единственная точка хрупка).
#   Стартовый HR пульса считается по WALL-CLOCK (не moving): пульс реален и в паузе
#   после работы — там и начинается восстановление; moving-маска выкинула бы ровно
#   ту точку, что recovery измеряет. duration едет фактом рядом с drop (несравнимость
#   кругов разной длины — суждение LLM §3.5.2, не нормировка коннектора).
#   intensityType Garmin (INTERVAL/REST) ИГНОРИРУЕТСЯ — чужой ярлык (§2.2);
#   рабочие круги отбираются механически по темпу.
#   Различаем no_laps (нет в кэше) / single_lap (один круг, структуры нет) /
#   no_fast_laps (круги есть, быстрее медианы нет) — три РАЗНЫХ факта (§3.5.2).
HR_RECOVERY_EDGE_WINDOW_S = 8.0   # окно медианы края recovery (сек); старт 5-10, на калибровку

# --- Резолвер лактат-меток через MCP (этап 7; ТЗ §6.1, QA этап7 Q3/Q4) ----------
# Единый резолвер намерения (at_time|user_ref) → секунда потока → {lap,hr_at,pace_at}.
# Дёргается ОДНОЙ функцией из recompute (батч) и add_lactate (немедленно, одна метка):
# немедленная строка обязана совпадать с той, что дал бы recompute текущей версии.
#
# Критерий определимости — НЕ target∈[ts0,ts_last], а БЛИЗОСТЬ ближайшей секунды:
# min(|ts−target|) ≤ допуск. Это ловит и края, и ВНУТРЕННИЕ ДЫРЫ (пауза записи в
# рваной пробежке: target «в диапазоне», но ближайшая секунда в десятках сек → None,
# а не молчаливый argmin к далёкой секунде). Вне допуска → None → раствор не пишем →
# метка остаётся pending (честно «привязка не определима»).
#
# Допуск АСИММЕТРИЧЕН по региону (симметричный узаконил бы невалидный случай):
#   правый край (target > ts_last): замер ПОСЛЕ стопа (встал, выдохнул, уколол) —
#       щедрый, привязка к последней секунде валидна (только что бежал, hr осмыслен);
#   левый край (target < ts0): замер ДО старта записи (базовый лактат в покое) —
#       жёсткий/нулевой: hr первой БЕГОВОЙ секунды для покойного замера семантически
#       неверен → лучше None, чем пульс разбега;
#   внутри [ts0, ts_last]: допуск на дыру — разреженность потока штатна (~1/2-3с),
#       но настоящая пауза (десятки сек без сэмпла) → None.
# Все три — версионируемы (при смене резолвера/окна → bump ALGO_VERSION, recompute
# перепишет раствор). Сегодня версия-независимы (_resolver_tolerances игнорит version).
LACTATE_TOL_RIGHT_S = 15.0   # правый край: замер после стопа (щедрый)
LACTATE_TOL_LEFT_S = 2.0     # левый край: до старта записи (жёсткий; hr разбега ≠ покой)
LACTATE_TOL_GAP_S = 8.0      # внутренняя дыра: > разреженности потока, < настоящей паузы

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
# GCT-balance (L/R баланс фазы контакта) пишется Garmin ТОЛЬКО с нагрудного датчика
# с акселерометром (HRM-Pro/Run) — оптика запястья его физически не даёт. Прямой,
# детерминированный признак нагрудника → hr_source=chest (enrich-0.6.0, §3.5.1).
K_GCT_BALANCE = "directGroundContactBalanceLeft"

# biomech_source (T7.6-2b): признак присутствия Stryd footpod в потоке.
# ФАКТ (проверено на двух классах — Stryd-активность vs trail без Stryd-датафилда):
# Stryd пишет developer-поля с этим appID в metricDescriptors. GCT/vert-ratio/stride
# приходят как НАТИВНЫЕ Garmin-поля (appID=None) И с часов (watch-only), И со Stryd —
# наличие GCT само по себе НЕ различает источник (has_biomech_sensor слеп к провенансу).
# Различитель — присутствие Stryd-appID: есть → footpod пишет biomech (foot-pod);
# нет → биомеханика от встроенного акселерометра часов (watch-only).
# Это признак ПРИСУТСТВИЯ Stryd, не источника каждого поля — но для провенанса достаточно
# (Stryd подключён = biomech идёт от него). Пороги отсутствуют (наличие appID, как
# hr_source по наличию balance — механический перевод факта в категорию, не суждение).
_STRYD_APPID = "18fb2cf0-1a4b-430d-ad66-988c847421f4"

# gps_type (T7.6-2b): GPS-СРЕДА из декларации пользователя (sport/typeKey Garmin).
# ДОВЕРЯЕМ РАЗМЕТКЕ — механический перевод словаря номенклатуры, НЕ инференс из данных
# (темп/lap_count не используются: вывод среды из сигнала заменил бы декларацию догадкой).
# Пять значений; indoor ОТДЕЛЬНО от treadmill (не схлопывать): дорожка несёт бегуна
# belt-assist, пол — нет → систематически разные GCT/vert-ratio (МЕТОД §5.4 межгодовое
# сравнение). Схлопывание = невосстановимая потеря (typeKey свёрнут при записи, recompute
# не вернёт). indoor ('без GPS, не дорожка') ≠ None ('typeKey не распознан').
_GPS_TYPE_BY_SPORT = {
    "running": "outdoor",
    "trail_running": "outdoor",
    "treadmill_running": "treadmill",
    "track_running": "track",       # круговой GPS с систематической погрешностью дистанции
    "indoor_running": "indoor",
}


def _gps_type_from_sport(sport: Optional[str]) -> Optional[str]:
    """typeKey → gps_type по словарю декларации. Неизвестный/None typeKey → None
    ('не распознан', не гадаем — расширяемо новым typeKey). Механический перевод, не
    суждение (пороги отсутствуют)."""
    if sport is None:
        return None
    return _GPS_TYPE_BY_SPORT.get(sport)   # неизвестный → None


def _index_map(stream: dict) -> dict[str, int]:
    """key → metricsIndex по metricDescriptors."""
    out: dict[str, int] = {}
    for m in (stream.get("metricDescriptors") or []):
        k = m.get("key")
        if k is not None:
            out[k] = m["metricsIndex"]
    return out


def _hr_source_from_stream(idx: dict, has_hr: bool) -> str:
    """Определяет hr_source по наличию GCT-balance в потоке (enrich-0.6.0, §3.5.1).

    ФИЗИКА признака: GCT-balance (L/R) Garmin пишет ТОЛЬКО с нагрудного датчика с
    акселерометром (HRM-Pro/Run). Оптика запястья его не даёт. Поэтому:
      balance в потоке есть → chest (НАДЁЖНО: физически только нагрудник)
      balance нет           → unknown (НЕ optical! — нечем подтвердить: может быть
                              оптика ИЛИ простой нагрудник без акселерометра ИЛИ
                              нагрудник-пульс со Stryd-biomech. Прямого поля сенсора
                              Garmin не отдаёт — проверено на архиве.)
      нет пульса вообще     → unknown

    СЕМАНТИКА ГРУПП (критично для LLM, чтобы не строить ложную границу в §5.4):
    - chest — ЧИСТАЯ, но НЕПОЛНАЯ подгруппа: где balance есть, там точно нагрудник →
      сравнение пульса ВНУТРИ chest надёжно. НО не все нагрудные сюда попадают (Stryd
      мог перебить biomech-источник → chest-пульс уехал в unknown).
    - unknown — СМЕСЬ (оптика / нагрудник-без-balance / chest-со-Stryd). «Не знаю»,
      НЕ «оптика» и НЕ однородная группа.
    - Переход chest→unknown во времени ≠ смена железа (unknown мог быть тем же
      нагрудником без balance). §5.4-«граница смены железа» надёжна ТОЛЬКО внутри
      chest-группы; chest↔unknown — не граница, а «дальше не знаю» (суждение LLM,
      коннектор кладёт факт).
    """
    if not has_hr:
        return "unknown"
    if K_GCT_BALANCE in idx:
        return "chest"
    return "unknown"


def _biomech_source_from_stream(descs: list, has_biomech: bool) -> Optional[str]:
    """Определяет biomech_source по присутствию Stryd-appID в metricDescriptors
    (enrich-0.6.2, T7.6-2b). ФАКТ (два класса: Stryd-активность vs trail без Stryd):
      Stryd-appID есть  → 'foot-pod' (biomech пишет footpod Stryd);
      Stryd-appID нет,
        но biomech есть  → 'watch-only' (GCT/vert-ratio от встроенного акселерометра);
      biomech нет вообще → None (нечего атрибутировать).

    'run-pod' (TZ-словарь) НЕ эмитится: наблюдаемых образцов нет (нагрудный biomech-
    датчик отдельным appID в архиве не встретился). Эмитим только ДВА наблюдаемых
    класса — не выдумываем третий без образца. Признак — наличие appID (механический
    перевод, не суждение; пороги отсутствуют, как hr_source по balance).

    ВАЖНО: watch-only даёт GCT/vert-ratio, но НЕ GCT-balance (L/R) — balance физически
    только с нагрудника. foot-pod (Stryd) тоже перебивает balance → на Stryd-активности
    balance нет, hr_source уезжает в unknown (см. _hr_source_from_stream). biomech_source
    ='foot-pod' восстанавливает для LLM провенанс: unknown+foot-pod может быть chest+Stryd,
    не оптика."""
    if not has_biomech:
        return None
    for m in descs:
        if m.get("appID") == _STRYD_APPID:
            return "foot-pod"
    return "watch-only"


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


def _suffstat_cell(values: np.ndarray, weights: np.ndarray) -> Optional[dict]:
    """Складывающаяся достаточная статистика бакета, ВЗВЕШЕННАЯ по времени.

    Отдаёт {sum, sum_sq, seconds}, НЕ квантили. Почему так (длинная калибровка
    метода, итог обсуждения этапа 5):
    - Квантили (median/p25/p75) НЕ объединяются взвешенно по периоду: median(пула)
      ≠ Σ wᵢ·medianᵢ. Чтобы aggregate честно собрал КВАРТАЛЬНЫЙ разброс HR-бакета,
      нужна аддитивная структура. Суммы аддитивны — складываются точно.
    - Полная саб-гистограмма дала бы и центр, и форму, но это 2D pace×hr таблица
      (квадратичный размер) ради разрешения, которое методу НЕ нужно: pace_by_hr_grid
      — сетка ТОЧЕК («темп на HR=X»), LLM читает центр + флаг ширины, не форму внутри.
    - {sum, sum_sq, seconds} даёт И центр (sum/seconds = взвеш. среднее), И разброс
      (√(sum_sq/seconds − центр²) = std пула) — оба складываются. Центр СМЕЩЁН на
      бимодальных бакетах (среднее смеси «разминка+разгон» ложится между горбами),
      НО там же широкий std → LLM предупреждён «не установившийся режим, не беру».
      Смещение само-маркируется разбросом: где центр врёт, там std широкий. На узких
      бакетах (честная аэробная точка) среднее ≈ медиана — центр честен там, где берётся.
    - Дисперсия здесь — ФЛАГ ШУМА, не физиологический сигнал (роль p25/p75 из прошлой
      редакции: информирует, не фильтрует). Для гистограммы пульса форма остаётся сутью
      (полки §5.3), но ВНУТРИ HR-бакета сетки методу нужен центр+ширина, не форма (§2.3:
      представление — свойство метрики, не общее правило).
    Взвешивание по времени (value·dt): точка, провисевшая дольше, весит больше —
    иначе прорежённый поток даёт ложный перекос. Никаких median/p25/p75 здесь НЕ
    фиксируем — это производные, выводятся на чтении из достаточной статистики.
    Малое seconds НЕ режется (§3.5.2 определимость, не значимость) — отдаём как есть.
    """
    good = ~np.isnan(values) & ~np.isnan(weights)
    v, w = values[good], weights[good]
    if v.size == 0 or w.sum() <= 0:
        return None
    sec = float(w.sum())
    s = float(np.sum(v * w))           # Σ value·dt   (взвешено по времени)
    s2 = float(np.sum((v ** 2) * w))   # Σ value²·dt
    return {"sum": round(s, 3), "sum_sq": round(s2, 3), "seconds": round(sec, 1)}


def _bucketize(axis: np.ndarray, dt: np.ndarray, bucket: float,
               metrics: dict[str, np.ndarray]) -> dict:
    """Раскладывает точки по бакетам ОСИ (темп или HR) и в каждом считает
    складывающуюся достаточную статистику {sum,sum_sq,seconds} по каждой метрике.
    Общий движок обеих выжимок — меняются только ось и набор метрик (§3.5.1).

    Возвращает {bucket_int: {metric_name: {sum,sum_sq,seconds} | None}}.
    Структура аддитивна: aggregate складывает одноимённые бакеты по периоду
    суммированием sum/sum_sq/seconds — квартальный центр и разброс выводятся точно.
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
            c = _suffstat_cell(arr[sel], dt[sel])
            cell[name] = c
            if c is not None:
                any_metric = True
        if any_metric:
            out[int(edges[k])] = cell
    return out


def _decoupling(hr_mv: np.ndarray, pace_mv: np.ndarray, dt_mv: np.ndarray,
                pace_variance: Optional[float], duration_s: float) -> dict:
    """Механический ratio пульс/скорость: (2-я половина) / (1-я) по moving-времени.

    БЕЗ ПОРОГА ровности (решение этапа 5). Раньше считался только при низкой
    pace_variance — но распределение pace_variance НЕПРЕРЫВНО (нет провала
    «ровные/рваные»), любой порог на континууме произволен (§2.5), а «достаточно ли
    ровная тренировка, чтобы decoupling осмыслен» — ЗНАЧИМОСТЬ, суждение LLM (§3.5.2).
    Поэтому decoupling считается ВСЕГДА, где есть две половины с данными
    (определимость механическая), а ровность судит LLM по приложенным фактам.

    САМОМАРКИРОВКА требует ДВУХ фактов, не одного — pace_variance НЕДОСТАТОЧЕН:
    - pace_variance ловит ПИЛУ (скачки темпа): высокий → LLM не берёт decoupling;
    - НО прогрессив (плавный разгон 5:30→4:30) даёт УМЕРЕННЫЙ variance и при этом
      2-я половина систематически быстрее → decoupling меряет РАЗНИЦУ ТЕМПА, не дрейф
      пульса, и врёт правдоподобно («база уехала», хотя человек просто ускорился).
      variance это НЕ выдаёт (прогрессив направленный, не шумный).
    - поэтому отдаём ещё pace_1st_half/pace_2nd_half: если 2-я заметно быстрее, LLM
      видит «темп между половинами не держался → decoupling про разгон, не дрейф».
    Разброс (пила) + тренд (прогрессив) — две ортогональные оси артефакта. Коннектор
    отдаёт обе как факты, НЕ отсекает; «осмыслен ли decoupling как дрейф» — LLM.

    value = ratio HR-на-скорость 2-й половины к 1-й. Рост = пульс выше при той же
    скорости. Имя «база держит/едет» НЕ вешаем — это LLM.
    """
    base = {"value": None, "pace_1st_half": None, "pace_2nd_half": None,
            "pace_variance": pace_variance, "duration_s": round(duration_s, 1)}
    good = ~np.isnan(hr_mv) & ~np.isnan(pace_mv) & (pace_mv > 0)
    hr_g, pace_g, dt_g = hr_mv[good], pace_mv[good], dt_mv[good]
    if hr_g.size < 4 or dt_g.sum() <= 0:
        return {**base, "reason": "too_few_points"}
    # делим по накопленному ВРЕМЕНИ пополам (не по числу точек — поток прорежен)
    cum = np.cumsum(dt_g)
    half_t = cum[-1] / 2.0
    first = cum <= half_t
    second = ~first
    if not first.any() or not second.any():
        return {**base, "reason": "too_few_points"}

    def whr(sel):  # взвешенный по времени HR половины
        w = dt_g[sel]; return float(np.sum(hr_g[sel] * w) / w.sum())
    def wpace(sel):  # взвешенный по времени темп половины (сек/км)
        w = dt_g[sel]; return float(np.sum(pace_g[sel] * w) / w.sum())

    hr1, hr2 = whr(first), whr(second)
    p1, p2 = wpace(first), wpace(second)
    # ratio = HR / скорость(м/с). pace сек/км → скорость = 1000/pace.
    r1 = hr1 / (1000.0 / p1)
    r2 = hr2 / (1000.0 / p2)
    if r1 <= 0:
        return {**base, "reason": "too_few_points"}
    decoup = (r2 - r1) / r1
    return {
        "value": round(float(decoup), 4),
        "pace_1st_half": round(p1, 1),   # факт: темп половин — ловит прогрессив
        "pace_2nd_half": round(p2, 1),   # (2-я заметно быстрее → decoupling про разгон, не дрейф)
        "pace_variance": pace_variance,  # факт: пила или нет
        "duration_s": round(duration_s, 1),
        "reason": "ok",
    }


def _hr_recovery(laps: Optional[dict], ts: np.ndarray, hr_clean: np.ndarray,
                 pace: np.ndarray, speed: np.ndarray) -> dict:
    """Падение HR после рабочих кругов (быстрее медианы темпа), структурное окно.

    Дизайн (длинная калибровка, итог обсуждения этапа 5):
    - ОКНО СТРУКТУРНОЕ: границы рабочего и следующего круга из laps. НЕ зашитые 60с
      (§2.5: абсолютный параметр, «почему 60 а не 90» не имеет якорь-нейтрального
      ответа). Recovery меряется за реальную длительность восстановительного круга.
    - РОБАСТНЫЕ КРАЯ из streams: стартовый HR = медиана HR последних
      HR_RECOVERY_EDGE_WINDOW_S секунд рабочего круга (НЕ maxHR-пик — §2.4: единственная
      точка хрупка, артефактный шип на оптике задрал бы падение). Конечный HR = медиана
      последних N секунд восстановительного круга (НЕ averageHR всего круга — размазан
      по падению).
    - WALL-CLOCK, не moving: пульс реален и в паузе после работы, там начинается
      восстановление; moving-маска выкинула бы ровно эту точку.
    - intensityType ИГНОРИРУЕТСЯ (§2.2 чужой ярлык): рабочие круги по темпу.
    - ОПРЕДЕЛИМОСТЬ (§3.5.2): нужны круги быстрее медианы темпа. no_laps (нет в кэше,
      чинится дозакачкой) vs no_fast_laps (нет быстрых кругов, честная неопределимость)
      — РАЗЛИЧАЮТСЯ, не схлопываются в один null.
    - Коннектор отдаёт drop + duration как ФАКТЫ, НЕ нормирует и НЕ судит «быстро ли».
      Несравнимость кругов разной длины — суждение LLM: duration едет рядом с drop.
    """
    if not laps:
        return {"events": [], "reason": "no_laps"}
    lap_list = laps.get("lapDTOs") or []
    if len(lap_list) == 0:
        return {"events": [], "reason": "no_laps"}
    if len(lap_list) < 2:
        # один круг = непрерывный бег без разметки. laps ЕСТЬ, но структуры для
        # recovery нет. Это НЕ «нет данных» (no_laps, чинится дозакачкой) — это
        # честная структурная неопределимость, как no_fast_laps (§3.5.2 — различаем).
        return {"events": [], "reason": "single_lap"}

    # границы кругов по wall-clock из startTimeGMT + duration. Если нет — пробуем
    # восстановить по накоплению duration от ts[0].
    bounds = []  # (start_ms, end_ms, pace_s_per_km)
    for lp in lap_list:
        dur = lp.get("duration") or lp.get("movingDuration")
        dist = lp.get("distance")
        spd = lp.get("averageSpeed") or lp.get("averageMovingSpeed")
        if not dur or dur <= 0:
            continue
        # темп круга из avg speed (м/с → сек/км); fallback dist/dur
        if spd and spd > 0:
            lp_pace = 1000.0 / spd
        elif dist and dist > 0:
            lp_pace = dur / (dist / 1000.0)
        else:
            continue
        bounds.append({"dur": float(dur), "pace": float(lp_pace)})
    if len(bounds) < 2:
        # круги есть, но <2 с валидной длительностью/скоростью — структуры нет
        return {"events": [], "reason": "single_lap"}

    paces = np.array([b["pace"] for b in bounds])
    med_pace = float(np.median(paces))
    # рабочие круги = быстрее медианы (меньше сек/км). Строго быстрее, чтобы на
    # ровном беге (все ~= медиане) не считать всё подряд рабочим.
    is_work = paces < med_pace * 0.97   # 3% быстрее медианы = заметно рабочий

    # восстановим wall-clock границы кругов: накопление duration от ts[0]
    t0 = float(ts[0]) if ts.size else 0.0
    edges = [t0]
    for b in bounds:
        edges.append(edges[-1] + b["dur"] * 1000.0)

    def median_hr_window(t_start_ms, t_end_ms):
        """медиана hr_clean в окне [t_start, t_end] по wall-clock (включая не-moving)."""
        sel = (ts >= t_start_ms) & (ts <= t_end_ms)
        vals = hr_clean[sel]
        vals = vals[~np.isnan(vals)]
        return float(np.median(vals)) if vals.size else None

    win = HR_RECOVERY_EDGE_WINDOW_S * 1000.0
    events = []
    for i in range(len(bounds) - 1):
        if not is_work[i]:
            continue
        # следующий круг должен быть медленнее (восстановление), иначе это два
        # рабочих подряд — не recovery-событие
        if is_work[i + 1]:
            continue
        work_end = edges[i + 1]              # конец рабочего круга (wall-clock)
        rec_end = edges[i + 2]               # конец восстановительного
        start_hr = median_hr_window(work_end - win, work_end)
        end_hr = median_hr_window(rec_end - win, rec_end)
        if start_hr is None or end_hr is None:
            continue
        events.append({
            "start_hr": round(start_hr, 1),
            "end_hr": round(end_hr, 1),
            "hr_drop": round(start_hr - end_hr, 1),
            "recovery_duration_s": round(bounds[i + 1]["dur"], 1),
            "work_lap_pace": round(bounds[i]["pace"], 1),
            "next_lap_pace": round(bounds[i + 1]["pace"], 1),
        })
    if not events:
        return {"events": [], "reason": "no_fast_laps"}
    return {"events": events, "reason": "ok"}


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


# --------------------------------------------------------------------------- #
# Резолвер user-меток (этап 7): намерение → секунда потока → привязка.
# Чистая функция, БЕЗ БД. Зовётся из recompute (батч) и add_lactate (немедленно).
# --------------------------------------------------------------------------- #
_LAP_RE = re.compile(r"lap\s*(\d+)", re.IGNORECASE)


def _resolver_tolerances(version: str) -> tuple[float, float, float]:
    """(правый, левый, дыра) допуски в МС для версии. Сегодня версия-независимы;
    точка ветвления, когда будущая версия сменит допуски/окно (тогда bump
    ALGO_VERSION → recompute перепишет раствор под новой версией)."""
    return (LACTATE_TOL_RIGHT_S * 1000.0,
            LACTATE_TOL_LEFT_S * 1000.0,
            LACTATE_TOL_GAP_S * 1000.0)


def _lap_edges_from_ts(laps: Optional[dict], ts: np.ndarray) -> Optional[list[float]]:
    """Границы кругов по WALL-CLOCK: накопление elapsedDuration от ts[0].

    elapsedDuration (НЕ moving `duration`!) включает паузы → накопленный конец
    круга совпадает с реальным wall-clock даже на рваной пробежке. Эпох-агностично
    (относительно ts[0]) — не зависит от согласованности startTimeGMT↔directTimestamp.
    edges[i] = конец круга i (1-индекс): edges[0]=ts0=старт круга 1, edges[N]=конец N.
    None, если кругов/длительностей нет.
    """
    if not laps or ts.size == 0:
        return None
    lap_list = laps.get("lapDTOs") or []
    if not lap_list:
        return None
    edges = [float(ts[0])]
    for lp in lap_list:
        dur = lp.get("elapsedDuration") or lp.get("duration") or lp.get("movingDuration")
        if not dur or dur <= 0:
            return None   # без длительности круга накопление рвётся — не угадываем
        edges.append(edges[-1] + float(dur) * 1000.0)
    return edges


def _lap_containing(edges: Optional[list[float]], t: float) -> Optional[int]:
    """Номер круга (1-индекс), в который попадает wall-clock t. edges[i-1..i] = круг i.
    None, если границ нет или t вне последнего края (в пределах — крайние круги)."""
    if not edges or len(edges) < 2:
        return None
    for i in range(1, len(edges)):
        if edges[i - 1] <= t < edges[i]:
            return i
    # t на самом конце последнего круга (граница) → последний круг
    if t == edges[-1]:
        return len(edges) - 1
    return None


def resolve_mark(stream: dict, laps: Optional[dict], intent: dict,
                 version: str = ALGO_VERSION) -> Optional[dict]:
    """Намерение user-метки → привязка к секунде потока, или None (не определима).

    intent = {"at_time": int|None (wall-clock UTC мс), "user_ref": str|None ("lapN")}.
      at_time приоритетнее user_ref (точнее — прямо в секунду, минуя нумерацию кругов).
      user_ref="lapN" → якорь = КОНЕЦ Garmin-круга N БУКВАЛЬНО (не «рабочего куска» —
        разметка работа/пауза = суждение LLM, §3.5.2; нумерация атлета ≠ Garmin, урок
        27.06). Возвращает {lap, hr_at, pace_at} той секунды.

    Возвращает {"lap", "hr_at", "pace_at"} ИЛИ None если target недостижим:
      нет потока / target вне допуска на краю / внутренняя дыра (min|ts−target|>допуск).
      None → раствор не пишем → метка pending (честно, §3.5.2).

    hr_at — СЫРОЙ пульс той секунды (как _lactate_from_watch; калибровочная точка, не
    распределение — чистка тут вредна). pace_at — None на паузной секунде (speed≈0 →
    pace NaN): честный факт «якорь в паузе, темпа нет», НЕ сглаживаем (LLM видит паузу
    и сам находит работу рядом). lap — механический факт (какой Garmin-круг), не разметка.
    """
    rows = stream.get("activityDetailMetrics") or []
    descs = stream.get("metricDescriptors") or []
    if not rows or not descs:
        return None   # нет потока (ручная запись/no_stream) → привязка не определима

    idx = _index_map(stream)
    ts = _column(rows, idx.get(K_TS))
    if ts.size == 0 or np.all(np.isnan(ts)):
        return None
    speed = _column(rows, idx.get(K_SPEED))
    hr = _column(rows, idx.get(K_HR))          # СЫРОЙ (не hr_clean) — точка, не распределение
    pace = _pace_s_per_km(speed)

    # 1) target (wall-clock мс): at_time приоритетнее user_ref
    at_time = intent.get("at_time")
    user_ref = intent.get("user_ref")
    target: Optional[float] = None
    if at_time is not None:
        target = float(at_time)
    elif user_ref:
        m = _LAP_RE.search(str(user_ref))
        if m:
            n_lap = int(m.group(1))
            edges = _lap_edges_from_ts(laps, ts)
            if edges is not None and 1 <= n_lap < len(edges):
                target = edges[n_lap]   # конец круга N (edges[N])
    if target is None:
        return None   # ни at_time, ни резолвимого user_ref → нечего привязывать

    # 2) ближайшая секунда + асимметричный допуск по региону
    ts0, ts_last = float(ts[0]), float(ts[-1])
    tol_right, tol_left, tol_gap = _resolver_tolerances(version)
    if target > ts_last:
        tol = tol_right          # правый край: замер после стопа (щедрый)
    elif target < ts0:
        tol = tol_left           # левый край: до старта (жёсткий)
    else:
        tol = tol_gap            # внутри: допуск на дыру записи
    k = int(np.argmin(np.abs(ts - target)))
    if abs(float(ts[k]) - target) > tol:
        return None   # ближайшая секунда дальше допуска (край/дыра) → pending

    # 3) привязка — механические факты той секунды
    hr_at = round(float(hr[k]), 1) if not np.isnan(hr[k]) else None
    pace_at = round(float(pace[k]), 1) if (k < pace.size and not np.isnan(pace[k])) else None
    lap = _lap_containing(_lap_edges_from_ts(laps, ts), float(ts[k]))
    return {"lap": lap, "hr_at": hr_at, "pace_at": pace_at}


def validate_mark(laps: Optional[dict], intent: dict) -> tuple[str, Optional[int]]:
    """Вердикт валидности НАМЕРЕНИЯ из текущих laps: (validation, lap_count).

    validation = f(laps) — БЕЗверсионно (не зависит от резолвера, только от структуры):
      at_time задан            → ('ok', None)   — круговой вопрос неприменим
      user_ref='lapN', laps нет → ('deferred', None) — N недоказуем (нет структуры)
      user_ref='lapN', круг N ∈ [1..M] → ('ok', M)
      user_ref='lapN', N вне [1..M]     → ('invalid', M)  — доказуемо нет круга N
    Предполагает user_ref уже well-formed 'lapN' (формат-чек — в туле до записи) или None.
    lap_count (M) едет при ok(user_ref)/invalid как ДОКАЗАТЕЛЬСТВО вердикта; NULL иначе.

    ОДНА функция для двух мест (как resolve_mark): add_lactate (немедленно, свежий вход)
    и recompute (батч). Вердикт один; ПОЛИТИКА по нему — у вызывающего: свежий вход с
    'invalid' → ошибка входа (опечатка, не писать); существующая метка 'invalid' при
    recompute → хранимое состояние (deferred→invalid при дозакачке laps).
    """
    at_time = intent.get("at_time")
    user_ref = intent.get("user_ref")
    if at_time is not None:
        return ("ok", None)
    if not user_ref:
        return ("ok", None)   # защитно: «нечего привязывать» ловит тул до вызова
    lap_list = (laps or {}).get("lapDTOs") or []
    if not lap_list:
        return ("deferred", None)   # laps нет → N недоказуем
    m = _LAP_RE.search(str(user_ref))
    lap_count = len(lap_list)
    if m and 1 <= int(m.group(1)) <= lap_count:
        return ("ok", lap_count)
    return ("invalid", lap_count)   # круга N доказуемо нет; M — доказательство


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
    laps: Optional[dict] = None,
    lactate_watch_points: Optional[list[dict]] = None,
    lactate_comment_values: Optional[list[float]] = None,
    sport: Optional[str] = None,
) -> dict:
    """Главная функция: сырой поток → числовые характеристики (МЕТОД §3.1).

    laps — сырьё кругов (lapDTOs) из store.get_raw(aid, 'laps'). НЕОБЯЗАТЕЛЕН:
        laps=None — легальный вход (исторические без кэшированных laps); тогда
        hr_recovery даёт reason=no_laps (отличается от no_fast_laps — §3.5.2).
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
            "decoupling": {"value": None, "pace_1st_half": None, "pace_2nd_half": None,
                           "pace_variance": None, "duration_s": None, "reason": "no_stream"},
            "hr_recovery": {"events": [], "reason": "no_stream"},
            "lactate_marks": None,
            "elevation": {"gain_m": None, "loss_m": None},
            "max_hr": None,
            "hr_source": "unknown",   # нет потока → источник неопределим
            "biomech_source": None,   # нет потока → appID не проверить
            "gps_type": _gps_type_from_sport(sport),  # sport известен и без потока
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
        "decoupling": _decoupling(
            hr_mv, pace_mv, dt_mv,
            (round(float(np.nanvar(pace_mv)), 2) if pace_mv[~np.isnan(pace_mv)].size else None),
            moving_time_s,
        ),
        "hr_recovery": _hr_recovery(laps, ts, hr_clean, pace, speed),
        "lactate_marks": lactate_marks,
        "elevation": {"gain_m": elev_gain, "loss_m": elev_loss},
        "max_hr": (int(round(float(np.nanpercentile(hr_clean, MAX_HR_PERCENTILE))))
                   if hr_clean[~np.isnan(hr_clean)].size else None),
        "hr_source": _hr_source_from_stream(
            idx, has_hr=bool(hr_clean[~np.isnan(hr_clean)].size)),
        "biomech_source": _biomech_source_from_stream(
            descs, has_biomech=bool(
                (gct is not None and not np.all(np.isnan(gct)))
                or (vr is not None and not np.all(np.isnan(vr))))),
        "gps_type": _gps_type_from_sport(sport),
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

    # --- enrich-0.4.0: бакетные выжимки (достаточная статистика) ---
    # pace_by_hr_bucket: ось=HR, в потоке есть pace и hr → должен заполниться.
    pbh = res["pace_by_hr_bucket"]
    assert pbh, "pace_by_hr_bucket должен считаться (есть pace+hr)"
    # формат ячейки: sum/sum_sq/seconds (складывающаяся стат-ка, НЕ квантили)
    sample = next(c["pace"] for c in pbh.values() if c.get("pace"))
    assert set(sample) == {"sum", "sum_sq", "seconds"}, f"формат бакета изменился: {sample}"
    # секунды HR-бакетов сходятся с hr-ГИСТОГРАММОЙ (та же чистка пульса: hr_clean)
    tot = sum(c["pace"]["seconds"] for c in pbh.values() if c.get("pace"))
    hist_tot = sum(res["hr_histogram"].values())
    assert abs(tot - hist_tot) < 5, \
        f"секунды HR-бакетов ({tot}) должны сходиться с hr-гистограммой ({hist_tot})"

    # центр (sum/seconds) и разброс (√(sum_sq/seconds−центр²)) восстанавливаются;
    # связка HR↔темп жива: на высоком HR центр-темп быстрее (меньше сек/км), чем на низком.
    def center(cell):  # взвешенное среднее
        return cell["sum"] / cell["seconds"]
    def disp(cell):    # std пула
        m = center(cell); v = cell["sum_sq"] / cell["seconds"] - m * m
        return v ** 0.5 if v > 0 else 0.0
    fast = [center(pbh[k]["pace"]) for k in pbh if int(k) >= 180 and pbh[k].get("pace")]
    slow = [center(pbh[k]["pace"]) for k in pbh if int(k) <= 140 and pbh[k].get("pace")]
    if fast and slow:
        assert min(fast) < max(slow), "на высоком HR центр-темп должен быть быстрее (меньше сек/км)"
    # разброс неотрицателен и конечен на каждом непустом бакете
    for c in pbh.values():
        if c.get("pace"):
            assert disp(c["pace"]) >= 0, "дисперсия пула не может быть отрицательной"

    # biomech_by_pace_bucket: в этом потоке биомеханики нет → бакеты есть (ось=темп
    # определима), но метрики внутри None. Это штатно, не ошибка.
    bbp = res["biomech_by_pace_bucket"]
    assert isinstance(bbp, dict), "biomech_by_pace_bucket должен быть dict даже без датчика"
    if bbp:
        any_cell = next(iter(bbp.values()))
        assert "gct" in any_cell, "формат бакета биомеханики: gct/vert_ratio/stride"
        assert any_cell["gct"] is None, "без датчика gct в бакете = None (нет данных)"

    # --- enrich-0.5.0: decoupling (БЕЗ порога — считается всегда, факты для LLM) ---
    # рваный поток (быстро→стоянка→трусца): decoupling ВСЁ РАВНО считается (нет порога),
    # но pace_variance высокий — самомаркирует пилу, LLM не возьмёт.
    dec = res["decoupling"]
    assert dec["reason"] == "ok", f"decoupling считается всегда где есть половины, got {dec['reason']}"
    assert dec["value"] is not None, "value есть (порога нет)"
    assert dec["pace_variance"] is not None, "pace_variance едет фактом (ловит пилу)"
    assert dec["pace_1st_half"] is not None and dec["pace_2nd_half"] is not None, \
        "темпы половин едут фактом (ловят прогрессив)"
    assert dec["duration_s"] is not None

    # РОВНЫЙ поток, пульс вверх во 2-й половине при ТОМ ЖЕ темпе → честный дрейф базы.
    # pace_1st ≈ pace_2nd (темп держался) → LLM читает decoupling как дрейф.
    n2 = 600
    speed2 = np.full(n2, 3.0) + rng.normal(0, 0.02, n2)   # ровный темп ~5:33/км
    hr2 = np.concatenate([np.full(300, 150.0), np.full(300, 162.0)]) + rng.normal(0, 1, n2)
    ts2 = (np.arange(n2) * 1000.0).astype(float)
    stream2 = {
        "metricDescriptors": [md("directTimestamp", 0), md("directSpeed", 1), md("directHeartRate", 2)],
        "activityDetailMetrics": [{"metrics": [ts2[k], speed2[k], hr2[k]]} for k in range(n2)],
    }
    dec2 = enrich_activity(stream2)["decoupling"]
    assert dec2["reason"] == "ok" and dec2["value"] > 0, f"дрейф базы → decoupling > 0, got {dec2}"
    assert abs(dec2["pace_1st_half"] - dec2["pace_2nd_half"]) < 15, \
        f"на честном дрейфе темп половин ≈ равен, got {dec2['pace_1st_half']} vs {dec2['pace_2nd_half']}"

    # ПРОГРЕССИВ: разгон 2-й половины при стабильном HR. decoupling вернёт число,
    # НО pace_2nd заметно быстрее pace_1st → самомаркируется как разгон, не дрейф.
    speed3 = np.concatenate([np.full(300, 2.7), np.full(300, 3.6)]) + rng.normal(0, 0.02, n2)  # 6:10→4:38
    hr3 = np.full(n2, 155.0) + rng.normal(0, 1, n2)   # пульс стабилен
    stream3 = {
        "metricDescriptors": [md("directTimestamp", 0), md("directSpeed", 1), md("directHeartRate", 2)],
        "activityDetailMetrics": [{"metrics": [ts2[k], speed3[k], hr3[k]]} for k in range(n2)],
    }
    dec3 = enrich_activity(stream3)["decoupling"]
    assert dec3["pace_2nd_half"] < dec3["pace_1st_half"] - 30, \
        f"прогрессив: 2-я половина заметно быстрее → видно в фактах, got {dec3['pace_1st_half']} vs {dec3['pace_2nd_half']}"
    # ключевое: факты РАЗЛИЧАЮТ дрейф (dec2: темп равен) от прогрессива (dec3: темп разный)
    # хотя оба могут дать ненулевой value. Без pace_1st/2nd LLM их не отличил бы.

    # --- enrich-0.5.0: hr_recovery (no_laps / single_lap / no_fast_laps различаются) ---
    assert res["hr_recovery"]["reason"] == "no_laps", "без laps → no_laps"
    # один круг → single_lap (НЕ no_laps: laps есть, структуры нет)
    single = enrich_activity(stream_r if False else stream2, laps={"lapDTOs": [{"duration": 600.0, "averageSpeed": 3.0}]})
    assert single["hr_recovery"]["reason"] == "single_lap", \
        f"один круг → single_lap, не no_laps, got {single['hr_recovery']['reason']}"
    # с laps: рабочий круг (быстрый) → восстановительный (медленный), HR падает
    # строю поток: круг1 работа 100с быстро HR175, круг2 восстановление 100с медленно HR130
    nr = 200
    speed_r = np.concatenate([np.full(100, 4.5), np.full(100, 2.2)])  # быстро / медленно
    hr_r = np.concatenate([np.full(100, 175.0), np.full(100, 130.0)]) + rng.normal(0, 1, nr)
    ts_r = (np.arange(nr) * 1000.0).astype(float)
    stream_r = {
        "metricDescriptors": [md("directTimestamp", 0), md("directSpeed", 1), md("directHeartRate", 2)],
        "activityDetailMetrics": [{"metrics": [ts_r[k], speed_r[k], hr_r[k]]} for k in range(nr)],
    }
    laps_r = {"lapDTOs": [
        {"duration": 100.0, "averageSpeed": 4.5, "distance": 450.0},   # рабочий (быстрый)
        {"duration": 100.0, "averageSpeed": 2.2, "distance": 220.0},   # восстановительный
    ]}
    res_r = enrich_activity(stream_r, laps=laps_r)
    rec = res_r["hr_recovery"]
    assert rec["reason"] == "ok", f"быстрый+медленный круг → recovery определён, got {rec['reason']}"
    assert len(rec["events"]) == 1, f"одно recovery-событие, got {len(rec['events'])}"
    ev = rec["events"][0]
    assert ev["hr_drop"] > 20, f"HR должен упасть с ~175 до ~130, drop={ev['hr_drop']}"
    assert ev["recovery_duration_s"] == 100.0, "длительность восстановительного круга — факт"
    assert "work_lap_pace" in ev and "next_lap_pace" in ev, "темпы кругов едут фактом"
    # стартовый HR робастен (медиана последних сек), НЕ задран артефактом-пиком:
    assert 170 <= ev["start_hr"] <= 180, f"старт recovery ≈ конец работы (~175), не пик, got {ev['start_hr']}"

    # --- этап 7: resolve_mark (намерение → секунда потока → привязка) ---
    # чистый поток 300с от базового эпоха, 1 сэмпл/с; speed рампа, hr рампа.
    base = 1_700_000_000_000        # произвольный эпох-мс; резолвер эпох-агностичен
    N = 300
    ts_m = (base + np.arange(N) * 1000.0).astype(float)
    speed_m = np.full(N, 3.0)       # ~5:33/км, бег весь поток
    hr_m = np.linspace(140.0, 175.0, N)
    def _stream(ts_arr, sp_arr, hr_arr):
        return {
            "metricDescriptors": [md("directTimestamp", 0), md("directSpeed", 1), md("directHeartRate", 2)],
            "activityDetailMetrics": [{"metrics": [ts_arr[k], sp_arr[k], hr_arr[k]]} for k in range(len(ts_arr))],
        }
    sm = _stream(ts_m, speed_m, hr_m)

    # (a) at_time ВНУТРИ потока → привязка к той секунде (hr той секунды)
    tgt = base + 150_000            # 150-я секунда
    r = resolve_mark(sm, None, {"at_time": tgt, "user_ref": None})
    assert r is not None and r["hr_at"] is not None, r
    assert abs(r["hr_at"] - hr_m[150]) < 0.5, f"at_time внутри → hr той секунды, got {r}"
    assert r["pace_at"] is not None, "бег → pace определён"

    # (b) at_time за ПРАВЫМ краем в допуске (замер через 10с после стопа) → к последней секунде
    r = resolve_mark(sm, None, {"at_time": base + (N - 1) * 1000 + 10_000, "user_ref": None})
    assert r is not None, "правый край в допуске (10с<15с) → привязка к ts_last"
    assert abs(r["hr_at"] - hr_m[-1]) < 0.5, f"привязка к последней секунде, got {r}"

    # (b2) at_time за правым краем ВНЕ допуска (через 30с) → None (pending)
    assert resolve_mark(sm, None, {"at_time": base + (N - 1) * 1000 + 30_000, "user_ref": None}) is None, \
        "правый край вне допуска (30с>15с) → None"

    # (c) at_time до ЛЕВОГО края (за 10с до старта, базовый замер) → None (жёсткий допуск 2с)
    assert resolve_mark(sm, None, {"at_time": base - 10_000, "user_ref": None}) is None, \
        "левый край: до старта записи → None (hr разбега ≠ покой)"

    # (d) ВНУТРЕННЯЯ ДЫРА: поток с пропуском записи 120..180с (пауза, датчик молчал).
    # target=150с формально в [ts0,ts_last], но ближайшая секунда в 30с → None.
    keep = np.concatenate([np.arange(0, 120), np.arange(180, N)])
    ts_gap = ts_m[keep]; sp_gap = speed_m[keep]; hr_gap = hr_m[keep]
    sg = _stream(ts_gap, sp_gap, hr_gap)
    assert resolve_mark(sg, None, {"at_time": base + 150_000, "user_ref": None}) is None, \
        "target в дыре записи (ближайшая секунда в 30с > допуска 8с) → None, не argmin к далёкой"
    # но target у ЗАПИСАННОЙ секунды в том же потоке резолвится (дыра не ломает остальное)
    assert resolve_mark(sg, None, {"at_time": base + 100_000, "user_ref": None}) is not None, \
        "target у записанной секунды резолвится (дыра локальна)"

    # (e) user_ref="lapN" → конец Garmin-круга N (буквально). Два круга по 150с.
    laps_m = {"lapDTOs": [
        {"elapsedDuration": 150.0, "averageSpeed": 3.0},   # круг 1: [0..150)
        {"elapsedDuration": 150.0, "averageSpeed": 3.0},   # круг 2: [150..300)
    ]}
    r = resolve_mark(sm, laps_m, {"at_time": None, "user_ref": "lap1"})
    assert r is not None, "user_ref=lap1 → конец круга 1 (150с) резолвится"
    assert r["lap"] in (1, 2), f"конец круга 1 — граница; lap механический факт, got {r}"
    assert abs(r["hr_at"] - hr_m[150]) < 1.0, f"якорь на секунде ~150 (конец круга 1), got {r}"

    # (f) at_time ПЕРЕКРЫВАЕТ user_ref (заданы оба) → берётся at_time
    r_both = resolve_mark(sm, laps_m, {"at_time": base + 50_000, "user_ref": "lap2"})
    assert abs(r_both["hr_at"] - hr_m[50]) < 0.5, f"at_time приоритетнее user_ref, got {r_both}"

    # (g) ПАУЗНАЯ секунда: speed=0 на 200-й → pace_at=None (честно), hr_at жив (пульс реален)
    sp_pause = speed_m.copy(); sp_pause[200] = 0.0
    sp_stream = _stream(ts_m, sp_pause, hr_m)
    r = resolve_mark(sp_stream, None, {"at_time": base + 200_000, "user_ref": None})
    assert r is not None and r["pace_at"] is None, f"пауза → pace_at None (не сглажен), got {r}"
    assert r["hr_at"] is not None, "hr реален и в паузе"

    # (h) нет потока (no_stream) → None
    assert resolve_mark({"activityDetailMetrics": [], "metricDescriptors": []},
                        None, {"at_time": base, "user_ref": None}) is None, "нет потока → None"

    # (i) user_ref без laps → None (нечего накапливать)
    assert resolve_mark(sm, None, {"at_time": None, "user_ref": "lap1"}) is None, \
        "user_ref без laps → None"

    # --- этап 7: validate_mark (вердикт из laps, versionless) ---
    laps2 = {"lapDTOs": [{"elapsedDuration": 150.0}, {"elapsedDuration": 150.0}]}  # M=2
    assert validate_mark(None, {"at_time": 123, "user_ref": None}) == ("ok", None), "at_time → ok"
    assert validate_mark(laps2, {"at_time": None, "user_ref": "lap1"}) == ("ok", 2), "круг 1 ∈[1..2] → ok"
    assert validate_mark(laps2, {"at_time": None, "user_ref": "lap2"}) == ("ok", 2)
    assert validate_mark(laps2, {"at_time": None, "user_ref": "lap5"}) == ("invalid", 2), \
        "круга 5 нет (M=2) → invalid + доказательство M=2"
    assert validate_mark(None, {"at_time": None, "user_ref": "lap4"}) == ("deferred", None), \
        "laps нет → deferred (N недоказуем)"
    assert validate_mark({"lapDTOs": []}, {"at_time": None, "user_ref": "lap4"}) == ("deferred", None)

    print("\nself-test OK")
