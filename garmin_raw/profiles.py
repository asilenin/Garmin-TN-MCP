"""profiles.py — профили и раскладка файлов (§5 ТЗ).

Профиль = атлет на этой машине. Полная изоляция по построению: у каждого свой
каталог токенов, своя БД (источник правды) и своя регистрация коннектора.
Ноль общего мутабельного состояния между профилями.

Раскладка (§5.2):
    ~/.garmin-tn/
      profiles.json                # реестр профилей
      profiles/<slug>/
        tokens/                    # токены Garmin профиля (≈ ~/.garminconnect)
        cache.db                   # SQLite профиля
        state.json                 # last_sync, policy, algo_version, range (зеркало meta)

Выбор профиля процессом — через окружение:
    GARMIN_TN_PROFILE   slug; из него резолвятся все пути
    GARMIN_TOKENSTORE   (legacy) явный путь к токенам, переопределяет tokens/
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(os.environ.get("GARMIN_TN_HOME", "~/.garmin-tn")).expanduser()
REGISTRY = ROOT / "profiles.json"

# slug: строчные буквы/цифры/дефис/подчёркивание, чтобы безопасно ложиться в путь
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _valid_slug(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug))


@dataclass(frozen=True)
class Profile:
    """Резолвенные пути одного профиля. Не лезет в сеть и БД — только пути."""
    slug: str
    base: Path           # profiles/<slug>/
    tokens_dir: Path     # tokens/  (или legacy GARMIN_TOKENSTORE)
    db_path: Path        # cache.db
    state_path: Path     # state.json

    def ensure_dirs(self) -> None:
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


def resolve(slug: str) -> Profile:
    """Пути профиля по slug. Не создаёт ничего на диске (кроме ensure_dirs())."""
    if not _valid_slug(slug):
        raise ValueError(
            f"Недопустимый slug {slug!r}: разрешены [a-z0-9_-], начинается с буквы/цифры."
        )
    base = ROOT / "profiles" / slug
    # legacy: явный GARMIN_TOKENSTORE переопределяет каталог токенов (обратная
    # совместимость с текущим backend.py, где TOKENSTORE = env(GARMIN_TOKENSTORE))
    legacy = os.environ.get("GARMIN_TOKENSTORE")
    tokens_dir = Path(legacy).expanduser() if legacy else base / "tokens"
    return Profile(
        slug=slug,
        base=base,
        tokens_dir=tokens_dir,
        db_path=base / "cache.db",
        state_path=base / "state.json",
    )


def current_slug() -> str:
    """Slug из окружения. Пусто → ошибка с понятной подсказкой."""
    slug = os.environ.get("GARMIN_TN_PROFILE")
    if not slug:
        raise RuntimeError(
            "GARMIN_TN_PROFILE не задан. Укажите профиль: "
            "`GARMIN_TN_PROFILE=<slug> ...` или создайте его `garmin-tn-init <slug>`."
        )
    return slug


def current() -> Profile:
    """Профиль текущего процесса (по GARMIN_TN_PROFILE)."""
    return resolve(current_slug())


# --------------------------------------------------------------------------- #
# Реестр profiles.json
# --------------------------------------------------------------------------- #
def _read_registry() -> list[dict]:
    if not REGISTRY.exists():
        return []
    try:
        return json.loads(REGISTRY.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        # битый реестр не должен валить всё — но и молча терять нельзя
        raise RuntimeError(f"Реестр профилей повреждён: {REGISTRY}")


def _write_registry(items: list[dict]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(REGISTRY)  # атомарная замена


def list_profiles() -> list[dict]:
    return _read_registry()


def create(slug: str, note: str = "") -> Profile:
    """Регистрирует профиль и создаёт его каталоги. Идемпотентно по slug.

    Авторизацию (токены) делает отдельно команда init — здесь только раскладка.
    """
    prof = resolve(slug)
    prof.ensure_dirs()
    items = _read_registry()
    if not any(p["slug"] == slug for p in items):
        items.append({"slug": slug, "note": note, "created": int(time.time())})
        _write_registry(items)
    return prof


def remove(slug: str, *, wipe: bool = False) -> None:
    """Убирает профиль из реестра. wipe=True — удаляет и данные (БД, токены).

    По умолчанию данные НЕ трогаются (источник правды дорогой; снести можно вручную).
    """
    items = [p for p in _read_registry() if p["slug"] != slug]
    _write_registry(items)
    if wipe:
        import shutil
        prof = resolve(slug)
        if prof.base.exists():
            shutil.rmtree(prof.base)


if __name__ == "__main__":
    # быстрый осмотр: что зарегистрировано и куда резолвится
    import sys
    if len(sys.argv) > 1:
        p = resolve(sys.argv[1])
        print(f"slug={p.slug}")
        print(f"tokens_dir={p.tokens_dir}")
        print(f"db_path={p.db_path}")
        print(f"state_path={p.state_path}")
    else:
        print(f"ROOT={ROOT}")
        for item in list_profiles():
            print(item)
