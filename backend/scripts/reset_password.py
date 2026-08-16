"""Скидання пароля адміністратора з командного рядка.

Потрібно, коли доступ до панелі втрачено: `BOOTSTRAP_ADMIN_PASSWORD`
застосовується лише поки таблиця користувачів порожня, тож заміна .env
на працюючій базі нічого не змінює — обліковий запис лишається зі старим
паролем.

Використання:
    python scripts/reset_password.py admin@example.com
    python scripts/reset_password.py admin@example.com --password "..."

Без --password береться значення BOOTSTRAP_ADMIN_PASSWORD із конфігурації:
так пароль у .env і пароль у базі знову збігаються.

Дія записується в аудит і відкликає всі активні сесії користувача.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

# Скрипт запускають як файл (`python scripts/reset_password.py`), тож корінь
# пакета `app` у sys.path сам не потрапляє.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.security import hash_password, validate_password_strength
from app.db.models import User
from app.services.audit import AuditAction, record_audit
from app.services.auth import revoke_all_sessions


async def reset(email: str, password: str) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as session:
            user = (
                await session.execute(
                    sa.select(User).where(User.email == email.strip().lower())
                )
            ).scalar_one_or_none()
            if user is None:
                print(f"[ПОМИЛКА] Користувача {email} не знайдено.", file=sys.stderr)
                return 1

            problems = validate_password_strength(password)
            if problems:
                print("[ПОМИЛКА] Пароль не відповідає вимогам:", file=sys.stderr)
                for problem in problems:
                    print(f"          - {problem}", file=sys.stderr)
                return 1

            user.password_hash = hash_password(password)
            # Зміна пароля має негайно вимикати вже видані токени — інакше
            # стара сесія переживе скидання.
            await revoke_all_sessions(session, user, reason="password_reset_cli")
            await record_audit(
                session,
                action=AuditAction.PASSWORD_RESET,
                actor_label="cli",
                user=user,
                object_type="user",
                object_id=user.id,
                details={"email": user.email, "source": "reset_password.py"},
            )
            await session.commit()
    finally:
        await engine.dispose()

    print(f"Пароль для {email} оновлено. Активні сесії відкликано.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument(
        "--password",
        help="новий пароль; без нього береться BOOTSTRAP_ADMIN_PASSWORD",
    )
    args = parser.parse_args()

    password = args.password or get_settings().bootstrap_admin_password
    if not password:
        print(
            "[ПОМИЛКА] Пароль не задано: вкажіть --password або "
            "BOOTSTRAP_ADMIN_PASSWORD у конфігурації.",
            file=sys.stderr,
        )
        return 1

    return asyncio.run(reset(args.email, password))


if __name__ == "__main__":
    raise SystemExit(main())
