"""sport_taxonomy.py — единый источник классификации Garmin typeKey.

Нейтральный модуль (БЕЗ numpy/сети) — импортируем из enrich (gps_type) и tools
(sport_class фильтр), не потянув тяжёлого. Причина существования: несколько
производных классификаций от ОДНОГО факта (typeKey Garmin) — gps_type (GPS-среда),
sport_class (беговой/не-беговой класс), и будущие (cross-training, MVP-расширение).

СТРУКТУРНАЯ гарантия полноты: производные НЕ отдельные словари, синхронизируемые
вручную (забыл ключ в одном → разъезд, ловится лишь тестом конкретной пары). Вместо
этого — ОДНА таблица _TAXONOMY: typeKey → (gps_type, sport_class). Добавить typeKey =
одна строка, дающая ВСЕ производные разом. «Забыл в одном словаре» структурно
невозможно — значения одного typeKey живут в одной строке, не в параллельных dict.

Новый производный признак (cross-training и т.п.) = новая КОЛОНКА таблицы, не новый
параллельный словарь: та же строка typeKey расширяется, полнота сохраняется по
построению. Расширение номенклатуры (новый вид спорта) = новая строка со ВСЕМИ
колонками — пропуск колонки = явная ошибка в строке, видимая на месте.

Демаркация: это словарь ФАКТОВ номенклатуры Garmin (treadmill_running буквально «на
дорожке»), не суждение с порогом. Механический перевод, как hr_source по balance.
Неизвестный typeKey → None во всех производных (не гадаем — расширяемо новой строкой).
"""
from __future__ import annotations

from typing import NamedTuple, Optional


class _SportInfo(NamedTuple):
    gps_type: Optional[str]      # GPS-среда: outdoor/treadmill/track/indoor
    sport_class: str             # грубый класс: run/ride/swim/strength/other


# ЕДИНАЯ таблица номенклатуры. Одна строка на typeKey — все производные разом.
# gps_type: indoor ОТДЕЛЬНО от treadmill (belt-assist vs пол, разные GCT/vert §5.4).
# sport_class: все *_running → run (union для «сколько пробежек» — RUN-CLASS-PREDICATE).
_TAXONOMY: dict[str, _SportInfo] = {
    "running":            _SportInfo(gps_type="outdoor",   sport_class="run"),
    "trail_running":      _SportInfo(gps_type="outdoor",   sport_class="run"),
    "treadmill_running":  _SportInfo(gps_type="treadmill", sport_class="run"),
    "track_running":      _SportInfo(gps_type="track",     sport_class="run"),
    "indoor_running":     _SportInfo(gps_type="indoor",    sport_class="run"),
    # cross-training (CROSS-TRAINING-SCOPE) добавит строки: cycling→(None,"ride"),
    # lap_swimming→(None,"swim"), strength_training→(None,"strength") и т.д. —
    # каждая новая строка даёт ОБА признака, полнота по построению.
}

# Известные typeKey — производное от таблицы (единственный источник).
KNOWN_TYPE_KEYS = frozenset(_TAXONOMY)

# Производные словари — СТРОЯТСЯ из таблицы, не пишутся параллельно.
GPS_TYPE_BY_SPORT: dict[str, Optional[str]] = {
    k: v.gps_type for k, v in _TAXONOMY.items()
}
# sport_class → набор typeKey (для разворота фильтра sport_class в sport IN (...)).
_TYPE_KEYS_BY_CLASS: dict[str, frozenset[str]] = {}
for _k, _v in _TAXONOMY.items():
    _TYPE_KEYS_BY_CLASS.setdefault(_v.sport_class, set()).add(_k)  # type: ignore[arg-type]
_TYPE_KEYS_BY_CLASS = {c: frozenset(ks) for c, ks in _TYPE_KEYS_BY_CLASS.items()}


def gps_type_from_sport(sport: Optional[str]) -> Optional[str]:
    """typeKey → gps_type. Неизвестный/None → None (не гадаем, расширяемо строкой)."""
    if sport is None:
        return None
    info = _TAXONOMY.get(sport)
    return info.gps_type if info else None


def type_keys_for_class(sport_class: str) -> Optional[frozenset[str]]:
    """sport_class ('run'/'ride'/...) → набор typeKey для sport IN (...).
    Неизвестный класс → None (не разворачиваем в пустоту молча — вызывающий решает)."""
    return _TYPE_KEYS_BY_CLASS.get(sport_class)


def sport_class_of(sport: Optional[str]) -> Optional[str]:
    """typeKey → sport_class. Неизвестный/None → None."""
    if sport is None:
        return None
    info = _TAXONOMY.get(sport)
    return info.sport_class if info else None
