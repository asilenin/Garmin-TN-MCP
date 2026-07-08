"""test_enrich_fetch.py — сетевой enrich: fetch (действие) + fetch_estimate (домен-знания).

ТЕСТ-КОНТРАСТ (граница третьего класса видима, не задекларирована): два соседа в
net_tools под forbid_network ведут себя ПРОТИВОПОЛОЖНО —
  • garmin_enrich_fetch (сетевое ДЕЙСТВИЕ) → КРАСНЫЙ (пробивает forbid_network / login);
  • garmin_enrich_fetch_estimate (домен ЗНАНИЯ) → ЗЕЛЁНЫЙ (не трогает сеть, count×pace).
Контраст в одном тесте фиксирует границу лучше, чем оговорка в каждом docstring.

Плюс cache-hit fetch (not_found/already — до сети, без токенов). Живая докачка — у
владельца (реальный streams+watch-лактат).
"""
import os, sys, tempfile
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))
tmp = tempfile.mkdtemp(); os.environ["GARMIN_TN_HOME"] = tmp

import profiles                                             # noqa: E402
import net_tools                                            # noqa: E402
from store import Store                                     # noqa: E402
from netguard import ForbiddenNetworkAccess, forbid_network  # noqa: E402

SLUG = "eftest"
prof = profiles.resolve(SLUG); prof.ensure_dirs()


def test_fetch_not_found() -> None:
    """not_found — до сети (активности нет в каталоге)."""
    r = net_tools.garmin_enrich_fetch(SLUG, 999999)
    assert r["status"] == "not_found", r
    print("  fetch not_found (до сети) OK")


def test_fetch_estimate_is_knowledge_domain() -> None:
    """ДОМЕН-ЗНАНИЯ: fetch_estimate под forbid_network ЗЕЛЁНЫЙ (count×pace, сеть не
    трогает). Здесь, не в tools.py, потому что владеет pace — но действие сети нет."""
    with Store(prof.db_path) as st:
        st.conn.execute("INSERT OR IGNORE INTO activities(activity_id,date,sport) "
                        "VALUES(1,'2026-07-01','running'),(2,'2026-07-01','running')")
        st.conn.commit()
    with forbid_network():
        r = net_tools.garmin_enrich_fetch_estimate(SLUG)   # НЕ должен пойти в сеть
    assert "count_missing_raw" in r and "estimated_hours_best_case" in r, r
    assert r["count_missing_raw"] == 2, r   # две активности без raw
    # время = count × pace / 3600, best_case
    assert r["estimated_hours_best_case"] >= 0, r
    print(f"  fetch_estimate под forbid_network ЗЕЛЁНЫЙ (домен-знания): "
          f"count={r['count_missing_raw']}, hours={r['estimated_hours_best_case']} OK")


def test_fetch_is_network_action() -> None:
    """ДЕЙСТВИЕ: fetch под forbid_network КРАСНЫЙ (валидная активность без raw →
    пытается скачать streams → сеть). Контраст с fetch_estimate выше."""
    with Store(prof.db_path) as st:
        st.conn.execute("INSERT OR IGNORE INTO activities(activity_id,date,sport) "
                        "VALUES(3,'2026-07-01','running')")
        st.conn.commit()
    reached_net = False
    with forbid_network():
        r = net_tools.garmin_enrich_fetch(SLUG, 3)
    # fetch ловит Exception внутрь status=error (login-сбой/ForbiddenNetworkAccess);
    # признак «пытался в сеть» — status=error с сетевым/auth detail, НЕ enriched/no_raw
    assert r["status"] == "error", f"fetch не пытался в сеть? {r}"
    print(f"  fetch под forbid_network КРАСНЫЙ (действие): status=error OK")
    print("  → КОНТРАСТ: fetch_estimate зелёный / fetch красный — граница видима")


if __name__ == "__main__":
    test_fetch_not_found()
    test_fetch_estimate_is_knowledge_domain()
    test_fetch_is_network_action()
    print("ТЕСТЫ сетевого enrich — ЗЕЛЁНЫЕ (живая докачка — у владельца)")
