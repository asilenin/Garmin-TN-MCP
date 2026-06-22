"""Тонкий сырьевой бэкенд поверх garminconnect 0.3.x.

Принцип: только СЫРЬЁ. Никаких VO2max / training-effect / device-оценок порога —
их мы по методологии отвергаем. Один бэкенд обслуживает и MCP-сервер, и one-shot
экспорт для тиража.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from garminconnect import Garmin

TOKENSTORE = os.path.expanduser(os.environ.get("GARMIN_TOKENSTORE", "~/.garminconnect"))

# Ключи, выдающие личность владельца — вырезаем при отдаче (гигиена для тиража,
# чтобы не таскать чужие имена/ID в выгрузках, которые уходят в анализ).
# Сравнение РЕГИСТРОНЕЗАВИСИМОЕ: Garmin использует непоследовательный регистр
# (userProfilePK vs userProfilePk vs profileId), поэтому держим ключи в lower-case.
_PII_KEYS = {
    key.lower()
    for key in (
        "ownerId",
        "ownerDisplayName",
        "ownerFullName",
        "ownerProfileImageUrlSmall",
        "ownerProfileImageUrlMedium",
        "ownerProfileImageUrlLarge",
        "userProfilePk",
        "userProfilePK",
        "userProfileId",
        "profileId",
    )
}

# Лактат пишется в комментарий активности как "LA:6.1" (возможны запятая и пробелы).
_LA_RE = re.compile(r"LA[:\s]*([0-9]+(?:[.,][0-9]+)?)", re.IGNORECASE)


def strip_pii(obj: Any) -> Any:
    """Рекурсивно убирает ключи с личностью владельца (регистронезависимо)."""
    if isinstance(obj, dict):
        return {k: strip_pii(v) for k, v in obj.items() if k.lower() not in _PII_KEYS}
    if isinstance(obj, list):
        return [strip_pii(v) for v in obj]
    return obj


def parse_lactate(text: Optional[str]) -> list[float]:
    """Достаёт все значения лактата из текста комментария: 'LA:6.1 @rep10' -> [6.1]."""
    if not text:
        return []
    return [float(x.replace(",", ".")) for x in _LA_RE.findall(text)]


def _first_method(client: Garmin, names: list[str]):
    """Возвращает первый существующий метод клиента из списка кандидатов.

    Нужно для устойчивости к переименованиям методов между версиями garminconnect.
    """
    for n in names:
        fn = getattr(client, n, None)
        if callable(fn):
            return fn, n
    return None, None


class GarminSource:
    """Подключение по сохранённым токенам и сырые выгрузки.

    Логин делается лениво — при первом обращении, по токенам из tokenstore
    (без пароля/MFA). Первичная авторизация — отдельной командой garmin-raw-auth.
    """

    def __init__(self, email: Optional[str] = None, tokenstore: str = TOKENSTORE):
        self.email = email or os.environ.get("GARMIN_EMAIL")
        self.tokenstore = os.path.expanduser(tokenstore)
        self._client: Optional[Garmin] = None

    @property
    def client(self) -> Garmin:
        if self._client is None:
            self._client = self._connect()
        return self._client

    def _connect(self) -> Garmin:
        client = Garmin(self.email) if self.email else Garmin()
        try:
            client.login(self.tokenstore)  # резюм по токенам, без MFA
        except Exception as exc:  # noqa: BLE001 — наружу отдаём понятную причину
            raise RuntimeError(
                f"Не удалось войти по токенам из {self.tokenstore}: {exc}. "
                f"Прогоните `garmin-raw-auth` один раз, чтобы создать/обновить токены."
            ) from exc
        return client

    # ------------------------------------------------------------------ #
    # 6 сырьевых тулзов
    # ------------------------------------------------------------------ #
    def list_activities(self, start: str, end: str, sport: str = "running") -> list[dict]:
        """Сырые сводки активностей за период. Один запрос на весь период."""
        return strip_pii(self.client.get_activities_by_date(start, end, sport))

    def get_activity_laps(self, activity_id: int) -> dict:
        """Данные по кругам (lapDTOs): пульс/каденс/мощность/шаг/высота на круг.

        Рабочая лошадка анализа — то, чего у Runalyze в сплитах не было.
        """
        return strip_pii(self.client.get_activity_splits(activity_id))

    def get_activity_streams(self, activity_id: int) -> dict:
        """Посекундные потоки (HR, каденс, высота, уклон, мощность, шаг, дыхание...).

        Тяжелее кругов — звать только когда круги не дают нужного (динамика на
        подъёме, декаплинг, easy-каденс в окне темпа).
        """
        return strip_pii(self.client.get_activity_details(activity_id))

    def get_activity_comment(self, activity_id: int) -> dict:
        """Комментарий активности (поле description) + распарсенный лактат.

        Лактат вносится в комментарий Garmin Connect как 'LA:x.x'. Зовётся ЛЕНИВО,
        отдельным вызовом — только для активностей, реально идущих в анализ, чтобы
        не удваивать число запросов на весь список (защита от 429).
        """
        full = self.client.get_activity(activity_id)
        desc = full.get("description") if isinstance(full, dict) else None
        return {
            "activity_id": activity_id,
            "description": desc,
            "lactate_mmol": parse_lactate(desc),
        }

    def get_wellness(self, date: str) -> dict:
        """Восстановление за день: сон, HRV, RHR, стресс, Body Battery.

        Имена методов wellness между версиями garminconnect плавают — поэтому
        каждый зовётся через _first_method и при отсутствии отдаёт _error, не роняя
        весь ответ.
        """
        out: dict[str, Any] = {"date": date}
        probes = {
            "sleep": ["get_sleep_data"],
            "hrv": ["get_hrv_data"],
            "rhr": ["get_rhr_day", "get_resting_heart_rate"],
            "stress": ["get_stress_data"],
            "body_battery": ["get_body_battery"],
        }
        for key, names in probes.items():
            fn, _ = _first_method(self.client, names)
            if fn is None:
                out[key] = {"_error": "метод недоступен в этой версии garminconnect"}
                continue
            try:
                out[key] = strip_pii(fn(date, date) if key == "body_battery" else fn(date))
            except Exception as exc:  # noqa: BLE001
                out[key] = {"_error": str(exc)}
        return out

    def get_personal_records(self) -> Any:
        """Личные рекорды по дистанциям (имя метода защищено перебором кандидатов)."""
        fn, _ = _first_method(self.client, ["get_personal_record", "get_personalrecord"])
        if fn is None:
            return {"_error": "метод PR недоступен в этой версии garminconnect"}
        try:
            return strip_pii(fn())
        except Exception as exc:  # noqa: BLE001
            return {"_error": str(exc)}
