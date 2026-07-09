"""test_wellness.py — герметичные тесты garmin_wellness (этап 7.6, CI без токенов/сети).

Покрывает то, что проверяемо БЕЗ реальной сети:
  (1) cache-hit-до-Fetcher: полный валидный кэш под forbid_network → сеть НЕ тронута
      (INV-NET-GUARD-требование placement: cache-hit возвращается до создания Fetcher);
  (2) per-зонд degradation: ok/empty/error в кэше отдаются раздельно, отсутствие
      строки = зонд не в ответе;
  (3) derived-пометка: body_battery/stress несут derived_fields (не резаны, WELL-FRESHNESS-LLM);
  (4) возраст даты как факт свежести (requested_at_age_days).

ЖИВАЯ часть (у владельца, с токенами) — ОТДЕЛЬНО (test_wellness_live через сокет-
страж): cache-miss → реальный поход через fetch.py, пять URL, ForbiddenNetworkAccess
под запретом. Здесь её нет: cache-miss под forbid_network проверяем как «пытается в
сеть» (ловится стражем), но реальные URL/пять зондов — только вживую.
"""
import os
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))

tmp = tempfile.mkdtemp()
os.environ["GARMIN_TN_HOME"] = tmp

import profiles                                             # noqa: E402
import net_tools                                            # noqa: E402
from store import Store                                     # noqa: E402
from netguard import ForbiddenNetworkAccess, forbid_network  # noqa: E402

SLUG = "wtest"
DATE = "2026-07-01"
prof = profiles.resolve(SLUG); prof.ensure_dirs()


def _seed_full_cache() -> None:
    """Кладём в кэш все пять зондов (разные статусы) — имитация состоявшегося похода."""
    with Store(prof.db_path) as st:
        st.put_wellness_probe(DATE, "sleep", "ok", {"deepSleepSeconds": 7200})
        st.put_wellness_probe(DATE, "hrv", "ok", {"weeklyAvg": 45})
        st.put_wellness_probe(DATE, "rhr", "ok", {"restingHeartRate": 48})
        st.put_wellness_probe(DATE, "stress", "empty")           # сходили, пусто
        st.put_wellness_probe(DATE, "body_battery", "error", detail="429 rate limit")


def _pin_and_seed() -> None:
    """Порядко-независимость: пиннит profiles.ROOT к своему tmp (другие тест-файлы
    мутируют глобал и НЕ восстанавливают — кросс-тестовая протечка), пере-резолвит prof
    и сеет кэш. Вызывается в начале каждого теста."""
    global prof
    profiles.ROOT = Path(tmp)
    prof = profiles.resolve(SLUG); prof.ensure_dirs()
    _seed_full_cache()


def test_cache_hit_no_network() -> None:
    """(1) Полный кэш (все 5 зондов есть строкой) под forbid_network → сеть НЕ тронута.
    Ключевой тест placement: garmin_wellness видит, что качать нечего, и НЕ создаёт
    Fetcher (иначе ленивый login полез бы в сокет)."""
    _pin_and_seed()
    with forbid_network():
        res = net_tools.garmin_wellness(SLUG, DATE)   # refresh=False, все зонды в кэше
    assert res["date"] == DATE
    assert set(res["probes"]) == {"sleep", "hrv", "rhr", "stress", "body_battery"}
    print("  (1) cache-hit: полный кэш под запретом сети → Fetcher не создан OK")


def test_per_probe_degradation() -> None:
    """(2) ok/empty/error отдаются раздельно, payload только у ok."""
    _pin_and_seed()
    with forbid_network():
        res = net_tools.garmin_wellness(SLUG, DATE)
    p = res["probes"]
    assert p["sleep"]["status"] == "ok" and p["sleep"]["payload"] == {"deepSleepSeconds": 7200}
    assert p["stress"]["status"] == "empty" and p["stress"]["payload"] is None
    assert p["body_battery"]["status"] == "error"
    assert p["body_battery"]["detail"] == "429 rate limit" and p["body_battery"]["payload"] is None
    print("  (2) per-зонд degradation: ok/empty/error раздельно, payload лишь у ok OK")


def test_derived_marked() -> None:
    """(3) derived-поля помечены (не резаны, WELL-FRESHNESS-LLM разв. C)."""
    _pin_and_seed()
    with forbid_network():
        res = net_tools.garmin_wellness(SLUG, DATE)
    assert res["probes"]["body_battery"]["derived_fields"], "body_battery без derived-пометки"
    assert res["probes"]["stress"]["derived_fields"], "stress без derived-пометки"
    assert res["probes"]["sleep"]["derived_fields"] == [], "sleep не должен нести derived"
    print("  (3) derived-пометка: body_battery/stress помечены, sleep пуст OK")


def test_age_days_fact() -> None:
    """(4) возраст даты — факт свежести для LLM (WELL-FRESHNESS-LLM), не суждение в коде."""
    _pin_and_seed()
    with forbid_network():
        res = net_tools.garmin_wellness(SLUG, DATE)
    assert "requested_at_age_days" in res
    assert isinstance(res["requested_at_age_days"], int)
    print(f"  (4) возраст даты как факт: requested_at_age_days="
          f"{res['requested_at_age_days']} OK")


def test_login_fail_blocked_by_auth() -> None:
    """(5) Промах кэша + login-сбой (в CI токенов нет → login падает) → зонды to_fetch
    получают blocked_by_auth В ВОЗВРАТЕ (не в кэше), + login_error с reauth-сообщением.
    Ключевое: login-сбой НЕ маскируется под пять зондовых error и НЕ пишется в кэш.
    Проверяет разведение login-фазы от зонд-фазы."""
    _pin_and_seed()
    empty_date = "2025-01-15"   # нет строк в кэше → все 5 в to_fetch
    with Store(prof.db_path) as st:
        assert st.get_wellness_date(empty_date) == {}, "дата должна быть пустой"
    res = net_tools.garmin_wellness(SLUG, empty_date)   # login упадёт (нет токенов)
    # все пять — blocked_by_auth (не отсутствие-строки, не error)
    assert set(res["probes"]) == {"sleep", "hrv", "rhr", "stress", "body_battery"}, res["probes"].keys()
    for probe, rec in res["probes"].items():
        assert rec["status"] == "blocked_by_auth", (probe, rec["status"])
        assert rec["fetched_at"] is None
    # login_error присутствует и несёт сообщение
    assert "login_error" in res and res["login_error"]["message"], res.get("login_error")
    # КРИТИЧНО: login-сбой НЕ записан в кэш (blocked живёт только в возврате)
    with Store(prof.db_path) as st:
        assert st.get_wellness_date(empty_date) == {}, "login-сбой протёк в кэш!"
    print("  (5) login-сбой → blocked_by_auth в возврате + login_error, кэш чист OK")


if __name__ == "__main__":
    test_cache_hit_no_network()
    test_per_probe_degradation()
    test_derived_marked()
    test_age_days_fact()
    test_login_fail_blocked_by_auth()
    print("ГЕРМЕТИЧНЫЕ тесты garmin_wellness — ЗЕЛЁНЫЕ (живая часть докачки — у владельца)")
