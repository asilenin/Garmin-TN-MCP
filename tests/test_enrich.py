"""test_enrich.py — герметичные тесты tools.enrich_activity (этап 7.6, CI без сети).

Покрывает СТРУКТУРНЫЕ отказы (не требуют реального streams-потока/numpy):
  not_found — активности нет в каталоге;
  no_raw    — активность есть, streams нет → cache-only enrich невозможен, отказ
              с hint (НЕ тихая деградация, НЕ авто-sync).

enriched/already/error требуют РЕАЛЬНОГО валидного streams в БД (синтетический
поток для enrich_activity собрать корректно нетривиально — кривой поток дал бы
ложно-зелёный 'enriched'). Проверяются ЖИВЫМ тестом у владельца (реальная активность
с raw, обогащение из БД под forbid_network — докажет и cache-only на реальном пути,
и что пересчёт работает). Cache-only-инвариант самого тула уже доказан в
test_cache_only (enrich_activity под forbid_network не полез).
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))

tmp = tempfile.mkdtemp()
os.environ["GARMIN_TN_HOME"] = tmp

import profiles                    # noqa: E402
import tools                       # noqa: E402
from store import Store            # noqa: E402

SLUG = "entest"
prof = profiles.resolve(SLUG); prof.ensure_dirs()


def test_not_found() -> None:
    r = tools.enrich_activity(SLUG, 999999)
    assert r["status"] == "not_found", r
    print("  not_found: активности нет в каталоге OK")


def test_no_raw() -> None:
    """Активность в каталоге есть, streams нет → структурированный отказ с hint."""
    with Store(prof.db_path) as st:
        st.conn.execute("INSERT OR IGNORE INTO activities(activity_id,date,sport) "
                        "VALUES(555,'2026-07-01','running')")
        st.conn.commit()
    r = tools.enrich_activity(SLUG, 555)
    assert r["status"] == "no_raw", r
    assert "hint" in r and r["hint"], "no_raw без hint — LLM не поймёт, что делать"
    print("  no_raw: streams нет → отказ с hint (не деградация, не авто-sync) OK")


def test_predicate_single_source() -> None:
    """has_raw('streams') — тот же предикат, что агрегирует estimate. Проверяем, что
    тул опирается ровно на него: положим streams-строку → перестанет быть no_raw
    (уйдёт в enriched/error, но НЕ no_raw). Это фиксирует единый источник истины."""
    with Store(prof.db_path) as st:
        st.conn.execute("INSERT OR IGNORE INTO activities(activity_id,date,sport) "
                        "VALUES(556,'2026-07-01','running')")
        # кладём заведомо кривой streams — тул уйдёт с no_raw на error/enriched,
        # проверяем ТОЛЬКО что покинул no_raw-ветку (предикат сработал по has_raw)
        st.put_raw(556, "streams", {"broken": True})
        st.conn.commit()
    r = tools.enrich_activity(SLUG, 556)
    assert r["status"] != "no_raw", f"has_raw есть, но тул вернул no_raw: {r}"
    print(f"  predicate: streams-строка есть → покинул no_raw (status={r['status']}) OK")


if __name__ == "__main__":
    test_not_found()
    test_no_raw()
    test_predicate_single_source()
    print("ГЕРМЕТИЧНЫЕ тесты enrich_activity — ЗЕЛЁНЫЕ "
          "(enriched/already/error — живой тест у владельца)")
