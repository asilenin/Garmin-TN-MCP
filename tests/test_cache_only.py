"""test_cache_only.py — ЗАМОК: tools.py cache-only ПО ПОСТРОЕНИЮ (этап 7.6).

Инвариант (QA INV-NET-GUARD): read-слой tools.py не ходит в сеть НИ НА КАКОМ пути. Проверяется
ДВУМЯ независимыми контурами (как netguard socket/http.client — непересекающиеся
слепые зоны):
  - СТАТИЧЕСКИЙ: tools.py не импортирует Fetcher (grep-проверяемый структурный
    признак; пометка-комментарий дрейфовала бы бесшумно);
  - ДИНАМИЧЕСКИЙ: каждый публичный тул под forbid_network НЕ бросает
    ForbiddenNetworkAccess (что бы ни вернул — сеть не тронул).

Полнота (не забыть новый тул) и инвариант — РАЗДЕЛЕНЫ (паттерн [3a]/[3b] netguard):
полнота интроспекцией (без вызова), инвариант вызовом с явными аргументами. Новый
тул в tools.py валит тест полноты (нет в реестре) ДО того, как забудется в проверке.
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))

tmp = tempfile.mkdtemp()
os.environ["GARMIN_TN_HOME"] = tmp

import profiles                                    # noqa: E402
import tools                                       # noqa: E402
from netguard import ForbiddenNetworkAccess, forbid_network  # noqa: E402

SLUG = "cotest"
prof = profiles.resolve(SLUG); prof.ensure_dirs()  # пустой профиль (schema v6)


# --- реестр: тул → аргументы для динамического вызова под запретом сети ---
# id/mark_id фиктивные: замку не нужен валидный результат, только «не полез в сеть».
# Любой не-сетевой исход (пустой ответ, None, KeyError на отсутствии) — ЗЕЛЁНО;
# красное — ТОЛЬКО ForbiddenNetworkAccess.
TOOL_ARGS = {
    "query_index":          ((SLUG,), {}),
    "get_activity_compact": ((SLUG, 999999), {}),
    "get_activity_full":    ((SLUG, 999999), {}),
    "cache_status":         ((SLUG,), {}),
    "get_period_aggregates": ((SLUG,), {}),
    "add_lactate":          ((SLUG, 999999, 5.0), {}),
    "add_note":             ((SLUG, 999999, "x"), {}),
    "delete_lactate":       ((SLUG, 999999), {}),
    "enrich_activity":      ((SLUG, 999999), {}),
    "enrich_estimate":      ((SLUG,), {}),
}


def _public_tools(mod) -> set:
    """Публичные функции, ОПРЕДЕЛЁННЫЕ в самом модуле (не импорты, не хелперы)."""
    import inspect
    out = set()
    for name, obj in vars(mod).items():
        if name.startswith("_"):
            continue
        if inspect.isfunction(obj) and obj.__module__ == mod.__name__:
            out.add(name)
    return out


def test_static_no_fetcher_import() -> None:
    """СТАТИЧЕСКИЙ контур: tools.py не импортирует сетевой слой."""
    src = open(os.path.join(_ROOT, "garmin_raw", "tools.py"), encoding="utf-8").read()
    for banned in ("from fetch import", "import fetch", "from garminconnect",
                   "import garminconnect", "from net_tools", "import net_tools"):
        assert banned not in src, (
            f"tools.py импортирует сетевое ('{banned}') — cache-only нарушен. "
            f"Сетевые тулы живут в net_tools.py."
        )
    print("  СТАТИЧЕСКИЙ: tools.py не импортирует Fetcher/garminconnect/net_tools OK")


def test_completeness() -> None:
    """ПОЛНОТА: каждый публичный тул tools.py есть в реестре TOOL_ARGS (иначе новый
    тул тихо выпадет из динамической проверки)."""
    actual = _public_tools(tools)
    registered = set(TOOL_ARGS)
    missing = actual - registered
    extra = registered - actual
    assert not missing, (
        f"тулы tools.py БЕЗ покрытия замком: {sorted(missing)} — добавь в TOOL_ARGS "
        f"(и проверь, что тул действительно cache-only)."
    )
    assert not extra, (
        f"в TOOL_ARGS есть несуществующие тулы: {sorted(extra)} — удалены из tools.py?"
    )
    print(f"  ПОЛНОТА: все {len(actual)} публичных тулов tools.py в реестре OK")


def test_dynamic_no_network() -> None:
    """ДИНАМИЧЕСКИЙ контур: каждый тул под forbid_network НЕ бросает
    ForbiddenNetworkAccess (что бы ни вернул — сеть не тронул)."""
    for name, (args, kwargs) in TOOL_ARGS.items():
        fn = getattr(tools, name)
        with forbid_network():
            try:
                fn(*args, **kwargs)
            except ForbiddenNetworkAccess as e:
                raise AssertionError(
                    f"тул {name} ПОЛЕЗ В СЕТЬ под запретом (контур {e.contour}): {e} "
                    f"— cache-only нарушен."
                )
            except Exception:  # noqa: BLE001
                pass  # любой НЕ-сетевой провал — ок (нет активности → пусто/ошибка)
        print(f"  ДИНАМИЧЕСКИЙ: {name} под запретом сети — не полез OK")


if __name__ == "__main__":
    test_static_no_fetcher_import()
    test_completeness()
    test_dynamic_no_network()
    print("ЗАМОК cache-only tools.py — ЗЕЛЁНЫЙ (статика + полнота + динамика)")
