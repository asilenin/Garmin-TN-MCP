"""test_netguard.py — доказательство корректности стража сети (этап 7.6, шаг 1).

ДВА уровня, с ЖЁСТКИМ ГЕЙТОМ между ними:

  A. Синтетика (а)(б) на КАЖДОМ контуре отдельно — герметична, без реальной сети.
     Доказывает, что инструмент (netguard) сам корректен на контролируемом входе:
     позитив (сеть под запретом → ForbiddenNetworkAccess) и негатив (cache-only путь,
     сокет не открывается → страж молчит, ложных срабатываний нет). Пара, не один
     позитив: непроверенный негатив socket-контура всплыл бы при миграции на httpx —
     в худший момент (Q4-доп).

  B. Калибровка на голом fetch.py — картография реальных сетевых точек. ТРЕБУЕТ токенов
     Garmin, идёт ТОЛЬКО у владельца. Запускается отдельной командой (--calibrate) и
     ГЕЙТ: сам прогоняет A первым и ОТКАЗЫВАЕТСЯ идти на fetch.py, если A не зелёная
     (иначе неожиданный красный на fetch.py неотличим — баг мока или утечка fetch.py).

Штатный прогон (без флага) = только A, герметичен, бежит в CI без токенов и сети.
"""
import os
import socket
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))

from netguard import ForbiddenNetworkAccess, forbid_network  # noqa: E402


# --------------------------------------------------------------------------- #
# A. СИНТЕТИКА (а)(б) — герметична, без реальной сети
# --------------------------------------------------------------------------- #
def _synthetic_ab() -> None:
    # --- http.client-контур ---
    # (а) позитив: попытка HTTP-запроса под запретом → ForbiddenNetworkAccess,
    #     контур именно http.client. НЕ открываем реальный сокет: putrequest
    #     перехвачен до соединения (конструктор HTTPConnection сети не трогает).
    import http.client as _hc
    conn = _hc.HTTPConnection("example.invalid", 80, timeout=0.01)
    with forbid_network():
        try:
            conn.putrequest("GET", "/")
            raise AssertionError("http.client-контур не сработал на putrequest")
        except ForbiddenNetworkAccess as e:
            assert e.contour == "http.client", e.contour
    print("  A.http.client (а) позитив: putrequest под запретом → Forbidden OK")

    # (б) негатив: cache-only путь (никакого HTTP) под запретом → страж молчит.
    #     Герметично: только память, ни одного сетевого объекта.
    with forbid_network():
        cache = {"2026-07-01": {"sleep": 7.5}}          # имитация wellness_cache-hit
        got = cache.get("2026-07-01")
        assert got == {"sleep": 7.5}
    print("  A.http.client (б) негатив: cache-only путь → страж молчит OK")

    # --- socket-контур ---
    # (а) позитив: ПРЯМОЙ socket.connect в обход http.client → Forbidden, контур socket.
    #     Это единственный способ проверить socket-контур: на реальном HTTP-стеке он
    #     не выстрелит (http.client перехватит раньше), дефект всплыл бы при миграции.
    #     Адрес недостижимый/резервный (TEST-NET-1, RFC 5737) — но connect перехвачен
    #     ДО реального обращения, так что сеть не трогается даже при живом интернете.
    with forbid_network():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("192.0.2.1", 80))
            raise AssertionError("socket-контур не сработал на connect")
        except ForbiddenNetworkAccess as e:
            assert e.contour == "socket", e.contour
        finally:
            s.close()
    print("  A.socket (а) позитив: прямой connect под запретом → Forbidden OK")

    # (б) негатив socket-контура: cache-only путь, где сокет СОЗДАЁТСЯ, но connect не
    #     зовётся (напр. локальная работа с объектом) → страж молчит. Герметично:
    #     создание сокета сети не трогает, connect не вызывается.
    with forbid_network():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _ = s.fileno() >= 0           # локальная операция, без connect
        finally:
            s.close()
    print("  A.socket (б) негатив: сокет создан, connect не зван → страж молчит OK")

    print("A СИНТЕТИКА (а)(б) на обоих контурах — ЗЕЛЁНАЯ")


# --------------------------------------------------------------------------- #
# B. КАЛИБРОВКА на голом fetch.py — только у владельца, за гейтом A
# --------------------------------------------------------------------------- #
def _calibrate_on_fetch(slug: str) -> None:
    # ГЕЙТ: A должна пройти ДО наведения инструмента на неизвестное (fetch.py).
    print("ГЕЙТ: прогон синтетики A перед калибровкой на fetch.py...")
    _synthetic_ab()
    print("ГЕЙТ пройден — инструмент доказан на синтетике, навожу на fetch.py.\n")

    sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))
    import profiles
    from fetch import Fetcher

    prof = profiles.resolve(slug)
    print(f"картография сетевых точек fetch.py (профиль {slug}):")
    print("КРАСНЫЙ ожидаем и НОРМАЛЕН — показывает, ГДЕ реально открывается сеть.\n")
    print("ФАКТ прошлого прогона: первый запрос на свежем соединении ловит socket-контур")
    print("(connect опережает putrequest). http.client-контур активен ТОЛЬКО на keep-alive")
    print("пути (connect не повторяется) — его на реальном стеке проверяет точка [3].\n")

    # Точка 1: ленивый login (_connect → client.login → сокет).
    print("[1] Fetcher.client (ленивый login):")
    f = Fetcher(tokenstore=prof.tokens_dir)
    with forbid_network():
        try:
            _ = f.client
            print("    login НЕ тронул сеть под запретом — неожиданно (кэш токенов?).")
        except ForbiddenNetworkAccess as e:
            print(f"    login → Forbidden, контур={e.contour}: {e}")
        except BaseException as e:  # noqa: BLE001
            print(f"    login упал иначе ({type(e).__name__}): {e}")

    # Точка 2: один сетевой вызов данных (list_activities → _call → сокет).
    print("[2] list_activities (один _call):")
    f2 = Fetcher(tokenstore=prof.tokens_dir)
    with forbid_network():
        try:
            f2.list_activities("2026-07-01", "2026-07-01")
            print("    list_activities НЕ тронул сеть под запретом — неожиданно.")
        except ForbiddenNetworkAccess as e:
            print(f"    list_activities → Forbidden, контур={e.contour}: {e}")
        except BaseException as e:  # noqa: BLE001
            print(f"    list_activities упал иначе ({type(e).__name__}): {e}")

    # Точка 3: keep-alive путь — единственный, где http.client-контур может выстрелить
    # вживую (connect не повторяется → socket-контур слеп → ловит http.client, ЕСЛИ
    # соединение переиспользовано). Распадается на предусловие [3a] и проверку [3b] с
    # гейтом: [3b] диагностически валиден ТОЛЬКО если [3a] доказал переиспользование.
    #
    # [3a] БЕЗ стража: считаем реальные connect по АДРЕСУ (host,port), не по числу.
    #   Различение по адресу критично: два connect к ОДНОМУ (host,port) = свежие
    #   соединения (пула нет); один connect, второй запрос без него = keep-alive.
    #   Счётчик по числу спутал бы keep-alive с «второй запрос к другому хосту».
    print("[3] keep-alive путь (проверка http.client-контура вживую):")
    print("  [3a] предусловие БЕЗ стража — воспроизводится ли переиспособление TCP:")
    connects: list[tuple] = []
    _orig_connect = socket.socket.connect

    def _counting_connect(self, address, *a, **k):  # noqa: ANN001
        connects.append(tuple(address) if isinstance(address, tuple) else (address,))
        return _orig_connect(self, address, *a, **k)

    f3 = Fetcher(tokenstore=prof.tokens_dir)
    socket.socket.connect = _counting_connect
    try:
        f3.list_activities("2026-07-01", "2026-07-01")   # вызов 1 (свежее соединение)
        n_after_first = len(connects)
        f3.list_activities("2026-06-30", "2026-06-30")   # вызов 2 (тот же Fetcher)
        n_after_second = len(connects)
    except BaseException as e:  # noqa: BLE001
        socket.socket.connect = _orig_connect
        print(f"    [3a] упал до измерения ({type(e).__name__}): {e}")
        print("    keep-alive не измерен — [3b] пропущен (нет предусловия).")
        print("\nкартография завершена. Верни вывод целиком.")
        return
    finally:
        socket.socket.connect = _orig_connect

    new_connects = connects[n_after_first:n_after_second]
    print(f"    connect за вызов 1: {connects[:n_after_first]}")
    print(f"    connect за вызов 2: {new_connects}")

    reused = len(new_connects) == 0  # второй вызов не открыл нового connect → keep-alive
    if reused:
        print("    → keep-alive ВОСПРОИЗВЁЛСЯ (вызов 2 без нового connect). [3b] валиден.")
        print("  [3b] под стражем — кто ловит второй (keep-alive) запрос:")
        f4 = Fetcher(tokenstore=prof.tokens_dir)
        f4.list_activities("2026-07-01", "2026-07-01")    # прогрев: соединение открыто
        with forbid_network():
            try:
                f4.list_activities("2026-06-30", "2026-06-30")   # keep-alive под стражем
                print("    keep-alive запрос НЕ пойман — ни один контур. Дыра, разбор.")
            except ForbiddenNetworkAccess as e:
                print(f"    keep-alive → Forbidden, контур={e.contour} "
                      f"(ожидание: http.client): {e}")
            except BaseException as e:  # noqa: BLE001
                print(f"    keep-alive упал иначе ({type(e).__name__}): {e}")
    else:
        print("    → keep-alive НЕ воспроизвёлся (вызов 2 открыл новый connect к тем же/")
        print("       другим адресам). На garminconnect-стеке пул синхронно не живёт.")
        print("       [3b] пропущен: http.client-контур вживую недостижим, остаётся")
        print("       страховкой по построению (проверен только синтетикой A).")

    print("\nкартография завершена. Верни вывод целиком — точки [1][2][3a]([3b]).")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--calibrate":
        slug = sys.argv[2] if len(sys.argv) > 2 else "anton"
        _calibrate_on_fetch(slug)
    else:
        _synthetic_ab()
        print("\nnetguard шаг 1: синтетика зелёная. Калибровку на fetch.py запусти:")
        print("  uv run python tests/test_netguard.py --calibrate anton")
