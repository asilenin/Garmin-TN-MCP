"""Установка/удаление garmin-raw в конфиг Claude Desktop.

Кроссплатформенно (macOS/Windows/Linux), merge-safe: читает существующий конфиг,
меняет только ключ 'garmin-raw' внутри mcpServers и пишет обратно — твои
preferences/coworkUserFilesPath и прочее остаются нетронутыми. Перед записью —
бэкап.

    uv run garmin-raw-install            # путь к репо берётся автоматически
    uv run garmin-raw-install /path/repo # или явно
    uv run garmin-raw-uninstall
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

SERVER_NAME = "garmin-raw"


def _config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
        return Path(base) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"  # linux


def _repo_dir(argv: list[str]) -> Path:
    if len(argv) > 1:
        repo = Path(argv[1]).expanduser().resolve()
    else:
        repo = Path(__file__).resolve().parents[1]  # корень репозитория
    if not (repo / "pyproject.toml").exists():
        sys.exit(f"В {repo} нет pyproject.toml — укажи путь к репозиторию аргументом.")
    return repo


def _uv_bin() -> str:
    found = shutil.which("uv")
    if found:
        return found
    for cand in (Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv"):
        if cand.exists():
            return str(cand)
    sys.exit("Не найден uv. Установи uv (https://astral.sh/uv) и повтори.")


def _load(cfg: Path) -> dict:
    if not cfg.exists():
        return {}
    try:
        return json.loads(cfg.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        sys.exit(f"{cfg} — невалидный JSON. Поправь вручную и повтори.")


def _save(cfg: Path, data: dict) -> None:
    cfg.parent.mkdir(parents=True, exist_ok=True)
    if cfg.exists():
        shutil.copy2(cfg, cfg.with_suffix(cfg.suffix + f".bak.{int(time.time())}"))
    cfg.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    cfg = _config_path()
    repo = _repo_dir(sys.argv)
    uv = _uv_bin()
    data = _load(cfg)
    data.setdefault("mcpServers", {})[SERVER_NAME] = {
        "command": uv,
        "args": ["--directory", str(repo), "run", "garmin-raw-mcp"],
    }
    _save(cfg, data)
    print(
        f"OK: '{SERVER_NAME}' прописан в {cfg}\n"
        f"  uv:   {uv}\n"
        f"  repo: {repo}\n"
        f"Перезапусти Claude Desktop (Cmd+Q), чтобы тулзы появились."
    )


def uninstall() -> None:
    cfg = _config_path()
    data = _load(cfg)
    if data.get("mcpServers", {}).pop(SERVER_NAME, None) is None:
        print(f"'{SERVER_NAME}' не найден в {cfg} — нечего удалять.")
        return
    _save(cfg, data)
    print(
        f"OK: '{SERVER_NAME}' удалён из {cfg}. Перезапусти Claude Desktop (Cmd+Q).\n"
        f"Токены не тронуты. Удалить их при желании: rm -rf ~/.garminconnect"
    )


def _repo_from(repo_arg: str | None) -> Path:
    repo = (Path(repo_arg).expanduser().resolve() if repo_arg
            else Path(__file__).resolve().parents[1])
    if not (repo / "pyproject.toml").exists():
        sys.exit(f"В {repo} нет pyproject.toml — укажи путь к репозиторию аргументом.")
    return repo


def _parse_tn_args(argv: list[str]) -> tuple[str, str, str | None, str | None]:
    """<provider> <user> [repo] [--tokenstore PATH]. → (provider, user, repo|None, tokenstore|None)."""
    args, positional, tokenstore = argv[1:], [], None
    i = 0
    while i < len(args):
        if args[i] == "--tokenstore":
            i += 1
            if i >= len(args):
                sys.exit("--tokenstore требует путь")
            tokenstore = args[i]
        else:
            positional.append(args[i])
        i += 1
    if len(positional) < 2:
        sys.exit("Использование: tn-install <provider> <user> [repo] [--tokenstore PATH]\n"
                 "  напр. tn-install garmin anton --tokenstore ~/.garminconnect")
    return (positional[0], positional[1],
            (positional[2] if len(positional) > 2 else None), tokenstore)


def install_tn() -> None:
    """Enriched-коннектор в конфиг Claude. Ключ tn-<provider>-<user>, env TN_USER/TN_PROVIDER
    (подключение выбирается транспортом, не моделью — CI-PROVIDER-BY-TRANSPORT). slug =
    <provider>-<user> (build_slug). GARMIN_TOKENSTORE — ТОЛЬКО по флагу --tokenstore (не дефолт:
    хардкод личного пути заглушил бы диагностику 'подключение не настроено' на чужой машине)."""
    try:                       # install — пакетный entry (from .), тест — флэт (import)
        from . import profiles
    except ImportError:
        import profiles

    provider, user, repo_arg, tokenstore = _parse_tn_args(sys.argv)
    try:
        slug = profiles.build_slug(user, provider)
    except ValueError as e:
        sys.exit(str(e))
    cfg, repo, uv = _config_path(), _repo_from(repo_arg), _uv_bin()
    key = f"tn-{slug}"
    env = {"TN_USER": user, "TN_PROVIDER": provider}
    if tokenstore:
        env["GARMIN_TOKENSTORE"] = str(Path(tokenstore).expanduser())

    data = _load(cfg)
    data.setdefault("mcpServers", {})[key] = {
        "command": uv,
        "args": ["--directory", str(repo), "run", "garmin-tn-mcp"],
        "env": env,
    }
    _save(cfg, data)

    # Грабля первой установки: профиль без СВОИХ токенов и без --tokenstore → auth
    # упадёт. Критерий — НЕПУСТОТА profiles/<slug>/tokens (тот же, что resolve в T7.5-1;
    # .exists() обманулся бы на пустой папке от ensure_dirs). Предупреждаем, не отказ.
    prof_tokens = profiles.ROOT / "profiles" / slug / "tokens"
    has_own = prof_tokens.is_dir() and any(prof_tokens.iterdir())
    warn = ""
    if not has_own and not tokenstore:
        warn = (f"\n⚠ profile '{slug}': нет своих токенов ({prof_tokens}) и не задан "
                f"--tokenstore.\n  auth упадёт при первом вызове. Передай "
                f"--tokenstore <path> (напр. ~/.garminconnect) ИЛИ положи токены в "
                f"{prof_tokens}.")
    print(f"OK: '{key}' прописан в {cfg}\n  uv:   {uv}\n  repo: {repo}\n  env:  {env}\n"
          f"Перезапусти Claude Desktop (Cmd+Q).{warn}")


def uninstall_tn() -> None:
    """Убрать enriched-коннектор tn-<provider>-<user>. Сырьевой garmin-raw и другие
    подключения не трогаются."""
    provider, user, _, _ = _parse_tn_args(sys.argv)
    cfg = _config_path()
    key = f"tn-{provider}-{user}"
    data = _load(cfg)
    if data.get("mcpServers", {}).pop(key, None) is None:
        print(f"'{key}' не найден в {cfg} — нечего удалять.")
        return
    _save(cfg, data)
    print(f"OK: '{key}' удалён из {cfg}. Перезапусти Claude Desktop (Cmd+Q).")


if __name__ == "__main__":
    main()
