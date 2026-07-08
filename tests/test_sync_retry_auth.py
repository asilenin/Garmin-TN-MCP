"""test_sync_retry_auth.py — login-сбой в sync не ретраится (backlog SYNC-RETRY-AUTH).

Раньше login-сбой (протухшие токены) тонул в per-window retry → ~6-мин зависание.
Теперь login ЯВНО до цикла окон → быстрый RuntimeError. Инвариант: без токенов sync
падает за СЕКУНДЫ (не минуты), с внятным login-сообщением. Тест ставит жёсткий
таймаут МЕНЬШЕ retry-budget (360с) — если правка отката, тест повиснет→упадёт по
таймауту, поймав регрессию."""
import os, sys, time, tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))
os.environ["GARMIN_TN_HOME"] = tempfile.mkdtemp()

import profiles       # noqa: E402
from sync import sync_catalog  # noqa: E402

SLUG = "retrytest"
prof = profiles.resolve(SLUG); prof.ensure_dirs()   # профиль без токенов


def test_login_fail_fast_not_retry() -> None:
    """Без токенов sync падает БЫСТРО (login вне retry), не виснет на retry-budget."""
    t0 = time.time()
    raised = None
    try:
        # явный узкий диапазон (MCP-путь) — чтобы точно 1 окно, но login до окон
        sync_catalog(SLUG, start_date="2026-07-01", end_date="2026-07-07")
    except RuntimeError as exc:
        raised = str(exc)
    elapsed = time.time() - t0
    assert raised is not None, "login-сбой не бросил RuntimeError"
    assert "login" in raised.lower() or "войти" in raised.lower(), raised
    # КЛЮЧЕВОЕ: быстро. retry-budget 360с; login-fail-fast должен быть секунды.
    # Порог 30с — с огромным запасом ниже 360, но выше сетевых задержек login-попытки.
    assert elapsed < 30, f"sync висел {elapsed:.0f}с — login ретраится (регрессия)?"
    print(f"  login-сбой → быстрый отказ за {elapsed:.1f}с (не retry-budget 360с) OK")
    print(f"  сообщение внятное: '{raised[:60]}...' OK")


if __name__ == "__main__":
    test_login_fail_fast_not_retry()
    print("SYNC-RETRY-AUTH тест — ЗЕЛЁНЫЙ")
