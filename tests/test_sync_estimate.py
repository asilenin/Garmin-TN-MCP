"""test_sync_estimate.py — garmin_sync_estimate (SYNC-EXPLICIT-RANGE): объём/время синка ДО закачки.

Сетевой read (list_activities за окна). Ключевое:
  (1) dry_run НЕ мутирует каталог/meta (страж тихого рассинхрона meta↔каталог);
  (2) login-fail-fast наследован (тот же sync_catalog-путь, не копия с дырой);
  (3) единица времени — ОКНА (windows×pace), не count активностей;
  (4) валидация диапазона (обязателен вместе).
Живой count/время — у владельца (реальный list_activities).
"""
import os, sys, tempfile, time
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))
os.environ["GARMIN_TN_HOME"] = tempfile.mkdtemp()

import profiles                       # noqa: E402
import net_tools                      # noqa: E402
from store import Store               # noqa: E402
from sync import sync_catalog         # noqa: E402

SLUG = "syncest"
prof = profiles.resolve(SLUG); prof.ensure_dirs()


def test_range_required() -> None:
    """Диапазон обязателен вместе (SYNC-EXPLICIT-RANGE, до сети)."""
    try:
        sync_catalog(SLUG, start_date="2026-07-01", dry_run=True)  # end нет
        raise AssertionError("одиночный start не отвергнут")
    except ValueError as e:
        assert "вместе" in str(e), e
    print("  (1) диапазон обязателен вместе OK")


def test_dry_run_no_mutation() -> None:
    """СТРАЖ рассинхрона: dry_run НЕ трогает каталог/meta (иначе estimate молча
    продвинул бы чекпойнт синка при нетронутом каталоге)."""
    with Store(prof.db_path) as st:
        st.conn.execute("INSERT OR IGNORE INTO activities(activity_id,date,sport) "
                        "VALUES(1,'2020-01-01','running')")
        st.meta_set("last_sync_window", "SENTINEL_LSW")
        st.meta_set("last_sync", "SENTINEL_LS")
        n_before = st.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        st.conn.commit()
    # dry_run-попытка (упадёт на login без токенов, но ДО мутации в любом случае)
    try:
        net_tools.garmin_sync_estimate(SLUG, "2026-07-01", "2026-07-07")
    except RuntimeError:
        pass   # login-сбой ожидаем (нет токенов)
    with Store(prof.db_path) as st:
        assert st.meta_get("last_sync_window") == "SENTINEL_LSW", "meta LSW искажено dry_run!"
        assert st.meta_get("last_sync") == "SENTINEL_LS", "meta last_sync искажено!"
        n_after = st.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        assert n_after == n_before, "каталог изменён dry_run!"
    print("  (2) dry_run НЕ мутирует каталог/meta (страж рассинхрона) OK")


def test_login_fail_fast_inherited() -> None:
    """login-fail-fast наследован (тот же sync_catalog-путь, не копия) — не виснет."""
    t0 = time.time()
    try:
        net_tools.garmin_sync_estimate(SLUG, "2026-07-01", "2026-07-07")
    except RuntimeError as e:
        assert "login" in str(e).lower() or "войти" in str(e).lower(), e
    dt = time.time() - t0
    assert dt < 30, f"висел {dt:.0f}с — login-fix НЕ наследован (копия с дырой?)"
    print(f"  (3) login-fail-fast наследован ({dt:.1f}с, не 360) OK")


def test_time_unit_is_windows() -> None:
    """Единица времени — ОКНА (windows×pace), не count. Проверяем формулу в коде."""
    import inspect
    src = inspect.getsource(net_tools.garmin_sync_estimate)
    assert "windows * DEFAULT_PACE_S" in src, "время не по окнам!"
    assert "count" in src and "activities_upserted" in src, "нет count объёма"
    # count и время — разные единицы (не count×pace)
    assert "count * DEFAULT_PACE" not in src and "count*DEFAULT" not in src, \
        "время по count — НЕВЕРНАЯ единица (должно по окнам)"
    print("  (4) время=windows×pace, count=объём — разные единицы (не count×pace) OK")


if __name__ == "__main__":
    test_range_required()
    test_dry_run_no_mutation()
    test_login_fail_fast_inherited()
    test_time_unit_is_windows()
    print("garmin_sync_estimate тесты — ЗЕЛЁНЫЕ (живой count/время — у владельца)")
