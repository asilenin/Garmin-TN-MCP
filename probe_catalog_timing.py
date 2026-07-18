"""probe_catalog_timing.py — ДИАГНОСТИКА: держит ли stdio catalog-sync (этап 7.6→8).

НЕ тул, НЕ тест. Измеритель ДО решения «писать ли catalog-sync из чата». Отвечает на
вопрос, поднятый прецедентом: garmin_query однажды завис ~240с на anton (cache-only!),
повтор прошёл. Нужно разделить классы возможной причины и понять, безопасен ли
блокирующий catalog-sync через stdio.

Требует токенов+сети, идёт на ЖИВОЙ БД (пишет каталог — но upsert идемпотентен и
resumable, не деструктивно; enrich-owned защищены fix 6fa179b). ТОЛЬКО у владельца.

ЧЕТЫРЕ блока (три автоматических + один ручной):

  [1] baseline query×N — виснет ли cache-only query САМ (воспроизводит прецедент?).
      Асимметрия (как QA baseline): ХОТЬ ОДИН завис → причина в query/БД/транспорте,
      НЕ в catalog (диагноз есть). Все N чисты → НЕ доказательство «query безопасен»,
      лишь «не поймано, частота ниже 1/N». Тишина слабее воспроизведения.

  [2] catalog history_years=0 — РЕАЛЬНЫЙ sync_catalog на ближнем горизонте (~7 мес,
      3 квартальных окна). Замер PER-WINDOW (count, время) — НЕ единый средний:
      разброс time/count различает модель стоимости:
        ~count  (время ∝ активности) → экстраполяция архива по суммарному count;
        ~окна   (время ∝ число окон, latency вызова доминирует) → по числу окон.
      Чтение sync_catalog предсказывает ~окна (один list_activities/окно). Проверяем.
      Асимметрия та же: прошло чисто на 3 окнах ≠ безопасно на 80 окнах архива.

  [3] query-под/после-catalog — виснет ли query РЯДОМ с catalog-записью → лок SQLite
      (catalog держит write-транзакцию). Отличает лок от транспорта: если [1] чист,
      а [3] виснет — лок; если [2] долгий сам по себе — транспорт/сеть-длина.

  [4] recoverability — РУЧНАЯ двухзапусковая процедура (sync_catalog синхронный, без
      хука прерывания — kill только снаружи). Инструкция в выводе, НЕ автоассерт.

Запуск:  uv run python probe_catalog_timing.py anton
"""
import os
import sys
import time

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))

import profiles                                    # noqa: E402
import tools                                       # noqa: E402
import sync                                        # noqa: E402
from store import Store                            # noqa: E402

N_BASELINE = 5
HANG_THRESHOLD_S = 30.0   # выше — считаем «подозрительно долго», флаг (не прерываем)


def block1_baseline(slug: str) -> bool:
    """[1] query×N соло. True если хоть один подозрительно долгий (воспроизвёл)."""
    print(f"[1] baseline: query_index ×{N_BASELINE} соло (cache-only, без сети)")
    any_slow = False
    for i in range(1, N_BASELINE + 1):
        t = time.time()
        try:
            r = tools.query_index(slug, limit=50)
            dt = time.time() - t
            flag = "  ⚠ДОЛГО" if dt > HANG_THRESHOLD_S else ""
            print(f"    #{i}: {dt:.2f}с, count={r['count']}{flag}")
            if dt > HANG_THRESHOLD_S:
                any_slow = True
        except BaseException as e:  # noqa: BLE001
            dt = time.time() - t
            print(f"    #{i}: УПАЛ за {dt:.2f}с ({type(e).__name__}): {e}")
            any_slow = True
    if any_slow:
        print("    → ВОСПРОИЗВЕДЕНО: query виснет/падает САМ → причина не в catalog. ДИАГНОЗ.")
    else:
        print(f"    → все {N_BASELINE} чисты. НЕ доказательство безопасности query —")
        print(f"      лишь «не поймано, частота ниже 1/{N_BASELINE}» (тишина ≠ диагноз).")
    return any_slow


def block2_catalog(slug: str) -> None:
    """[2] sync_catalog ДВА прогона (year=0, year=1), разностный наклон как ГРУБЫЙ
    индикатор модели стоимости. Per-window время недоступно (SyncReport его не пишет,
    sync_catalog берёт только history_years — явных границ нет; править функцию нельзя
    — загрязнит предмет). Два горизонта — единственный публичный способ увидеть наклон."""
    print(f"\n[2] catalog: sync_catalog ДВА прогона (history_years=0, затем =1)")
    print("    (реальный код, БЕЗ правок; year=1 ПЕРЕ-обходит окна year=0 —")
    print("     last_sync_window не читается, backlog — но налог сокращается в РАЗНОСТИ)")

    t0 = time.time()
    rep0 = sync.sync_catalog(slug, history_years=0)
    dt0 = time.time() - t0
    print(f"    year=0: {dt0:.2f}с, окон {rep0.windows}, активностей {rep0.activities_upserted}")
    for line in rep0.window_log:
        print(f"      {line}")

    t1 = time.time()
    rep1 = sync.sync_catalog(slug, history_years=1)
    dt1 = time.time() - t1
    print(f"    year=1: {dt1:.2f}с, окон {rep1.windows}, активностей {rep1.activities_upserted}")
    for line in rep1.window_log:
        print(f"      {line}")

    d_win = rep1.windows - rep0.windows
    d_act = rep1.activities_upserted - rep0.activities_upserted
    d_time = dt1 - dt0
    print(f"\n    РАЗНОСТЬ (year1 − year0): Δокон={d_win}, Δактивностей={d_act}, "
          f"Δвремя={d_time:.2f}с")
    if d_win > 0:
        print(f"      наклон по окнам: {d_time/d_win:.2f}с/окно")
    if d_act > 0:
        print(f"      наклон по активностям: {d_time/d_act:.3f}с/активность")
    print(f"    ТРАКТОВКА (грубый индикатор, НЕ доказательство модели):")
    print(f"      • разностный наклон вычитает налог перекрытия — но ЧИСТ только если")
    print(f"        RTT list_activities ≫ локальная запись upsert (иначе завышен).")
    print(f"        RTT к Garmin (сотни мс–сек) обычно ≫ SQLite-upsert (мс) — вероятно, но")
    print(f"        не факт, это само предмет замера.")
    print(f"      • одна пара точек НЕ устанавливает модель в общем (пагинация/throttle")
    print(f"        могут быть нелинейны на объёме) — ищем лишь НАСТОРАЖИВАЮЩЕЕ:")
    print(f"        Δвремя резко ∝ Δактивностям (не Δокнам) = красный флаг «дорого на")
    print(f"        объёме»; ∝ Δокнам = стоимость в числе окон (ожидание из чтения кода).")
    print(f"      • оба прогона чисты ≠ безопасно на 80 окнах архива (асимметрия baseline).")
    for rep, yr in ((rep0, 0), (rep1, 1)):
        if rep.stopped_early:
            print(f"    year={yr} останов рано: {rep.stop_reason}")


def block3_query_under_catalog(slug: str) -> None:
    """[3] query сразу ПОСЛЕ catalog — виснет ли рядом с записью (лок?)."""
    print(f"\n[3] query сразу после catalog-записи (лок SQLite?)")
    t = time.time()
    try:
        r = tools.query_index(slug, limit=50)
        dt = time.time() - t
        flag = "  ⚠ДОЛГО (лок?)" if dt > HANG_THRESHOLD_S else ""
        print(f"    query после catalog: {dt:.2f}с, count={r['count']}{flag}")
        print(f"    → если [1] был чист, а здесь ДОЛГО — лок; если и тут чисто — не лок.")
    except BaseException as e:  # noqa: BLE001
        print(f"    query после catalog УПАЛ ({type(e).__name__}): {e}")


def block4_recoverability_manual(slug: str) -> None:
    """[4] РУЧНАЯ процедура — sync_catalog синхронный, kill только снаружи."""
    print(f"\n[4] recoverability — РУЧНАЯ двухзапусковая процедура (не автоассерт):")
    print(f"    sync_catalog resumable ПО КОДУ (upsert per-window+commit, meta-чекпойнт,")
    print(f"    idempotent). Проверить ФАКТОМ:")
    print(f"      (1) uv run python -c \"import sys; sys.path.insert(0,'garmin_raw'); "
          f"import sync; sync.sync_catalog('{slug}')\"  ← прерви Ctrl-C на 2-3 окне")
    print(f"      (2) sqlite3 <db> \"SELECT COUNT(*),MAX(date) FROM activities\"  ← запомни")
    print(f"      (3) повтори (1) до конца (не прерывая)")
    print(f"      (4) снова COUNT — каталог ДОПОЛНИЛСЯ, не побит, докачка прошла?")
    print(f"    Ожидание по коду: (2) даёт неполный-но-валидный каталог, (4) — полный.")


if __name__ == "__main__":
    slug = sys.argv[1] if len(sys.argv) > 1 else "anton"
    print(f"=== ДИАГНОСТИКА catalog-timing, профиль {slug} ===")
    print("живой прогон, токены+сеть, пишет каталог (idempotent/resumable, не деструктивно)\n")
    block1_baseline(slug)
    block2_catalog(slug)
    block3_query_under_catalog(slug)
    block4_recoverability_manual(slug)
    print("\n=== верни вывод [1][2][3] целиком; [4] — прогони руками отдельно ===")
    print("интерпретация: воспроизведение (в [1] или [3]) = диагноз класса причины;")
    print("всё чисто = аномалия не пойдана на этом масштабе (не зелёный свет архиву).")
