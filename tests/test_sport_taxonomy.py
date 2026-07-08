"""test_sport_taxonomy.py — единый источник классификации typeKey + RUN-CLASS-PREDICATE.

Структурная гарантия: производные (gps_type, sport_class) строятся из ОДНОЙ таблицы
_TAXONOMY — «забыл typeKey в одном словаре» невозможно (одна строка = все признаки).
Тест это подтверждает + проверяет sport_class-фильтр query (недобор mila).
"""
import os, sys, tempfile
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))
os.environ["GARMIN_TN_HOME"] = tempfile.mkdtemp()

import sport_taxonomy as tax  # noqa: E402
import profiles               # noqa: E402
import tools                  # noqa: E402
from store import Store       # noqa: E402


def test_derivatives_complete_over_source() -> None:
    """СТРУКТУРНАЯ полнота: каждый KNOWN_TYPE_KEY имеет ОБА производных (gps_type,
    sport_class) — не забыт ни в одном. По построению (NamedTuple-строка), тест
    подтверждает инвариант."""
    for tk in tax.KNOWN_TYPE_KEYS:
        assert tk in tax.GPS_TYPE_BY_SPORT, f"{tk} нет в gps_type-производном"
        assert tax.sport_class_of(tk) is not None, f"{tk} без sport_class"
    # обратно: производные не содержат typeKey вне источника
    assert set(tax.GPS_TYPE_BY_SPORT) == set(tax.KNOWN_TYPE_KEYS)
    print(f"  полнота: все {len(tax.KNOWN_TYPE_KEYS)} typeKey имеют оба производных OK")


def test_all_running_are_run_class() -> None:
    """Все пять *_running → sport_class 'run' (union для «сколько пробежек»)."""
    for tk in ("running", "trail_running", "treadmill_running", "track_running",
               "indoor_running"):
        assert tax.sport_class_of(tk) == "run", f"{tk} не run"
    keys = tax.type_keys_for_class("run")
    assert keys == frozenset({"running", "trail_running", "treadmill_running",
                              "track_running", "indoor_running"}), keys
    print("  все *_running → class 'run', type_keys_for_class('run') полон OK")


def test_unknown_class_and_typekey() -> None:
    assert tax.type_keys_for_class("flying") is None    # неизв. класс → None (не пусто)
    assert tax.sport_class_of("obstacle_run") is None    # неизв. typeKey → None
    assert tax.gps_type_from_sport("obstacle_run") is None
    print("  неизвестный класс/typeKey → None (не гадаем) OK")


def test_sport_class_filter_solves_mila_undercount() -> None:
    """RUN-CLASS-PREDICATE: sport_class='run' ловит ВСЕ беговые (недобор mila —
    sport=running пропускал treadmill/trail/track/indoor)."""
    prof = profiles.resolve("taxq"); prof.ensure_dirs()
    with Store(prof.db_path) as st:
        st.conn.executemany(
            "INSERT OR IGNORE INTO activities(activity_id,date,sport) VALUES(?,?,?)",
            [(1, "2026-01-01", "running"), (2, "2026-01-02", "treadmill_running"),
             (3, "2026-01-03", "trail_running"), (4, "2026-01-04", "track_running"),
             (5, "2026-01-05", "indoor_running"), (6, "2026-01-06", "cycling")])
        st.conn.commit()
    # sport_class=run → 5 беговых, cycling НЕ включён
    r = tools.query_index("taxq", sport_class="run", limit=50)
    assert r["count"] == 5, f"ждём 5 беговых, got {r['count']}"
    assert all(a["sport"] != "cycling" for a in r["activities"])
    # sport=running (один тип) → 1 (контраст: недобор, если считать только этим)
    assert tools.query_index("taxq", sport="running", limit=50)["count"] == 1
    # неизвестный класс → ignored (честно, не молча пусто)
    r3 = tools.query_index("taxq", sport_class="flying", limit=50)
    assert "sport_class" in r3.get("ignored_filters", [])
    print("  sport_class=run → 5 беговых (не cycling); sport=running → 1 (недобор) OK")


if __name__ == "__main__":
    test_derivatives_complete_over_source()
    test_all_running_are_run_class()
    test_unknown_class_and_typekey()
    test_sport_class_filter_solves_mila_undercount()
    print("sport_taxonomy + RUN-CLASS-PREDICATE тесты — ЗЕЛЁНЫЕ")
