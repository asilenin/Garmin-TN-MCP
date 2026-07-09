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

Выбор подключения процессом — через окружение (CI-PROVIDER-BY-TRANSPORT):
    TN_USER / TN_PROVIDER   подключение = (user, provider); slug = <provider>-<user>
    GARMIN_TN_PROFILE       (legacy) bare user, provider=garmin (обратная совместимость)
    GARMIN_TOKENSTORE       (legacy) явный путь к токенам, переопределяет tokens/
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


_DEFAULT_PROVIDER = "garmin"


def build_slug(user: str, provider: str | None = None) -> str:
    """Ключ подключения <provider>-<user>. provider дефолтит в garmin. Валидирует обе части.
    slug — непрозрачный ключ-каталог (обратно в (provider,user) не парсится → дефис внутри
    user безопасен: provider/user несёт env раздельно)."""
    provider = (provider or _DEFAULT_PROVIDER).strip().lower()
    user = (user or "").strip()
    if not _valid_slug(provider):
        raise ValueError(f"Недопустимый provider {provider!r}: [a-z0-9_-], с буквы/цифры.")
    if not _valid_slug(user):
        raise ValueError(f"Недопустимый user {user!r}: [a-z0-9_-], с буквы/цифры.")
    return f"{provider}-{user}"


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
    # Токены (I3 backlog): ПРОФИЛЬ-first → фолбэк на общий GARMIN_TOKENSTORE.
    # НЕ «общий всегда»: профиль со своими токенами (разный Garmin-аккаунт) обязан
    # использовать их, иначе один сервер тянет из Garmin под чужим аккаунтом.
    # Проверяем НЕПУСТОТУ, не .exists(): ensure_dirs()/create() сами создают пустой
    # profiles/<slug>/tokens/ — по .exists() он затенил бы фолбэк навсегда. Пустая
    # профильная папка → игнорируем, идём на общий (или дефолт-путь для будущего auth).
    prof_tokens = base / "tokens"
    legacy = os.environ.get("GARMIN_TOKENSTORE")
    if prof_tokens.is_dir() and any(prof_tokens.iterdir()):
        tokens_dir = prof_tokens                       # профиль имеет свои токены
    elif legacy:
        tokens_dir = Path(legacy).expanduser()         # фолбэк на общий
    else:
        tokens_dir = prof_tokens                       # дефолт: сюда auth положит токены
    return Profile(
        slug=slug,
        base=base,
        tokens_dir=tokens_dir,
        db_path=base / "cache.db",
        state_path=base / "state.json",
    )


def current_slug() -> str:
    """Slug подключения из окружения: <provider>-<user>.

    Приоритет: TN_USER (+ TN_PROVIDER, дефолт garmin) → legacy GARMIN_TN_PROFILE (bare user,
    provider=garmin) → ошибка. Legacy-фолбэк держит живые Garmin-коннекторы рабочими после
    миграции раскладки на profiles/<provider>-<user>/ (CI-PROVIDER-BY-TRANSPORT)."""
    user = os.environ.get("TN_USER")
    if user:
        return build_slug(user, os.environ.get("TN_PROVIDER"))
    legacy = os.environ.get("GARMIN_TN_PROFILE")
    if legacy:
        return build_slug(legacy, _DEFAULT_PROVIDER)
    raise RuntimeError(
        "Подключение не задано. Укажите TN_USER (+опц. TN_PROVIDER, дефолт garmin) в env "
        "коннектора, напр. TN_USER=anton TN_PROVIDER=garmin. Legacy GARMIN_TN_PROFILE=<user> "
        "тоже принимается (provider=garmin)."
    )


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
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        import tempfile
        # T7.5-1: токен-резолв профиль-first → фолбэк общий, с ловушкой пустой папки
        with tempfile.TemporaryDirectory() as d:
            P = sys.modules[__name__]
            P.ROOT = Path(d)                    # resolve() читает ROOT как глобал при вызове
            shared = Path(d) / "shared"; shared.mkdir(); (shared / "garmin_tokens.json").write_text("{}")
            os.environ["GARMIN_TOKENSTORE"] = str(shared)

            # (1) нет профильных токенов → фолбэк на общий
            p = P.resolve("anton")
            assert p.tokens_dir == shared, ("fallback", p.tokens_dir)

            # (2) профильные токены есть и НЕПУСТЫ → профиль-first (перебивает общий)
            pt = Path(d) / "profiles" / "mila" / "tokens"; pt.mkdir(parents=True)
            (pt / "oauth.json").write_text("{}")
            p = P.resolve("mila")
            assert p.tokens_dir == pt, ("profile-first", p.tokens_dir)

            # (3) ЛОВУШКА: профильная папка есть, но ПУСТА → игнор, фолбэк на общий
            empty = Path(d) / "profiles" / "bob" / "tokens"; empty.mkdir(parents=True)
            p = P.resolve("bob")
            assert p.tokens_dir == shared, ("empty-dir → fallback", p.tokens_dir)

            # (4) ни профильных, ни общего → дефолт-путь (auth туда положит)
            del os.environ["GARMIN_TOKENSTORE"]
            p = P.resolve("carol")
            assert p.tokens_dir == Path(d) / "profiles" / "carol" / "tokens", p.tokens_dir

            # (5) build_slug / current_slug: TN_USER+TN_PROVIDER, legacy fallback
            assert P.build_slug("anton") == "garmin-anton"
            assert P.build_slug("andrey", "coros") == "coros-andrey"
            for k in ("TN_USER", "TN_PROVIDER"):
                os.environ.pop(k, None)
            os.environ["GARMIN_TN_PROFILE"] = "mila"
            assert P.current_slug() == "garmin-mila", "legacy → garmin-<user>"
            os.environ["TN_USER"] = "andrey"; os.environ["TN_PROVIDER"] = "coros"
            assert P.current_slug() == "coros-andrey", "новый env перебивает legacy"
            for k in ("TN_USER", "TN_PROVIDER", "GARMIN_TN_PROFILE"):
                os.environ.pop(k, None)
        print("profiles self-test OK")
        sys.exit(0)
    # быстрый осмотр: что зарегистрировано и куда резолвится
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
