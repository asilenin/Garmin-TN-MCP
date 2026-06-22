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


if __name__ == "__main__":
    main()
