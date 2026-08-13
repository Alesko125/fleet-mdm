#!/bin/sh
# Старт контейнера API.
#
# Міграції виконуються роллю-власником схеми (MIGRATION_DATABASE_URL), а сам
# застосунок працює роллю з обмеженими правами: змінювати схему в робочому
# режимі він не повинен уміти взагалі.
set -e

echo "[entrypoint] чекаємо на PostgreSQL..."
python - <<'PY'
import asyncio, os, sys, time
from sqlalchemy.ext.asyncio import create_async_engine
from app.core.config import get_settings

async def wait() -> None:
    url = os.environ.get("MIGRATION_DATABASE_URL") or get_settings().sqlalchemy_url
    deadline = time.time() + 60
    while True:
        engine = create_async_engine(url)
        try:
            async with engine.connect():
                pass
            await engine.dispose()
            return
        except Exception as exc:
            await engine.dispose()
            if time.time() > deadline:
                print(f"[entrypoint] БД недоступна: {exc}", file=sys.stderr)
                raise SystemExit(1)
            await asyncio.sleep(2)

asyncio.run(wait())
PY

echo "[entrypoint] застосовуємо міграції..."
if [ -n "$MIGRATION_DATABASE_URL" ]; then
    # Alembic читає DATABASE_URL із того самого Settings, тож на час міграції
    # підміняємо його DSN власника — і лише в цьому підпроцесі.
    DATABASE_URL="$MIGRATION_DATABASE_URL" alembic upgrade head
else
    alembic upgrade head
fi

echo "[entrypoint] запуск: $*"
exec "$@"
