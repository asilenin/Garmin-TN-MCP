"""Одноразовая авторизация: вход с паролем + MFA, сохранение токенов 0.3.x.

После этого garmin-raw-mcp и garmin-raw-export работают по токенам без логина
(и без риска 429 от повторных входов). Токены лежат в ~/.garminconnect.
"""
from __future__ import annotations

import getpass
import os

from garminconnect import Garmin

from .backend import TOKENSTORE


def _ask_mfa() -> str:
    # Читаем код прямо с терминала (/dev/tty) — работает даже когда stdin занят.
    with open("/dev/tty") as tty:
        print("MFA code: ", end="", flush=True)
        return tty.readline().strip()


def main() -> None:
    email = os.environ.get("GARMIN_EMAIL") or input("Garmin email: ").strip()
    pwd = os.environ.get("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")
    client = Garmin(email, pwd, prompt_mfa=_ask_mfa)
    client.login(TOKENSTORE)  # 0.3.x сохраняет токены в tokenstore
    print(
        f"OK. Токены сохранены в {TOKENSTORE}. "
        f"Теперь garmin-raw-mcp и garmin-raw-export работают без логина."
    )


if __name__ == "__main__":
    main()
