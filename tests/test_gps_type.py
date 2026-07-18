"""test_gps_type.py — gps_type из декларации sport (T7.6-2b, enrich-0.6.3).

Механический перевод словаря typeKey→категория (доверие разметке, не инференс).
Юнит маппинга (пять значений + indoor отдельно от treadmill + неизвестный→None) +
СТРАЖ СОГЛАСОВАННОСТИ: gps_type в каталоге согласован с sport для ЭТОГО id (не
«параметр передан», а «значение соответствует источнику истины» — класс has_raw).
"""
import os, sys, tempfile
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))
os.environ["GARMIN_TN_HOME"] = tempfile.mkdtemp()

from enrich import _gps_type_from_sport, ALGO_VERSION  # noqa: E402
from sport_taxonomy import GPS_TYPE_BY_SPORT as _GPS_TYPE_BY_SPORT  # noqa: E402


def test_mapping_five_values() -> None:
    assert _gps_type_from_sport("running") == "outdoor"
    assert _gps_type_from_sport("trail_running") == "outdoor"
    assert _gps_type_from_sport("treadmill_running") == "treadmill"
    assert _gps_type_from_sport("track_running") == "track"
    assert _gps_type_from_sport("indoor_running") == "indoor"
    print("  пять значений: running/trail→outdoor, treadmill, track, indoor OK")


def test_indoor_not_collapsed_to_treadmill() -> None:
    """indoor ОТДЕЛЬНО от treadmill (belt-assist vs пол — разные GCT/vert, §5.4).
    Схлопывание = невосстановимая потеря при записи."""
    assert _gps_type_from_sport("indoor_running") != _gps_type_from_sport("treadmill_running")
    assert _gps_type_from_sport("indoor_running") == "indoor"
    print("  indoor ≠ treadmill (не схлопнуто — belt-assist vs пол) OK")


def test_unknown_typekey_none() -> None:
    """Неизвестный typeKey → None ('не распознан'), НЕ гадаем в outdoor.
    None ('typeKey не распознан') ≠ indoor ('знаю: без GPS, не дорожка')."""
    assert _gps_type_from_sport("obstacle_running") is None
    assert _gps_type_from_sport("virtual_run") is None
    assert _gps_type_from_sport(None) is None
    print("  неизвестный typeKey → None (не гадаем, ≠ indoor) OK")


def test_consistency_with_catalog() -> None:
    """СТРАЖ: gps_type в enriched согласован с sport для ЭТОГО id (поведение, не
    «передан ли параметр»). Прогоняем маппинг для каждого sport из словаря — enriched
    gps_type обязан совпасть с _gps_type_from_sport(sport_каталога). Если вызов достанет
    sport из устаревшей копии — разойдётся."""
    from enrich import enrich_activity
    empty_stream = {"activityDetailMetrics": [], "metricDescriptors": []}
    for sport, expected_gps in _GPS_TYPE_BY_SPORT.items():
        r = enrich_activity(empty_stream, sport=sport)
        assert r["gps_type"] == expected_gps, f"{sport}: {r['gps_type']} != {expected_gps}"
        # согласованность: то же, что прямой маппинг источника
        assert r["gps_type"] == _gps_type_from_sport(sport), sport
    # неизвестный sport в enrich → None (не падает, не гадает)
    r = enrich_activity(empty_stream, sport="obstacle_running")
    assert r["gps_type"] is None, r["gps_type"]
    print("  страж: gps_type в enriched согласован с sport-источником (все 5 + unknown) OK")


def test_version_bumped() -> None:
    assert ALGO_VERSION == "enrich-0.6.5", ALGO_VERSION
    print(f"  ALGO_VERSION = {ALGO_VERSION} (bump под gps_type) OK")


if __name__ == "__main__":
    test_mapping_five_values()
    test_indoor_not_collapsed_to_treadmill()
    test_unknown_typekey_none()
    test_consistency_with_catalog()
    test_version_bumped()
    print("gps_type тесты — ЗЕЛЁНЫЕ")
