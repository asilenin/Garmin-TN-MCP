"""test_argv_safety.py — CLI-диспетчер sync.py: нераспознанная подкоманда → ошибка,
НЕ молчаливый сетевой синк (вектор wipe, backlog ARGV-SAFETY).

Раньше любой нераспознанный argv[2] проваливался в дефолтный sync_catalog (полный
горизонт). Проверяем различитель «есть ли argv[2]»: голый slug → синк (легитимный
дефолт), нераспознанная подкоманда → exit 2 + usage. Subprocess (тестируем argv-парсинг
как процесс, не функцию). Сетевые ветки НЕ доводим до сети (проверяем ранний выход/
намерение синка по stdout, не результат)."""
import os, sys, subprocess, tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SYNC = os.path.join(_ROOT, "garmin_raw", "sync.py")
_ENV = {**os.environ, "GARMIN_TN_HOME": tempfile.mkdtemp()}


def _run(*args, timeout=15):
    return subprocess.run([sys.executable, _SYNC, *args], capture_output=True,
                          text=True, env=_ENV, timeout=timeout)


def test_unknown_subcommand_errors() -> None:
    """Нераспознанная подкоманда → exit 2 + usage, НЕ синк."""
    r = _run("anton", "statsu", timeout=10)
    assert r.returncode == 2, f"нераспознанная не дала exit 2: {r.returncode}"
    assert "неизвестная подкоманда" in r.stdout, r.stdout
    # маркер РЕАЛЬНОГО запуска синка — строка старта с «окна по» (не usage-подсказка,
    # где тоже есть слова «sync каталога»)
    assert "окна по" not in r.stdout, "провалилась в синк вместо ошибки!"
    print("  нераспознанная подкоманда → exit 2 + usage, НЕ синк OK")


def test_bare_slug_attempts_sync() -> None:
    """Голый slug (нет подкоманды) → идёт в синк (легитимный дефолт сохранён).
    Без токенов синк упрётся в login-retry (виснет ~6мин, SYNC-RETRY-AUTH) — поэтому
    НЕ ждём завершения: читаем stdout до маркера старта синка и убиваем процесс.
    Проверяем НАМЕРЕНИЕ синка (маркер печатается до сети), не результат."""
    import subprocess as _sp, time
    p = _sp.Popen([sys.executable, _SYNC, "anton"], stdout=_sp.PIPE, stderr=_sp.STDOUT,
                  text=True, env=_ENV)
    saw_sync = False
    t0 = time.time()
    try:
        # читаем построчно до маркера или короткого дедлайна
        while time.time() - t0 < 10:
            line = p.stdout.readline()
            if not line:
                break
            if "окна по" in line:
                saw_sync = True
                break
            if "неизвестная подкоманда" in line:
                break
    finally:
        p.kill()
        p.wait(timeout=5)
    assert saw_sync, "голый slug не пошёл в синк (маркер старта не появился)"
    print("  голый slug → синк (легитимный дефолт цел; процесс убит до login-retry) OK")


def test_recognized_offline_not_flagged() -> None:
    """Распознанная офлайн-подкоманда (recompute) НЕ ловится как unknown."""
    r = _run("anton", "recompute", timeout=15)
    assert "неизвестная подкоманда" not in r.stdout, "recompute ошибочно как unknown"
    print("  recompute (распознанная) не помечена unknown OK")


if __name__ == "__main__":
    test_unknown_subcommand_errors()
    test_bare_slug_attempts_sync()
    test_recognized_offline_not_flagged()
    print("ARGV-SAFETY тесты — ЗЕЛЁНЫЕ")
