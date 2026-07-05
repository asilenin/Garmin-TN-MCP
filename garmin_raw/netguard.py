"""netguard.py — страж «сеть только через fetch.py» (этап 7.6, контракт сетевых тулов).

Инвариант (QA 7.6 Q4): read-тулы cache-only не ходят в сеть НИ НА КАКОМ пути; сетевые
тулы (garmin_wellness, garmin_sync) ходят ТОЛЬКО через fetch.py. Признак «сетевой» —
декларативный, но декларация дрейфует от тела → нужен страж, доказывающий соответствие
ПО ФАКТУ (не по этикетке).

Страж = процессный запрет сети на границе, ниже которой веры нет никому. НЕ мок самого
fetch.py (ловит обход только если обход тоже зовёт fetch) и НЕ мок garminconnect-клиента
(дрейф по транспорту). Двойной контур с непересекающимися слепыми зонами (Q4-доп):

  - socket.socket.connect      — ШИРОКИЙ: ловит любой транспорт (в т.ч. будущий httpx),
                                 СЛЕП к keep-alive (пул переиспользует сокет, connect не
                                 зовётся повторно). На текущем стекe — СТРАХОВКА на смену
                                 библиотеки, как активный контур сегодня НЕ срабатывает.
  - http.client.putrequest     — ТОЧНЫЙ: ловит каждый HTTP-запрос (keep-alive или нет),
                                 СЛЕП к смене библиотеки. На текущем стеке
                                 (garminconnect→requests→urllib3→http.client) — АКТИВНАЯ
                                 защита.

Слепые зоны не пересекаются: keep-alive обходит socket, смена транспорта обходит
http.client. Любой поход к серверу пробивает хотя бы один контур.

ForbiddenNetworkAccess наследует BaseException, НЕ Exception: fetch.py._call ловит
`except Exception` (retry по 429) — от Exception страж был бы проглочен и превращён в
retry/штатную ошибку. BaseException пролетает `except Exception` наверх нетронутым, тип
сохраняется, тест (б) видит именно ForbiddenNetworkAccess, а не результат ретраев.
"""
from __future__ import annotations

import contextlib
import http.client
import socket
from typing import Iterator


class ForbiddenNetworkAccess(BaseException):
    """Сеть тронута под запретом стража. BaseException — чтобы пролетать `except
    Exception` в fetch.py._call нетронутым (иначе retry проглотит). Уникальный тип =
    контракт мока: тест отличает «дошли до сети» от «упали раньше» (ValueError/KeyError)
    по типу, без инспекции traceback (traceback дрейфует от рефакторинга — Q4 разв. F)."""

    def __init__(self, contour: str, detail: str = "") -> None:
        self.contour = contour  # 'socket' | 'http.client' — какой контур сработал
        super().__init__(
            f"сеть под запретом стража (контур: {contour})"
            + (f": {detail}" if detail else "")
        )


@contextlib.contextmanager
def forbid_network() -> Iterator[None]:
    """Оба контура активны: любая попытка исходящей сети → ForbiddenNetworkAccess.

    На текущем стеке фактически срабатывает http.client-контур (перехватывает раньше,
    socket.connect может быть не вызван или скрыт пулом). socket-контур — страховка на
    будущую смену транспорта; проверяется ОТДЕЛЬНОЙ синтетикой (calibrate.py), не этой
    связкой — здесь он существует, но на реальном HTTP-пути может не выстрелить.
    """
    orig_connect = socket.socket.connect
    orig_putrequest = http.client.HTTPConnection.putrequest

    def blocked_connect(self, address, *a, **k):  # noqa: ANN001
        raise ForbiddenNetworkAccess("socket", f"connect{address!r}")

    def blocked_putrequest(self, method, url, *a, **k):  # noqa: ANN001
        raise ForbiddenNetworkAccess("http.client", f"{method} {url}")

    socket.socket.connect = blocked_connect
    http.client.HTTPConnection.putrequest = blocked_putrequest
    try:
        yield
    finally:
        socket.socket.connect = orig_connect
        http.client.HTTPConnection.putrequest = orig_putrequest
