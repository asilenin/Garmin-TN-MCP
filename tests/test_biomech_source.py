"""test_biomech_source.py — biomech_source из потока (T7.6-2b, enrich-0.6.2).

Признак — присутствие Stryd-appID в metricDescriptors. Проверено на ДВУХ реальных
классах (Stryd-активность / trail без Stryd-датафилда): appID есть → foot-pod, нет
→ watch-only, нет biomech → None. Юнит функции + инвариант эмиссии в каталог.
"""
import os, sys, tempfile
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))
os.environ["GARMIN_TN_HOME"] = tempfile.mkdtemp()

from enrich import _biomech_source_from_stream, _STRYD_APPID, ALGO_VERSION  # noqa: E402


def test_footpod_when_stryd_appid() -> None:
    descs = [{"key": "connectIQDeveloperField-11", "appID": _STRYD_APPID},
             {"key": "directGroundContactTime", "appID": None}]
    assert _biomech_source_from_stream(descs, has_biomech=True) == "foot-pod"
    print("  Stryd-appID присутствует → foot-pod OK")


def test_watch_only_when_no_stryd() -> None:
    """Trail-класс: GCT есть (часы), Stryd-appID нет → watch-only."""
    descs = [{"key": "directGroundContactTime", "appID": None},
             {"key": "directVerticalRatio", "appID": None},
             {"key": "directPower", "appID": None}]   # power есть и без Stryd (факт)
    assert _biomech_source_from_stream(descs, has_biomech=True) == "watch-only"
    print("  biomech есть, Stryd-appID нет → watch-only OK")


def test_none_when_no_biomech() -> None:
    assert _biomech_source_from_stream([], has_biomech=False) is None
    print("  нет biomech → None (нечего атрибутировать) OK")


def test_run_pod_not_invented() -> None:
    """run-pod (TZ-словарь) НЕ эмитится без образца — только два наблюдаемых класса."""
    for hb in (True, False):
        r = _biomech_source_from_stream([{"key": "x", "appID": None}], has_biomech=hb)
        assert r in ("watch-only", None), f"неожиданный класс: {r}"
    print("  run-pod не выдуман (только foot-pod/watch-only/None) OK")


def test_version_bumped() -> None:
    """biomech_source — новое поле → версия поднята (иначе has_enriched не пересчитает)."""
    assert ALGO_VERSION == "enrich-0.6.2", ALGO_VERSION
    print(f"  ALGO_VERSION = {ALGO_VERSION} (bump под biomech_source) OK")


if __name__ == "__main__":
    test_footpod_when_stryd_appid()
    test_watch_only_when_no_stryd()
    test_none_when_no_biomech()
    test_run_pod_not_invented()
    test_version_bumped()
    print("biomech_source тесты — ЗЕЛЁНЫЕ")
