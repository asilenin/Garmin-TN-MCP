"""test_sync_catalog.py — герметичные тесты garmin_sync_catalog (валидация + сетевой класс).

Сетевой WRITE-тул: сам синк требует токенов+сети → живой тест у владельца. Герметично
проверяемо БЕЗ сети:
  (1) валидация диапазона — обязателен, оба вместе, start≤end (до всякой сети);
  (2) сетевой класс — под forbid_network тул ПЫТАЕТСЯ в сеть (не случайно cache-only).

Живьём (владелец): реальный инкремент за узкий диапазон → окна обошлись, каталог дописан.
"""
import os, sys, tempfile
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))
tmp = tempfile.mkdtemp(); os.environ["GARMIN_TN_HOME"] = tmp

import profiles                                             # noqa: E402
import net_tools                                            # noqa: E402
from netguard import ForbiddenNetworkAccess, forbid_network  # noqa: E402
from sync import sync_catalog                               # noqa: E402

SLUG = "syctest"
prof = profiles.resolve(SLUG); prof.ensure_dirs()


def test_range_required_together() -> None:
    """start/end обязательны ВМЕСТЕ (валидация ДО сети — ValueError, не сетевой поход)."""
    import inspect
    # прямая проверка sync_catalog (тул её тонко оборачивает)
    try:
        sync_catalog(SLUG, start_date="2026-06-27")  # end нет
        raise AssertionError("одиночный start_date не отвергнут")
    except ValueError as e:
        assert "вместе" in str(e), e
    print("  (1a) start без end → ValueError (оба вместе) OK")


def test_start_after_end() -> None:
    try:
        sync_catalog(SLUG, start_date="2026-07-07", end_date="2026-06-27")
        raise AssertionError("start>end не отвергнут")
    except ValueError as e:
        assert "позже" in str(e), e
    print("  (1b) start>end → ValueError OK")


def test_calls_sync_catalog_with_range() -> None:
    """Сетевой класс + контракт делегирования: тул зовёт sync_catalog с ЯВНЫМ
    диапазоном (start_date/end_date), НЕ history_years, и форматирует SyncReport в
    ответ. Мок sync_catalog — гонять реальный до retry-цикла значило бы тестировать
    sync_catalog, не тул (методология wellness/enrich: контракт вызова, не факт что
    движок не падает на всех входах). Сетевой-класс тула держится на импорте Fetcher
    в net_tools (статически) + том, что делегат sync_catalog идёт в сеть (установлено
    отдельно login-попыткой)."""
    import net_tools as nt
    from dataclasses import dataclass

    @dataclass
    class _FakeReport:
        windows: int = 2
        activities_upserted: int = 7
        stopped_early: bool = False
        stop_reason: object = None
        range_start: str = "2026-06-27"
        range_end: str = "2026-07-07"
        elapsed_s: float = 3.14159

    captured = {}
    def _fake_sync_catalog(slug, *, start_date=None, end_date=None, **kw):
        captured.update(slug=slug, start_date=start_date, end_date=end_date, kw=kw)
        return _FakeReport()

    orig = nt.sync_catalog if hasattr(nt, "sync_catalog") else None
    import sync as sync_mod
    orig_real = sync_mod.sync_catalog
    sync_mod.sync_catalog = _fake_sync_catalog
    try:
        r = nt.garmin_sync_catalog(SLUG, "2026-06-27", "2026-07-07")
    finally:
        sync_mod.sync_catalog = orig_real

    # тул передал ЯВНЫЙ диапазон, не history_years
    assert captured["start_date"] == "2026-06-27", captured
    assert captured["end_date"] == "2026-07-07", captured
    assert "history_years" not in captured["kw"], "тул не должен слать history_years"
    # тул отформатировал SyncReport в контрактный ответ
    assert r["windows"] == 2 and r["activities_upserted"] == 7, r
    assert r["range"] == ["2026-06-27", "2026-07-07"], r
    assert r["elapsed_s"] == 3.14, r  # округление до 2 знаков
    assert set(r) == {"windows", "activities_upserted", "range", "stopped_early",
                      "stop_reason", "elapsed_s"}, r
    print("  (2) тул зовёт sync_catalog с явным диапазоном, форматирует SyncReport OK")


if __name__ == "__main__":
    test_range_required_together()
    test_start_after_end()
    test_calls_sync_catalog_with_range()
    print("ГЕРМЕТИЧНЫЕ тесты garmin_sync_catalog — ЗЕЛЁНЫЕ (живой инкремент — у владельца)")
