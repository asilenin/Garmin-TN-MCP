"""fetch.py — сетевой слой поверх garminconnect 0.3.x (этап 2, §14).

Единственное место, где код ходит в сеть. Политика (§4, решение пользователя):
  - ПЕРВИЧНО: консервативный заданный темп — пауза между вызовами, чтобы 429
    почти не возникал. Предсказуемо, ценой времени.
  - СТРАХОВКА: если 429/5xx всё же прилетел — экспоненциальный backoff с лимитом
    попыток, затем исключение наверх (драйвер sync поймает и остановится чисто).
  - ВОЗОБНОВЛЯЕМОСТЬ — не здесь: драйвер sync пропускает уже скачанное через
    store.has_raw(). Темп защищает от бана, resume — от обрыва (сон/сеть/Ctrl+C).

Логин — ленивый, по сохранённым токенам профиля (без пароля/MFA), как в текущем
GarminSource. Первичная авторизация — отдельной командой init.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

from garminconnect import Garmin

# Темп по умолчанию: пауза между сетевыми вызовами, сек. Консервативно.
# Калибруется на реальном архиве (§12.5) — здесь безопасный старт.
DEFAULT_PACE_S = float(os.environ.get("GARMIN_TN_PACE_S", "1.5"))
# Backoff-страховка fetch.py — ТОЛЬКО для мелких разовых сбоев (моргнула сеть,
# одиночный 5xx): быстро ретраит, ~1 минута суммарно, потом отдаёт RateLimited
# наверх. Долгое терпение («Garmin прилёг на минуты») живёт НЕ здесь, а в
# sync.py на уровне окна, где уже сохранён чекпойнт (предыдущие окна в БД).
MAX_RETRIES = int(os.environ.get("GARMIN_TN_MAX_RETRIES", "4"))
BACKOFF_BASE_S = float(os.environ.get("GARMIN_TN_BACKOFF_BASE_S", "5"))
BACKOFF_CAP_S = float(os.environ.get("GARMIN_TN_BACKOFF_CAP_S", "40"))


class RateLimited(RuntimeError):
    """429/5xx не отступили за MAX_RETRIES — драйвер sync должен остановиться чисто."""


def _is_rate_limit(exc: Exception) -> bool:
    s = str(exc).lower()
    return "429" in s or "too many" in s or "rate" in s or "5xx" in s \
        or any(c in s for c in ("500", "502", "503", "504"))


class Fetcher:
    """Троттлящий клиент. Все сетевые вызовы идут через _call().

    Использование:
        f = Fetcher(tokenstore=profile.tokens_dir)
        acts = f.list_activities("2006-01-01", "2026-12-31")
        laps = f.get_laps(activity_id)
    """

    def __init__(
        self,
        tokenstore: str | os.PathLike,
        *,
        email: Optional[str] = None,
        pace_s: float = DEFAULT_PACE_S,
    ):
        self.tokenstore = os.fspath(tokenstore)
        self.email = email or os.environ.get("GARMIN_EMAIL")
        self.pace_s = pace_s
        self._client: Optional[Garmin] = None
        self._last_call_ts = 0.0

    # ------------------------------------------------------------------ #
    @property
    def client(self) -> Garmin:
        if self._client is None:
            self._client = self._connect()
        return self._client

    def _connect(self) -> Garmin:
        client = Garmin(self.email) if self.email else Garmin()
        try:
            client.login(self.tokenstore)  # резюм по токенам, без MFA
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Не удалось войти по токенам из {self.tokenstore}: {exc}. "
                f"Прогоните init для этого профиля, чтобы создать/обновить токены."
            ) from exc
        return client

    # ------------------------------------------------------------------ #
    def _throttle(self) -> None:
        """Консервативный темп: держим минимум pace_s между вызовами."""
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < self.pace_s:
            time.sleep(self.pace_s - elapsed)

    def _call(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """Один сетевой вызов: throttle перед, backoff-страховка при 429/5xx."""
        attempt = 0
        while True:
            self._throttle()
            try:
                result = fn(*args, **kwargs)
                self._last_call_ts = time.monotonic()
                return result
            except Exception as exc:  # noqa: BLE001
                self._last_call_ts = time.monotonic()
                if not _is_rate_limit(exc):
                    raise  # не rate-limit — наверх как есть
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise RateLimited(
                        f"429/5xx не отступил за {MAX_RETRIES} попыток: {exc}"
                    ) from exc
                wait = min(BACKOFF_BASE_S * (2 ** (attempt - 1)), BACKOFF_CAP_S)
                time.sleep(wait)

    # ------------------------------------------------------------------ #
    # Сырьевые методы — тонкие обёртки над garminconnect. Без обработки данных:
    # обработка/strip_pii живёт в backend/enrich, fetch только достаёт сырьё.
    # ------------------------------------------------------------------ #
    def list_activities(self, start: str, end: str, sport: str = "running") -> list[dict]:
        # ВНИМАНИЕ: библиотека пагинирует внутри (~41 c на 2000 активностей —
        # проверено на живом аккаунте). Это десятки запросов под капотом, но
        # с нашим throttle между внешними вызовами здесь один внешний вызов.
        return self._call(self.client.get_activities_by_date, start, end, sport)

    def get_laps(self, activity_id: int) -> dict:
        return self._call(self.client.get_activity_splits, activity_id)

    def get_streams(self, activity_id: int) -> dict:
        return self._call(self.client.get_activity_details, activity_id)

    def get_full_activity(self, activity_id: int) -> dict:
        return self._call(self.client.get_activity, activity_id)


# --------------------------------------------------------------------------- #
# Сверка маппинга на ЖИВОЙ выдаче (§14 этап 2: «проверить ключи до наполнения»).
# Запуск: GARMIN_TN_PROFILE=<slug> python fetch.py
# Берёт 3 свежие активности, показывает, какие поля каталога извлеклись, а какие
# вышли NULL — чтобы поймать расхождение ключей Garmin до массового sync.
# --------------------------------------------------------------------------- #
def _verify_mapping() -> None:
    import json
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import profiles  # noqa: E402
    from store import activity_row_from_summary  # noqa: E402

    prof = profiles.current()
    f = Fetcher(tokenstore=prof.tokens_dir)
    # узкое окно, чтобы не тянуть весь архив ради проверки ключей
    acts = f.list_activities("2026-06-01", "2026-12-31")
    print(f"получено активностей: {len(acts)}")
    if not acts:
        print("пусто — расширьте окно дат в _verify_mapping()")
        return
    for a in acts[:3]:
        row = activity_row_from_summary(a)
        nulls = [k for k, v in row.items() if v is None and k != "summary_json"]
        print("-" * 60)
        print(f"activity_id={row['activity_id']} date={row['date']} "
              f"sport={row['sport']} dist={row['distance_m']} maxHR={row['max_hr']}")
        if nulls:
            print(f"  NULL-поля каталога (проверить ключи Garmin): {nulls}")
        # покажем сырые ключи сводки, чтобы видеть реальные имена
        print(f"  сырые ключи сводки: {sorted(a.keys())[:20]} ...")


if __name__ == "__main__":
    _verify_mapping()
