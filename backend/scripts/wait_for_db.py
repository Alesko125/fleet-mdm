"""Чекає, поки база даних почне приймати підключення.

Використовується `start-backend.bat` перед міграціями.

Чому не `docker compose exec pg_isready`: той шлях залежить від розбору
всього compose-файлу (а отже — від змінних оточення й наявності `.env`) і
мовчки падає з причин, не пов'язаних зі станом БД. Тут ми просто пробуємо
підключитися тими самими обліковими даними, якими працюватиме застосунок:
якщо це вдалося — БД справді готова.

Використання:
    python scripts/wait_for_db.py [--url DSN] [--timeout 60]

Код виходу: 0 — готова, 1 — не дочекалися (з поясненням причини).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from sqlalchemy.ext.asyncio import create_async_engine


def _describe(error: BaseException) -> str:
    """Коротке людське пояснення, що саме не так."""
    text = str(error)
    lowered = text.lower()

    if "password authentication failed" in lowered or "authentication" in lowered:
        return (
            "невірний пароль до бази.\n"
            "        Найчастіша причина: том із даними лишився від попереднього\n"
            "        запуску з іншим паролем. Прибрати й почати з чистого:\n"
            "            start-backend.bat reset"
        )
    if "does not exist" in lowered:
        return "бази або ролі не існує (том міг бути створений з іншими параметрами)."
    if "connection refused" in lowered or "cannot connect" in lowered:
        return "сервер ще не приймає підключення."
    if "timeout" in lowered:
        return "перевищено час очікування підключення."
    return text.splitlines()[0][:200]


async def wait(url: str, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    attempt = 0
    last: BaseException | None = None

    while time.monotonic() < deadline:
        attempt += 1
        engine = create_async_engine(url, pool_pre_ping=True)
        try:
            async with engine.connect():
                pass
            await engine.dispose()
            print(f"        База готова (спроба {attempt}).")
            return 0
        except Exception as exc:  # noqa: BLE001 — будь-яка помилка = ще не готова
            last = exc
            await engine.dispose()
            await asyncio.sleep(1.5)

    print("[ПОМИЛКА] База не відповіла вчасно.", file=sys.stderr)
    if last is not None:
        print(f"        Причина: {_describe(last)}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    if not args.url:
        print("[ПОМИЛКА] Не вказано DSN бази (--url або DATABASE_URL).", file=sys.stderr)
        return 1

    try:
        return asyncio.run(wait(args.url, args.timeout))
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
